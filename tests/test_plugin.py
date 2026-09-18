from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import importlib.util
import json
import logging
import sys
import tomllib

from character_guess.config import Config
from test_game import FakePlanner, choice, drain


ROOT = Path(__file__).resolve().parents[1]


def load_entry():
    spec = importlib.util.spec_from_file_location(
        "guess_plugin_test", ROOT / "plugin.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_actual_sdk_discovers_handlers_and_lifecycle(tmp_path):
    async def run():
        plugin = load_entry().create_plugin()
        ctx = SimpleNamespace(
            logger=logging.getLogger("plugin-test"),
            paths=SimpleNamespace(data_dir=tmp_path / "data", runtime_dir=tmp_path / "temp"),
            send=SimpleNamespace(text=AsyncMock(return_value=True)),
        )
        plugin._set_context(ctx)
        plugin.set_plugin_config({})
        names = {item["name"] for item in plugin.get_components()}
        assert names == {"character_guess", "character_guess_answer"}
        hook = next(item for item in plugin.get_components() if item["name"] == "character_guess_answer")
        assert hook["metadata"]["hook"] == "chat.receive.after_process"
        await plugin.on_load()
        plugin.service.planner = FakePlanner(choice(), choice(text="下一题？"))
        result = await plugin.command(stream_id="s1", user_id="u1", matched_groups={"instruction": "开始"})
        assert result[0] and result[2]
        await drain(plugin.service)
        message = {
            "session_id": "s1",
            "message_id": "m1",
            "message_info": {"user_info": {"user_id": "u1"}},
            "raw_message": [{"type": "text", "data": "不知道"}],
        }
        assert await plugin.answer(message=message) == {"action": "abort"}
        await drain(plugin.service)
        assert plugin.service.store.get("s1").questions[0].answer == "不知道"
        message["message_info"]["user_info"]["user_id"] = "other"
        assert await plugin.answer(message=message) is None
        await plugin.on_config_update("self", {}, "0.1.0")
        assert plugin.service.store.get("s1").questions[0].answer == "不知道"
        await plugin.on_unload()
        assert plugin.service is None

    asyncio.run(run())


def test_config_example_and_all_prompt_locales_match_schema():
    cfg = Config.model_validate(tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8")))
    assert cfg.model_dump() == Config().model_dump()
    for language in ["zh-CN", "en-US", "ja-JP"]:
        prompts = json.loads((ROOT / "character_guess" / "prompts" / f"{language}.json").read_text(encoding="utf-8"))
        assert set(prompts) == {"game", "search"}
        assert "question_index" in prompts["game"] and "source_urls" in prompts["search"]
