from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import asyncio
import re
import time

from .config import Config
from .models import ANSWERS, Game, Question
from .planner import Planner, PlanningError
from .search import SearchError, SearchRouter
from .store import Store


HELP = """猜角色：你心里选一个角色，让我来猜。只有开局者的回答推进本局。
猜角色 开始 / 状态 / 记录 / 来源 / 结束
每题回答：是、否、不知道、可能是、可能不是
也可用「猜角色 答 题号 是」，避免延迟消息串题。
猜角色 撤回：重新回答上一题
猜角色 修改 题号 否：修改某个答案并重新推理
猜角色 提示 具体线索 / 跳过提示：回应额外提示请求
猜角色 保持：资料冲突时确认原答案
猜角色 继续：到达问题上限后增加一段问题
猜角色 重试：推理失败后继续
猜角色 揭晓 角色名：结束并公布答案
猜出名字后请回复「猜对了」或「猜错了」。"""


def parse_answer(text: str) -> Tuple[Optional[int], str]:
    match = re.fullmatch(r"(?:答|回答)\s*(\d+)\s+(.+)", text)
    if match:
        return int(match[1]), ANSWERS.get(match[2].strip(), "")
    return None, ANSWERS.get(text.strip(), "")


class Service:
    def __init__(self, context: Any, data_dir: Path, runtime_dir: Path, config: Config) -> None:
        self.ctx = context
        self.config = config
        self.store = Store(data_dir)
        self.search = SearchRouter(config, self.store, runtime_dir)
        self.planner = Planner(context, config)
        self.tasks: Dict[str, asyncio.Task] = {}
        self.notices: Set[asyncio.Task] = set()
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        tasks = list(self.tasks.values()) + list(self.notices)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.store.close()

    async def send(self, stream: str, text: str) -> None:
        result = await self.ctx.send.text(text, stream)
        if result is False:
            raise RuntimeError("游戏消息发送失败")

    def _notice(self, stream: str, text: str) -> None:
        async def deliver() -> None:
            try:
                await self.send(stream, text)
            except Exception:
                self.ctx.logger.exception("猜角色通知发送失败")

        task = asyncio.create_task(deliver())
        self.notices.add(task)
        task.add_done_callback(self.notices.discard)

    def submit(
        self, stream: str, owner: str, text: str, message_id: str = "", timestamp: float = 0, explicit: bool = True
    ) -> bool:
        """同步认领一条输入后立即返回，阻塞事件处理器不等待模型请求。"""
        if self.closed or not stream or not owner:
            return False
        text = text.strip()
        game = self.store.get(stream)
        active = game is not None and game.phase != "finished"
        expired = game is not None and time.time() - game.updated_at > self.config.game.idle_minutes * 60
        if not explicit:
            if not self.config.game.accept_bare_answers or not active or game.owner != owner:
                return False
            if text not in ANSWERS and text not in {"猜对了", "猜错了"}:
                return False
            if time.time() - game.updated_at > self.config.game.idle_minutes * 60:
                return False
        if message_id and game and message_id in game.seen_messages:
            return True
        if active and game.owner != owner and text not in {"帮助", "状态"} and not (expired and text == "开始"):
            if explicit:
                self._notice(stream, "本局由开局者作答；其他人可以围观，发送「猜角色 状态」查看当前问题。")
            return explicit
        if stream in self.tasks:
            if explicit and text == "结束" and game and game.owner == owner:
                self.tasks[stream].cancel()
            else:
                if explicit:
                    self._notice(stream, "正在处理上一条回答，请等下一题发出后再答。")
                return True
        if (
            game
            and timestamp
            and timestamp < game.opened_at
            and (parse_answer(text)[1] or text in {"猜对了", "猜错了"})
        ):
            if explicit:
                self._notice(stream, "这条回答早于当前问题，未计入。请按当前题号重新作答。")
            return True
        if game and message_id:
            game.seen_messages = (game.seen_messages + [message_id])[-100:]
            self.store.save(game, touch=False)
        revision = game.revision if game else -1
        task = asyncio.create_task(self._run(stream, owner, text, revision, message_id))
        self.tasks[stream] = task
        return True

    async def _run(self, stream: str, owner: str, text: str, revision: int, message_id: str) -> None:
        try:
            game = self.store.get(stream)
            if game and revision != game.revision:
                return
            await self.handle(stream, owner, text, game, message_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.exception("猜角色处理失败：%s", type(exc).__name__)
            try:
                reason = str(exc) if isinstance(exc, PlanningError) else "本轮处理失败"
                await self.send(stream, reason + "。进度已保留，可发送「猜角色 状态」或「猜角色 重试」。")
            except Exception:
                self.ctx.logger.exception("猜角色错误通知发送失败")
        finally:
            if self.tasks.get(stream) is asyncio.current_task():
                self.tasks.pop(stream, None)

    async def handle(self, stream: str, owner: str, text: str, game: Optional[Game], message_id: str) -> None:
        if text in {"", "帮助"}:
            await self.send(stream, HELP)
            return
        if text == "开始":
            if (
                game
                and game.phase != "finished"
                and time.time() - game.updated_at <= self.config.game.idle_minutes * 60
            ):
                await self.send(stream, "本群已有进行中的游戏。发送「猜角色 状态」查看，或由开局者结束。")
                return
            game = Game(
                stream_id=stream,
                owner=owner,
                limit=self.config.game.question_limit,
                seen_messages=[message_id] if message_id else [],
            )
            self.store.save(game)
            await self.send(
                stream, "请在心里确定一个角色，不要告诉我名字。你可以回答：是、否、不知道、可能是、可能不是。"
            )
            await self.advance(game)
            return
        if game is None:
            await self.send(stream, "还没有游戏，发送「猜角色 开始」。")
            return
        if text == "状态":
            await self.send(stream, self.status(game))
            return
        if game.phase == "finished":
            await self.send(stream, "本局已结束。发送「猜角色 开始」开始新局。")
            return
        if text == "记录":
            history = "\n".join(f"{i}. {q.text} → {q.answer or '待回答'}" for i, q in enumerate(game.questions, 1))
            await self.send(stream, history or "还没有问题记录。")
            return
        if text == "来源":
            sources = {}
            for evidence in game.evidence:
                for source in evidence.research.sources:
                    sources[source.url] = source.title
            await self.send(
                stream,
                "\n".join(f"{title}\n{url}" for url, title in list(sources.items())[-12:]) or "本局尚无搜索来源。",
            )
            return
        if text == "结束" or text.startswith("揭晓 "):
            game.phase = "finished"
            game.outcome = text[:200]
            self.store.save(game)
            await self.send(stream, "本局结束。" + ("答案是：" + text[3:203] if text.startswith("揭晓 ") else ""))
            return
        if text == "撤回":
            answered = [i for i, question in enumerate(game.questions) if question.answer]
            if not answered:
                await self.send(stream, "还没有可以撤回的答案。")
                return
            index = answered[-1]
            game.corrections.append({"question": str(index + 1), "old": game.questions[index].answer, "new": ""})
            game.questions = game.questions[: index + 1]
            game.questions[index].answer = ""
            game.evidence.clear()
            game.kept_conflicts.clear()
            await self.present(game, "answer", game.questions[index].text)
            return
        correction = re.fullmatch(r"修改\s+(\d+)\s+(.+)", text)
        if correction:
            index, answer = int(correction[1]), ANSWERS.get(correction[2].strip(), "")
            if not answer or not 1 <= index <= len(game.questions) or not game.questions[index - 1].answer:
                await self.send(stream, "请使用「猜角色 修改 已回答的题号 是/否/不知道/可能是/可能不是」。")
                return
            game.corrections.append({"question": str(index), "old": game.questions[index - 1].answer, "new": answer})
            game.questions[index - 1].answer = answer
            game.questions = [q for q in game.questions if q.answer]
            game.evidence.clear()
            game.kept_conflicts.clear()
            game.phase = "planning"
            self.store.save(game)
            await self.advance(game)
            return
        if game.phase == "conflict" and text == "保持":
            game.kept_conflicts.append(game.conflict_index)
            game.phase = "planning"
            self.store.save(game)
            await self.advance(game)
            return
        if game.phase == "hint" and (text in {"跳过提示", "拒绝"} or text.startswith("提示 ")):
            game.hint = text[3:503] if text.startswith("提示 ") else "玩家拒绝额外提示，请只使用问答线索。"
            game.phase = "planning"
            self.store.save(game)
            await self.advance(game)
            return
        if game.phase == "limit" and text == "继续":
            game.limit += self.config.game.question_limit
            game.phase = "planning"
            self.store.save(game)
            await self.advance(game)
            return
        if text in {"重试", "继续"} and game.phase == "planning":
            await self.advance(game)
            return
        number, answer = parse_answer(text)
        if game.phase == "guess" and text in {"猜对了", "猜错了", "是", "否", "不是"}:
            if text in {"猜对了", "是"}:
                game.phase, game.outcome = "finished", "猜中：" + game.pending
                self.store.save(game)
                await self.send(stream, f"猜中了！是 {game.pending}。本局问了 {len(game.questions)} 题。")
            else:
                game.rejected.append(game.pending)
                game.phase = "planning"
                self.store.save(game)
                await self.advance(game)
            return
        if game.phase == "answer" and answer:
            if number is not None and number != len(game.questions):
                await self.send(stream, f"当前是第 {len(game.questions)} 题，这条回答未计入。")
                return
            game.questions[-1].answer = answer
            game.phase = "planning"
            self.store.save(game)
            await self.advance(game)
            return
        await self.send(stream, "这条输入不适用于当前步骤。\n" + self.status(game))

    def status(self, game: Game) -> str:
        header = f"已问 {len(game.questions)}/{game.limit} 题，已检索 {game.searches}/{self.config.search.max_requests_per_game} 次。"
        if game.phase == "answer":
            return header + f"\n第 {len(game.questions)} 题：{game.pending}\n是 / 否 / 不知道 / 可能是 / 可能不是"
        if game.phase == "guess":
            return header + f"\n我猜是：{game.pending}。请回复「猜对了」或「猜错了」。"
        if game.phase == "hint":
            return header + f"\n{game.pending}\n可发送「猜角色 提示 线索」或「猜角色 跳过提示」。"
        if game.phase == "conflict":
            return header + f"\n{game.pending}\n请「猜角色 保持」或「猜角色 修改 {game.conflict_index} 新答案」。"
        if game.phase == "limit":
            return header + "\n已到本段上限，可「猜角色 继续」或「猜角色 揭晓 角色名」。"
        if game.phase == "finished":
            return header + "\n本局已结束：" + game.outcome
        return header + "\n正在推理；若服务刚重启或上一轮失败，可发送「猜角色 重试」。"

    async def present(self, game: Game, phase: str, pending: str, prefix: str = "") -> None:
        game.phase, game.pending = phase, pending
        game.revision += 1
        game.opened_at = time.time()
        self.store.save(game)
        await self.send(game.stream_id, prefix + self.status(game))

    async def research(self, game: Game, query: str, notes: List[str]) -> bool:
        game.searches += 1
        self.store.save(game)
        await self.send(game.stream_id, "我查一下角色线索，请稍等。")
        try:
            evidence = await self.search.search(game, query)
        except SearchError as exc:
            notes.append("本次搜索失败：" + str(exc))
            await self.send(game.stream_id, "这次没能联网核实：" + str(exc) + "。我会保留已有线索。")
            return False
        game.evidence.append(evidence)
        self.store.save(game)
        notes.append("已获得新资料。核对全部玩家答案，继续决定问题；存在矛盾时请先澄清。")
        return True

    async def advance(self, game: Game) -> None:
        notes: List[str] = []
        searched = 0
        checked: Set[str] = set()
        for _ in range(self.config.search.max_requests_per_turn + 4):
            allowed = (
                searched < self.config.search.max_requests_per_turn
                and game.searches < self.config.search.max_requests_per_game
            )
            decision = await self.planner.decide(game, allowed, notes)
            if decision.action == "search":
                if not allowed:
                    notes.append("本轮不可继续搜索，请选择提问、提示、澄清或放弃。")
                    continue
                searched += 1
                await self.research(game, decision.query, notes)
                continue
            if decision.action == "guess":
                character = decision.character.strip()
                if character in game.rejected:
                    notes.append("这个角色已经被玩家否定，请换一个候选或继续提问。")
                    continue
                if character not in checked and allowed:
                    checked.add(character)
                    searched += 1
                    await self.research(
                        game, f"核实候选角色 {character} 的作品和关键特征，逐条比对已有回答，列出矛盾与来源。", notes
                    )
                    # 让模型审阅检索结果，不能把“查过”直接视为“证实”。
                    continue
                supported = any(
                    c.name == character and c.facts and c.source_urls and not c.conflicts
                    for e in game.evidence
                    for c in e.research.candidates
                )
                prefix = "" if supported else "本次资料未能充分核实，我先按已有线索尝试猜测。\n"
                game.guesses += 1
                await self.present(game, "guess", character, prefix)
                return
            if decision.action == "ask":
                if len(game.questions) >= game.limit:
                    await self.present(game, "limit", "")
                    return
                question = decision.text.strip()
                if any(q.text == question for q in game.questions):
                    notes.append("该问题已经问过，请提出不同的问题。")
                    continue
                game.questions.append(Question(text=question))
                await self.present(game, "answer", question)
                return
            if decision.action == "hint":
                if game.hint_used:
                    notes.append("本局已请求过一次提示，不能再请求。")
                    continue
                game.hint_used = True
                await self.present(game, "hint", decision.text)
                return
            if decision.action == "clarify":
                index = decision.question_index
                if not 1 <= index <= len(game.questions) or index in game.kept_conflicts:
                    notes.append("只能澄清有效且尚未由玩家再次确认的题号。")
                    continue
                game.conflict_index = index
                await self.present(game, "conflict", decision.text)
                return
            if decision.action == "give_up":
                await self.present(game, "limit", "", "目前线索不足以可靠猜出。\n")
                return
        raise PlanningError("游戏模型没有给出可执行的下一步")
