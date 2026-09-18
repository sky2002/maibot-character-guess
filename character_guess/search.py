from pathlib import Path
from typing import Any, Dict, List, Optional

import asyncio
import hashlib
import json
import os
import signal
import tempfile
import time

import httpx

from .config import Config
from .models import Evidence, Game, Research, parse_object
from .store import Store


class SearchError(Exception):
    def __init__(self, code: str, message: str, retry_at: float = 0) -> None:
        super().__init__(message)
        self.code = code
        self.retry_at = retry_at


def load_prompt(locale: str, name: str) -> str:
    path = Path(__file__).parent / "prompts" / f"{locale}.json"
    return json.loads(path.read_text(encoding="utf-8"))[name]


def search_payload(game: Game, query: str) -> Dict[str, Any]:
    # 不把群号、账号、聊天记录和玩家身份发送给搜索服务。
    return {
        "query": query,
        "answers": [q.model_dump() for q in game.questions if q.answer],
        "hint": game.hint,
        "excluded_characters": game.rejected,
    }


def codex_error(events: List[Dict], stderr: str, returncode: int) -> Optional[SearchError]:
    errors = []
    for event in events:
        if event.get("type") in {"error", "turn.failed"}:
            errors.append(event.get("error", event))
    if not errors and returncode == 0:
        return None
    # 仅检查错误事件与进程诊断，绝不把网页或模型正文当成配额错误。
    diagnostic = json.dumps(errors, ensure_ascii=False) + "\n" + stderr
    lowered = diagnostic.lower().replace("’", "'")
    quota = any(
        marker in lowered
        for marker in (
            "usage_limit_reached",
            "insufficient_quota",
            "quota_exceeded",
            "credits_depleted",
            "you've hit your usage limit",
            "you have hit your usage limit",
            "usage limit reached",
        )
    )
    if quota:
        retry_at = 0.0
        for error in errors:
            if isinstance(error, dict):
                for key in ("resets_at", "reset_at", "resets_at_unix"):
                    value = error.get(key)
                    if isinstance(value, (int, float)) and value > time.time():
                        retry_at = max(retry_at, float(value))
        return SearchError("quota", "Codex 额度已耗尽", retry_at)
    return SearchError("codex_failed", "Codex 调用失败，请在部署机器检查登录与 CLI 配置")


async def read_limited(reader: asyncio.StreamReader, limit: int = 2_000_000) -> bytes:
    chunks = []
    size = 0
    while chunk := await reader.read(16384):
        size += len(chunk)
        if size > limit:
            raise SearchError("output_limit", "Codex 输出超出限制")
        chunks.append(chunk)
    return b"".join(chunks)


async def stop_process(process: asyncio.subprocess.Process) -> None:
    """Linux 同时清理 CLI 派生的进程；超时和插件卸载不能遗留任务。"""
    if os.name != "posix" and process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    await process.wait()


