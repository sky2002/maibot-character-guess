"""部署检查：默认不请求模型；--probe 才会使用真实额度。"""

from pathlib import Path

import argparse
import asyncio
import json
import os
import shutil
import tempfile
import tomllib

from .config import Config
from .search import CodexSearch, DeepSeekSearch, SearchError
from .store import Store


async def probe(config: Config, backend: str, data_dir: Path) -> None:
    store = Store(data_dir)
    try:
        with tempfile.TemporaryDirectory(prefix="guess-check-") as runtime:
            client = CodexSearch(config, Path(runtime)) if backend == "codex" else DeepSeekSearch(config, store)
            result = await client.search(
                {
                    "query": "核实初音未来的官方角色设定，返回可追溯资料。",
                    "answers": [],
                    "hint": "",
                    "excluded_characters": [],
                }
            )
            print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--probe", choices=["codex", "deepseek"], help="执行真实搜索，会消耗对应服务额度")
    parser.add_argument("--data-dir", type=Path, help="探测时必填，使用该目录保存预算，不要反复更换")
    args = parser.parse_args()
    if not args.config.is_file():
        parser.error("配置文件不存在；先复制 config.example.toml 并填写部署信息")
    config = Config.model_validate(tomllib.loads(args.config.read_text(encoding="utf-8")))
    print("配置格式：正常")
    print("Codex 可执行文件：" + (shutil.which(config.codex.executable) or "未找到"))
    print("Codex 登录：请在同一 Linux 用户下运行 codex login status 检查（本工具不读取登录令牌）")
    print("DeepSeek 密钥环境变量：" + ("已设置" if os.environ.get(config.deepseek.api_key_env) else "未设置"))
    if args.probe:
        if args.data_dir is None:
            parser.error("真实探测需指定 --data-dir；建议使用 MaiBot 分配的插件 data_dir")
        try:
            asyncio.run(probe(config, args.probe, args.data_dir))
        except SearchError as exc:
            parser.exit(1, f"检索未通过：{exc.code}：{exc}\n")


if __name__ == "__main__":
    main()
