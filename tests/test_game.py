from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import logging
import time

from character_guess.config import Config
from character_guess.models import Candidate, Decision, Evidence, Game, Question, Research, Source
from character_guess.planner import Planner
from character_guess.search import SearchError
from character_guess.service import Service


def choice(action="ask", text="她是游戏角色吗？", **kwargs):
    return Decision(
        action=action,
        text=text,
        query=kwargs.get("query", ""),
        character=kwargs.get("character", ""),
        question_index=kwargs.get("question_index", 0),
    )


def evidence(name="角色甲", query="查询"):
    url = "https://example.com/character"
    return Evidence(
        query=query,
        backend="codex",
        research=Research(
            summary="资料摘要",
            candidates=[Candidate(name=name, work="作品", facts=["游戏角色"], conflicts=[], source_urls=[url])],
            sources=[Source(title="角色资料", url=url)],
            uncertainties=[],
        ),
    )


class FakePlanner:
    def __init__(self, *decisions):
        self.decisions = deque(decisions)
        self.inputs = []

    async def decide(self, game, allowed, notes):
        self.inputs.append((game.model_copy(deep=True), allowed, list(notes)))
        return self.decisions.popleft()


def service(tmp_path, *decisions):
    ctx = SimpleNamespace(
        logger=logging.getLogger("guess-test"),
        send=SimpleNamespace(text=AsyncMock(return_value=True)),
        llm=SimpleNamespace(generate=AsyncMock()),
    )
    svc = Service(ctx, tmp_path / "data", tmp_path / "runtime", Config())
    svc.planner = FakePlanner(*decisions)
    svc.search = SimpleNamespace(search=AsyncMock(return_value=evidence()))
    return svc


async def drain(svc):
    while svc.tasks or svc.notices:
        await asyncio.gather(*list(svc.tasks.values()), *list(svc.notices))


async def command(svc, text, owner="u1", stream="s1", **kwargs):
    svc.submit(stream, owner, text, **kwargs)
    await drain(svc)


def test_five_answers_owner_isolation_and_stale_messages(tmp_path):
    async def run():
        svc = service(tmp_path, *[choice(text=f"问题{i}？") for i in range(7)])
        await command(svc, "开始", message_id="start")
        assert not svc.submit("s1", "spectator", "是", explicit=False)
        opened = svc.store.get("s1").opened_at
        await command(svc, "是", timestamp=opened - 1)
        assert not svc.store.get("s1").questions[0].answer
        await command(svc, "答 8 是")
        assert len(svc.store.get("s1").questions) == 1
        for i, answer in enumerate(["是", "否", "不知道", "可能是", "可能不是"]):
            await command(svc, answer, message_id=f"a{i}", explicit=False)
        game = svc.store.get("s1")
        assert [q.answer for q in game.questions[:5]] == ["是", "否", "不知道", "可能是", "可能不是"]
        await command(svc, "是", message_id="a4")
        assert len(svc.store.get("s1").questions) == 6
        await svc.close()

    asyncio.run(run())


def test_correction_clears_derived_evidence_and_undo_restores_question(tmp_path):
    async def run():
        svc = service(tmp_path, choice(text="新问题？"))
        game = Game(
            stream_id="s1",
            owner="u1",
            phase="answer",
            pending="旧问题？",
            questions=[Question(text="第一题？", answer="是"), Question(text="旧问题？")],
            evidence=[evidence()],
        )
        svc.store.save(game)
        await command(svc, "修改 1 可能不是")
        saved = svc.store.get("s1")
        assert saved.questions[0].answer == "可能不是"
        assert not saved.evidence and len(saved.corrections) == 1
        await command(svc, "撤回")
        saved = svc.store.get("s1")
        assert len(saved.questions) == 1 and saved.questions[0].answer == ""
        assert saved.phase == "answer" and saved.pending == "第一题？"
        await svc.close()

    asyncio.run(run())


def test_hint_only_once_and_conflict_does_not_overwrite(tmp_path):
    async def run():
        svc = service(
            tmp_path,
            choice("hint", "可以给个作品提示吗？"),
            choice("hint", "再给提示？"),
            choice(text="她用剑吗？"),
            choice("clarify", "资料与第1题冲突，请确认。", question_index=1),
            choice(text="她是主角吗？"),
        )
        await command(svc, "开始")
        await command(svc, "跳过提示")
        assert svc.store.get("s1").hint_used
        await command(svc, "否")
        assert svc.store.get("s1").phase == "conflict"
        assert svc.store.get("s1").questions[0].answer == "否"
        await command(svc, "保持")
        assert svc.store.get("s1").kept_conflicts == [1]
        assert svc.store.get("s1").questions[0].answer == "否"
        await svc.close()

    asyncio.run(run())