class CodexSearch:
    def __init__(self, config: Config, runtime_dir: Path) -> None:
        self.config = config
        self.runtime_dir = runtime_dir

    def command(self, directory: Path) -> List[str]:
        cfg = self.config.codex
        args = [
            cfg.executable,
            "-a",
            "never",
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--json",
            "--color",
            "never",
            "--model",
            cfg.model,
            "--cd",
            str(directory),
            "--output-schema",
            str(directory / "schema.json"),
            "--output-last-message",
            str(directory / "result.json"),
        ]
        overrides = [
            'web_search="live"',
            'model_provider="openai"',
            'forced_login_method="chatgpt"',
            'model_reasoning_effort="low"',
            "features.shell_tool=false",
            "features.unified_exec=false",
            "features.apps=false",
            "features.multi_agent=false",
            "features.multi_agent_v2=false",
            "features.skill_search=false",
            "features.skill_mcp_dependency_install=false",
            "features.skip_host_skill_discovery=true",
            "mcp_servers={}",
            "hooks={}",
        ]
        for setting in overrides:
            args.extend(["-c", setting])
        return args + ["-"]

    def environment(self) -> Dict[str, str]:
        allowed = {
            "PATH",
            "HOME",
            "USER",
            "LOGNAME",
            "LANG",
            "LC_ALL",
            "TZ",
            "TMPDIR",
            "TMP",
            "TEMP",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "CODEX_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "XDG_RUNTIME_DIR",
        }
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        if self.config.codex.home:
            env["CODEX_HOME"] = self.config.codex.home
        return env

    async def search(self, payload: Dict[str, Any]) -> Research:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="search-", dir=self.runtime_dir) as temp:
            directory = Path(temp).resolve()
            (directory / "schema.json").write_text(json.dumps(Research.model_json_schema()), encoding="utf-8")
            prompt = (
                load_prompt(self.config.game.prompt_locale, "search") + "\n" + json.dumps(payload, ensure_ascii=False)
            )
            try:
                process = await asyncio.create_subprocess_exec(
                    *self.command(directory),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=directory,
                    env=self.environment(),
                    start_new_session=os.name == "posix",
                )
            except OSError as exc:
                raise SearchError("unavailable", "无法启动 Codex CLI，请检查路径与执行权限") from exc
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            stdout_task = asyncio.create_task(read_limited(process.stdout))
            stderr_task = asyncio.create_task(read_limited(process.stderr))
            try:
                async with asyncio.timeout(self.config.codex.timeout_seconds):
                    try:
                        process.stdin.write(prompt.encode("utf-8"))
                        await process.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        # CLI 在读完输入前拒绝请求时，仍解析它的错误事件（包括额度耗尽）。
                        pass
                    process.stdin.close()
                    stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
                    await process.wait()
            except TimeoutError as exc:
                raise SearchError("timeout", "Codex 搜索超时") from exc
            finally:
                await stop_process(process)
                for task in (stdout_task, stderr_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            events = []
            for line in stdout.decode("utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
            error = codex_error(events, stderr.decode("utf-8", errors="replace"), process.returncode or 0)
            if error:
                raise error
            searched = any(
                e.get("type") == "item.completed"
                and e.get("item", {}).get("type") in {"web_search", "web_search_call"}
                and e.get("item", {}).get("status", "completed") == "completed"
                for e in events
            )
            if not searched or not any(e.get("type") == "turn.completed" for e in events):
                raise SearchError("not_searched", "Codex 没有完成实际联网搜索")
            result_path = directory / "result.json"
            if not result_path.exists() or result_path.stat().st_size > 256_000:
                raise SearchError("bad_result", "Codex 没有返回可用的检索结果")
            try:
                result = Research.model_validate(parse_object(result_path.read_text(encoding="utf-8")))
            except (ValueError, OSError) as exc:
                raise SearchError("bad_result", "Codex 检索结果格式不正确") from exc
            if not result.sources:
                raise SearchError("no_sources", "Codex 搜索未提供可追溯来源")
            return result


class DeepSeekSearch:
    def __init__(self, config: Config, store: Store, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self.config = config
        self.store = store
        self.transport = transport

    async def search(self, payload: Dict[str, Any]) -> Research:
        cfg = self.config.deepseek
        key = os.environ.get(cfg.api_key_env, "").strip()
        if not key:
            raise SearchError("not_configured", f"未配置环境变量 {cfg.api_key_env}")
        if not self.store.reserve_deepseek(cfg.daily_requests, cfg.daily_output_tokens, cfg.max_output_tokens):
            raise SearchError("budget", "DeepSeek 今日请求或输出预算已用完")
        body = {
            "model": cfg.model,
            "max_tokens": cfg.max_output_tokens,
            "stream": False,
            "system": load_prompt(self.config.game.prompt_locale, "search"),
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False)
                    + "\nJSON schema:\n"
                    + json.dumps(Research.model_json_schema(), ensure_ascii=False),
                }
            ],
            "tools": [{"type": cfg.tool_type, "name": "web_search", "max_uses": cfg.max_uses}],
        }
        try:
            async with asyncio.timeout(cfg.timeout_seconds):
                async with httpx.AsyncClient(timeout=cfg.timeout_seconds, transport=self.transport) as client:
                    response = await client.post(
                        cfg.base_url.rstrip("/") + "/v1/messages",
                        json=body,
                        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                    )
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise SearchError("timeout", "DeepSeek 搜索超时") from exc
        except httpx.HTTPError as exc:
            raise SearchError("network", "DeepSeek 搜索网络错误") from exc
        if response.status_code != 200:
            raise SearchError("http_error", f"DeepSeek 返回 HTTP {response.status_code}")
        try:
            data = response.json()
            blocks = data.get("content", [])
            urls = set()
            search_errors = []
            called = False
            for block in blocks:
                if block.get("type") == "server_tool_use" and block.get("name") == "web_search":
                    called = True
                if block.get("type") == "web_search_tool_result":
                    content = block.get("content", [])
                    items = content if isinstance(content, list) else [content]
                    for item in items:
                        if item.get("error_code"):
                            search_errors.append(item["error_code"])
                        if item.get("url"):
                            urls.add(item["url"])
            if data.get("stop_reason") != "end_turn":
                raise SearchError("incomplete", "DeepSeek 搜索未正常结束（暂停或输出截断），本次不采用")
            if not called or not urls or search_errors:
                raise SearchError("not_searched", "DeepSeek 未返回成功的服务端搜索结果")
            text = "\n".join(b["text"] for b in blocks if b.get("type") == "text")
            result = Research.model_validate(parse_object(text))
            # 摘要中的来源必须能追溯到本次服务端搜索结果，而非模型自行编造 URL。
            result.sources = [source for source in result.sources if source.url in urls]
            allowed = {source.url for source in result.sources}
            for candidate in result.candidates:
                candidate.source_urls = [url for url in candidate.source_urls if url in allowed]
            if not result.sources:
                raise SearchError("no_sources", "DeepSeek 摘要来源与实际搜索结果不匹配")
            return result
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise SearchError("bad_result", "DeepSeek 检索结果格式不正确") from exc


