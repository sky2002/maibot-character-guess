from unittest.mock import AsyncMock

import asyncio
import json
import sys
import time

import httpx
import pytest

from character_guess.config import Config
from character_guess.models import Game, Research
from character_guess.search import CodexSearch, DeepSeekSearch, SearchError, SearchRouter, codex_error, search_payload
from character_guess.store import Store
from test_game import evidence


def test_codex_quota_detection_ignores_web_text_and_429():
    assert codex_error([{"type": "item.completed", "item": {"text": "usage_limit_reached"}}], "", 0) is None
    assert codex_error([{"type": "error", "message": "HTTP 429 rate limit"}], "", 1).code == "codex_failed"
    reset = time.time() + 1000
    exc = codex_error([{"type": "turn.failed", "error": {"code": "usage_limit_reached", "resets_at": reset}}], "", 1)
    assert exc.code == "quota" and exc.retry_at == reset
    assert codex_error([{"type": "error", "message": "You’ve hit your usage limit"}], "", 1).code == "quota"


def test_codex_quota_recovery_retries_primary(tmp_path):
    async def run():
        store = Store(tmp_path)
        store.set_quota_until(time.time() - 1)
        router = SearchRouter(Config(), store, tmp_path)
        router.codex.search = AsyncMock(return_value=evidence().research)
        router.deepseek.search = AsyncMock()
        result = await router.search(Game(stream_id="s", owner="u"), "角色")
        assert result.backend == "codex"
        router.deepseek.search.assert_not_awaited()
        store.close()

    asyncio.run(run())


@pytest.mark.parametrize("quota", [True, False])
def test_router_fallback_and_quota_persist(tmp_path, quota):
    async def run():
        cfg, store = Config(), Store(tmp_path)
        cfg.search.cache_hours = 0
        router = SearchRouter(cfg, store, tmp_path)
        router.codex.search = AsyncMock(side_effect=SearchError("quota" if quota else "timeout", "模拟故障"))
        router.deepseek.search = AsyncMock(return_value=evidence().research)
        game = Game(stream_id="private-group", owner="private-user")
        first = await router.search(game, "问题")
        second = await router.search(game, "另一个问题")
        assert first.backend == second.backend == "deepseek"
        assert router.codex.search.await_count == (1 if quota else 2)
        store.close()
        store = Store(tmp_path)
        assert (store.quota_until() > time.time()) == quota
        store.close()

    asyncio.run(run())


def test_cache_reuses_results_and_payload_omits_ids(tmp_path):
    async def run():
        store = Store(tmp_path)
        router = SearchRouter(Config(), store, tmp_path)
        router.codex.search = AsyncMock(return_value=evidence().research)
        game = Game(stream_id="private-group", owner="private-user")
        assert "private" not in json.dumps(search_payload(game, "谁"))
        await router.search(game, "谁")
        await router.search(game, "谁")
        assert router.codex.search.await_count == 1
        store.close()

    asyncio.run(run())


def test_daily_budget_is_atomic_and_survives_restart(tmp_path):
    store = Store(tmp_path)
    assert store.reserve_deepseek(2, 8192, 4096)
    assert store.reserve_deepseek(2, 8192, 4096)
    assert not store.reserve_deepseek(2, 8192, 4096)
    store.close()
    store = Store(tmp_path)
    assert not store.reserve_deepseek(2, 8192, 4096)
    store.close()


def response_body(stop="end_turn", server=True):
    content = [{"type": "text", "text": evidence().research.model_dump_json()}]
    if server:
        content += [
            {"type": "server_tool_use", "name": "web_search", "id": "x"},
            {
                "type": "web_search_tool_result",
                "tool_use_id": "x",
                "content": [{"type": "web_search_result", "url": "https://example.com/character"}],
            },
        ]
    return {"stop_reason": stop, "content": content}


def test_deepseek_real_protocol_shape_and_source_filter(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
        store = Store(tmp_path)

        async def handler(request):
            assert request.url.path == "/anthropic/v1/messages"
            body = json.loads(request.content)
            assert body["tools"][0]["type"] == "web_search_20260209"
            assert request.headers["x-api-key"] == "test-only"
            return httpx.Response(200, json=response_body())

        backend = DeepSeekSearch(Config(), store, httpx.MockTransport(handler))
        result = await backend.search({"query": "角色甲"})
        assert result.candidates[0].source_urls == ["https://example.com/character"]
        assert result.sources
        store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "stop,server,code",
    [("end_turn", False, "not_searched"), ("pause_turn", True, "incomplete"), ("max_tokens", True, "incomplete")],
)
def test_deepseek_does_not_treat_plain_answer_or_truncation_as_search(tmp_path, monkeypatch, stop, server, code):
    async def run():
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
        store = Store(tmp_path)
        backend = DeepSeekSearch(
            Config(), store, httpx.MockTransport(lambda req: httpx.Response(200, json=response_body(stop, server)))
        )
        with pytest.raises(SearchError) as exc:
            await backend.search({"query": "角色"})
        assert exc.value.code == code
        store.close()

    asyncio.run(run())


def test_deepseek_failure_does_not_refund_unknown_bill(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
        cfg, store = Config(), Store(tmp_path)
        cfg.deepseek.daily_requests = 1
        backend = DeepSeekSearch(cfg, store, httpx.MockTransport(lambda req: httpx.Response(503)))
        with pytest.raises(SearchError, match="HTTP 503"):
            await backend.search({})
        with pytest.raises(SearchError) as exc:
            await backend.search({})
        assert exc.value.code == "budget"
        store.close()

    asyncio.run(run())


def test_codex_flags_and_environment_are_search_only(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("CODEX_API_KEY", "secret")
    backend = CodexSearch(Config(), tmp_path)
    args = backend.command(tmp_path)
    assert "features.shell_tool=false" in args and "features.unified_exec=false" in args
    assert 'forced_login_method="chatgpt"' in args
    assert "read-only" in args and "--ignore-user-config" in args
    assert not any(key.endswith("API_KEY") for key in backend.environment())


def test_codex_subprocess_reads_stdin_and_requires_search_event(tmp_path):
    async def run():
        script = tmp_path / "fake_cli.py"
        research = evidence().research.model_dump_json()
        script.write_text(
            "import sys,json,pathlib\n"
            "assert 'query' in sys.stdin.read()\n"
            f"pathlib.Path('result.json').write_text({research!r}, encoding='utf-8')\n"
            "print(json.dumps({'type':'item.completed','item':{'type':'web_search','status':'completed'}}))\n"
            "print(json.dumps({'type':'turn.completed'}))\n",
            encoding="utf-8",
        )
        backend = CodexSearch(Config(), tmp_path / "runtime")
        backend.command = lambda directory: [sys.executable, str(script)]
        result = await backend.search({"query": "角色甲"})
        assert isinstance(result, Research)
        assert not list((tmp_path / "runtime").iterdir())

    asyncio.run(run())


def test_codex_timeout_cleans_process_and_temp_files(tmp_path):
    async def run():
        cfg = Config()
        cfg.codex.timeout_seconds = 5
        backend = CodexSearch(cfg, tmp_path)
        backend.command = lambda directory: [sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(30)"]
        with pytest.raises(SearchError) as exc:
            await backend.search({"query": "角色"})
        assert exc.value.code == "timeout"
        assert not list(tmp_path.iterdir())

    asyncio.run(run())
