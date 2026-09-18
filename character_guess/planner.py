from typing import Any, Dict, List

import asyncio
import json

from .config import Config
from .models import Decision, Game, parse_object
from .search import load_prompt


class PlanningError(Exception):
    pass


class Planner:
    def __init__(self, context: Any, config: Config) -> None:
        self.context = context
        self.config = config

    async def decide(self, game: Game, search_allowed: bool, notes: List[str]) -> Decision:
        payload: Dict = {
            "questions": [q.model_dump() for q in game.questions],
            "rejected_characters": game.rejected,
            "evidence": [e.model_dump() for e in game.evidence[-6:]],
            "hint_used": game.hint_used,
            "hint": game.hint,
            "search_allowed": search_allowed,
            "notes": notes,
            "player_reconfirmed_questions": game.kept_conflicts,
            "remaining_questions": game.limit - len(game.questions),
        }
        messages = [
            {
                "role": "system",
                "content": load_prompt(self.config.game.prompt_locale, "game")
                + "\nJSON schema:\n"
                + json.dumps(Decision.model_json_schema(), ensure_ascii=False),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        try:
            async with asyncio.timeout(self.config.game.model_timeout_seconds):
                result = await self.context.llm.generate(
                    prompt=messages,
                    task_name=self.config.game.task_name,
                    model_name=self.config.game.model_name,
                    max_tokens=1800,
                )
        except TimeoutError as exc:
            raise PlanningError("游戏推理超时") from exc
        if not result.get("success"):
            # 不向群聊回显底层异常，避免泄露模型配置或鉴权信息。
            raise PlanningError("游戏模型调用失败，请检查所选模型或任务名")
        try:
            decision = Decision.model_validate(parse_object(result.get("response", "")))
        except (ValueError, TypeError) as exc:
            raise PlanningError("游戏模型返回的决策格式不正确") from exc
        if decision.action in {"ask", "hint", "clarify"} and not decision.text.strip():
            raise PlanningError("游戏模型没有给出问题内容")
        if decision.action == "guess" and not decision.character.strip():
            raise PlanningError("游戏模型没有给出角色名称")
        if decision.action == "search" and not decision.query.strip():
            raise PlanningError("游戏模型没有给出检索内容")
        return decision