class SearchRouter:
    def __init__(self, config: Config, store: Store, runtime_dir: Path) -> None:
        self.config = config
        self.store = store
        self.codex = CodexSearch(config, runtime_dir)
        self.deepseek = DeepSeekSearch(config, store)
        self.semaphore = asyncio.Semaphore(config.search.max_parallel)

    async def search(self, game: Game, query: str) -> Evidence:
        payload = search_payload(game, query)
        cache_key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if self.config.search.cache_hours:
            cached = self.store.cached(cache_key)
            if cached:
                return cached
        try:
            async with asyncio.timeout(5):
                await self.semaphore.acquire()
        except TimeoutError as exc:
            raise SearchError("busy", "搜索队列繁忙，请稍后继续") from exc
        try:
            errors = []
            if self.config.codex.enabled and time.time() >= self.store.quota_until():
                try:
                    result = await self.codex.search(payload)
                    evidence = Evidence(query=query, backend="codex", research=result)
                    self.store.cache(cache_key, evidence, self.config.search.cache_hours)
                    return evidence
                except SearchError as exc:
                    errors.append(str(exc))
                    if exc.code == "quota":
                        retry_at = exc.retry_at or time.time() + self.config.codex.quota_retry_minutes * 60
                        self.store.set_quota_until(retry_at)
            elif self.config.codex.enabled:
                errors.append("Codex 额度耗尽，正在等待恢复")
            if self.config.deepseek.enabled:
                try:
                    result = await self.deepseek.search(payload)
                    evidence = Evidence(query=query, backend="deepseek", research=result)
                    self.store.cache(cache_key, evidence, self.config.search.cache_hours)
                    return evidence
                except SearchError as exc:
                    errors.append(str(exc))
            raise SearchError("unavailable", "；".join(errors) or "没有启用搜索后端")
        finally:
            self.semaphore.release()