def test_guess_checks_search_then_player_must_confirm(tmp_path):
    async def run():
        svc = service(tmp_path, choice("guess", character="角色甲"), choice("guess", character="角色甲"))
        await command(svc, "开始")
        assert svc.store.get("s1").phase == "guess"
        assert svc.search.search.await_count == 1
        assert svc.planner.inputs[-1][0].evidence
        await command(svc, "猜对了")
        assert svc.store.get("s1").phase == "finished"
        await svc.close()

    asyncio.run(run())


def test_wrong_guess_recorded_and_search_failure_disclosed(tmp_path):
    async def run():
        svc = service(
            tmp_path,
            choice("guess", character="角色甲"),
            choice("guess", character="角色甲"),
            choice("guess", character="角色甲"),
            choice(text="另一条特征？"),
        )
        svc.search.search.side_effect = SearchError("timeout", "搜索超时")
        await command(svc, "开始")
        messages = [c.args[0] for c in svc.ctx.send.text.call_args_list]
        assert any("未能充分核实" in text for text in messages)
        await command(svc, "猜错了")
        assert svc.store.get("s1").rejected == ["角色甲"]
        assert svc.store.get("s1").phase == "answer"
        await svc.close()

    asyncio.run(run())


def test_last_answer_can_still_be_guessed_without_question_21(tmp_path):
    async def run():
        svc = service(tmp_path, choice("guess", character="角色甲"), choice("guess", character="角色甲"))
        game = Game(
            stream_id="s1",
            owner="u1",
            limit=3,
            phase="answer",
            questions=[Question(text="一", answer="是"), Question(text="二", answer="否"), Question(text="三")],
        )
        svc.store.save(game)
        await command(svc, "是")
        assert svc.store.get("s1").phase == "guess"
        await svc.close()

    asyncio.run(run())


def test_limit_requires_continue(tmp_path):
    async def run():
        svc = service(tmp_path, choice(text="第四题？"), choice(text="第四题？"))
        svc.config.game.question_limit = 3
        svc.store.save(
            Game(stream_id="s1", owner="u1", limit=3, questions=[Question(text=str(i), answer="是") for i in range(3)])
        )
        await command(svc, "重试")
        assert svc.store.get("s1").phase == "limit"
        await command(svc, "继续")
        assert svc.store.get("s1").limit == 6 and len(svc.store.get("s1").questions) == 4
        await svc.close()

    asyncio.run(run())


def test_background_busy_does_not_queue_answers_and_end_cancels(tmp_path):
    async def run():
        svc = service(tmp_path, choice())
        await command(svc, "开始")
        waiting = asyncio.Event()

        async def blocked(*args):
            waiting.set()
            await asyncio.Future()

        svc.planner.decide = blocked
        svc.submit("s1", "u1", "是")
        await waiting.wait()
        svc.submit("s1", "u1", "否", explicit=False)
        svc.submit("s1", "u1", "结束")
        await drain(svc)
        game = svc.store.get("s1")
        assert game.phase == "finished" and game.questions[0].answer == "是"
        await svc.close()

    asyncio.run(run())


def test_restart_recovers_and_expired_game_allows_new_owner(tmp_path):
    async def run():
        svc = service(tmp_path, choice(text="持久化的问题？"))
        await command(svc, "开始")
        await svc.close()
        svc = service(tmp_path, choice(text="新玩家的问题？"))
        assert svc.store.get("s1").pending == "持久化的问题？"
        game = svc.store.get("s1")
        game.updated_at = time.time() - 86400
        svc.store.save(game, touch=False)
        await command(svc, "开始", owner="u2")
        assert svc.store.get("s1").owner == "u2"
        await svc.close()

    asyncio.run(run())


def test_model_json_failure_keeps_accepted_answer(tmp_path):
    async def run():
        svc = service(tmp_path, choice())
        await command(svc, "开始")
        svc.ctx.llm.generate.return_value = {"success": True, "response": "这不是JSON"}
        svc.planner = Planner(svc.ctx, svc.config)
        await command(svc, "是")
        game = svc.store.get("s1")
        assert game.questions[0].answer == "是" and game.phase == "planning"
        assert "格式不正确" in svc.ctx.send.text.call_args.args[0]
        svc.ctx.llm.generate.return_value = {
            "success": True,
            "response": choice(text="恢复后新问题？").model_dump_json(),
        }
        await command(svc, "重试")
        assert svc.store.get("s1").phase == "answer"
        payload = svc.ctx.llm.generate.call_args.kwargs["prompt"][1]["content"]
        assert "stream_id" not in payload and "owner" not in payload
        await svc.close()

    asyncio.run(run())
