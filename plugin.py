"""MaiBot 猜角色入口：命令与五选短答共用同一条游戏处理链。"""

from typing import Any, Dict, Optional, Tuple

from maibot_sdk import Command, HookHandler, MaiBotPlugin
from maibot_sdk.types import HookMode, HookOrder

from .character_guess.config import Config
from .character_guess.service import Service


def message_fields(message: Any) -> Tuple[str, str, str, str, float]:
    if not isinstance(message, dict):
        return "", "", "", "", 0
    info = message.get("message_info", {})
    user = info.get("user_info", {}) if isinstance(info, dict) else {}
    text = message.get("processed_plain_text")
    if not isinstance(text, str):
        text = "".join(
            str(segment.get("data", ""))
            for segment in message.get("raw_message", [])
            if isinstance(segment, dict) and segment.get("type") == "text"
        )
    try:
        timestamp = float(message.get("timestamp", 0))
    except (TypeError, ValueError):
        timestamp = 0
    return (
        str(message.get("session_id", "")),
        str(user.get("user_id", "")),
        text.strip(),
        str(message.get("message_id", "")),
        timestamp,
    )


class CharacterGuessPlugin(MaiBotPlugin):
    config_model = Config

    def __init__(self) -> None:
        super().__init__()
        self.service: Optional[Service] = None

    async def on_load(self) -> None:
        if self.service is None and self.config.plugin.enabled:
            self.service = Service(self.ctx, self.ctx.paths.data_dir, self.ctx.paths.runtime_dir, self.config)
            self.ctx.logger.info("猜角色插件已启动，发送「猜角色 帮助」查看玩法")

    async def on_unload(self) -> None:
        if self.service is not None:
            service, self.service = self.service, None
            await service.close()

    async def on_config_update(self, scope: str, config_data: Dict[str, object], version: str) -> None:
        # 在切换配置前取消旧调用，持久化状态由新实例继续使用。
        await self.on_unload()
        await self.on_load()

    @Command(
        "character_guess",
        description="猜角色小游戏，发送「猜角色 帮助」查看玩法",
        pattern=r"^/?猜角色(?:\s+(?P<instruction>[\s\S]*))?\s*$",
    )
    async def command(self, stream_id: str = "", user_id: str = "", **kwargs: Any):
        if self.service is None:
            return False, "猜角色插件未启用", False
        stream, user, raw, message_id, timestamp = message_fields(kwargs.get("message"))
        groups = kwargs.get("matched_groups", {})
        instruction = str(groups.get("instruction") or "") if isinstance(groups, dict) else ""
        if not instruction:
            raw = str(kwargs.get("text") or raw)
            instruction = raw.lstrip("/").removeprefix("猜角色").strip()
        accepted = self.service.submit(
            stream_id or stream, user_id or user, instruction, message_id=message_id, timestamp=timestamp
        )
        return accepted, "猜角色指令已接收" if accepted else "缺少聊天流或玩家身份", accepted

    @HookHandler(
        "chat.receive.after_process",
        name="character_guess_answer",
        description="接收当前开局者的五选回答",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=3000,
    )
    async def answer(self, message: Any = None, **kwargs: Any):
        if self.service is None:
            return None
        stream, user, text, message_id, timestamp = message_fields(message)
        if self.service.submit(stream, user, text, message_id, timestamp, explicit=False):
            return {"action": "abort"}
        return None


def create_plugin() -> CharacterGuessPlugin:
    return CharacterGuessPlugin()
