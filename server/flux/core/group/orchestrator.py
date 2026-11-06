"""内部群聊编排器：一次用户消息 → 有限轮讨论。

机制（与 Grok Bot / DS Bot 实测行为对齐）：
- 有群主（role=admin）时按「发言权」推进：用户点名的人先回，没点名就由群主先判断；
  发言里 `@某成员` 把发言权交给对方，最多 `max_rounds` 波（结构性闸门，不是预算）；
- 没有群主时保持旧行为：每轮给全员一次机会，全员沉默即结束；
- 每个 bot 都可以选择沉默（JSON 的 `speak: false`，或直接回 `[PASS]`）；
- 与最近群消息归一化后重复的内容直接丢弃，不复述、不自嗨；
- 用户发新消息会把当前 epoch 置 interrupted，编排器在每个发言者前检查状态立刻收手。

记忆分三层，注入各自的发言上下文：
- bot 私有：它自己在本群的发言笔记（`chat_group_member_states`，别的成员看不到）；
- 群共享：本群讨论压缩出的记忆（`chat_group_memories`，scope=group）；
- 团队共享：跨群沉淀的记忆（scope=team，group_id 为空，所有群可见）。

上下文边界：每个 bot 能看到群聊记录 + 它自己的私聊记录（仅它自己可见，不注入群），
群内看不到任何其他 bot 的私聊内容；工具调用过程不进群，只有最终发言进群。
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from flux.core.group.repository import GroupMessageDraft, GroupRepository
from flux.core.key_vault.service import KeyVaultService
from flux.core.memory.bot_memory_service import BotMemoryService
from flux.core.model_gateway.base import ChatMessage, ChatResult
from flux.core.model_gateway.providers.registry import build_channel_provider
from flux.core.model_gateway.router import ModelRouter
from flux.core.task_engine.conversation_summary import ConversationSummarizer
from flux.core.task_engine.repository import TaskRepository
from flux.logging import get_logger
from flux.models.bot import Bot
from flux.models.group import ChatGroup, ChatGroupMember, ChatGroupMessage
from flux.models.task import TaskMessage

logger = get_logger(__name__)

#: 单次模型调用的超时（秒）。超时视为本次沉默，不影响其他人发言。
SPEAK_TIMEOUT_SECONDS = 90
#: 拼装群聊上下文时最多取多少条最近消息（还会受字符预算限制）。
TRANSCRIPT_LIMIT = 80
#: 群聊上下文的字符预算：超出后从最旧的消息开始丢弃。
TRANSCRIPT_CHAR_BUDGET = 12_000
#: 拼装 bot 私聊上下文时取多少条最近消息。
PRIVATE_HISTORY_LIMIT = 10
#: 单条群发言的最大长度（超出截断，避免一个 bot 刷屏）。
MAX_REPLY_CHARS = 4000
#: 未压缩的群消息累计到多少字符才生成一次群记忆（太短不值得调模型）。
MEMORY_TRIGGER_CHARS = 4_000
#: 一次压缩最多读多少条群消息。
MEMORY_TRANSCRIPT_LIMIT = 200
#: 兜底摘录（模型不可用时）保留的字符数。
MEMORY_EXCERPT_CHARS = 1_200
#: 团队记忆的字符上限（跨群累计，只保留最近的沉淀）。
TEAM_MEMORY_MAX_CHARS = 3_000

#: 结构化输出协议：模型没说"要说"时一律视为沉默。
SYSTEM_PROMPT = """你是「{bot_name}」，正在一个内部协作群聊「{group_name}」中。
{description_line}{project_line}{role_line}
群成员：
{roster}
{memory_block}{bot_memory_block}{notes_block}
规则：
1. 你看到的是群聊记录；另外会给你自己与用户的私聊摘要——它只属于你，不要在群里复述其中细节。
2. 只在你有新的、能推进当前话题的信息时发言；不要重复别人刚说过的话，不必为发言而谋。
3. 用户点名（@）你时优先回应；但如果你确实没有可补充的内容，也可以保持沉默。
4. 需要同伴接手时，在发言里用 `@成员名` 把话题交给对方；不要无故 @ 所有人。
5. 不寒暄、不自报家门、不复述群聊、不总结别人说过的话。
6. 不要输出工具调用过程，只输出最终要对群里说的话。

只输出一个 JSON 对象（不要用 markdown 代码块包裹）：
- 不发言：{{"speak": false}}（或直接回复 [PASS]）
- 发言：{{"speak": true, "reply": "要发到群里的话（中文，简洁）"}}
"""


@dataclass(frozen=True)
class _Decision:
    speak: bool
    reply: str


class GroupOrchestrator:
    def __init__(
        self,
        repo: GroupRepository,
        tasks: TaskRepository,
        router: ModelRouter,
        *,
        key_vault: KeyVaultService | None = None,
        summarizer: ConversationSummarizer | None = None,
        bot_memories: BotMemoryService | None = None,
    ) -> None:
        self._repo = repo
        self._tasks = tasks
        self._router = router
        self._key_vault = key_vault
        self._summarizer = summarizer
        self._bot_memories = bot_memories

    async def run_epoch(
        self,
        group_id: uuid.UUID,
        epoch_id: uuid.UUID,
        *,
        mentioned: set[uuid.UUID] | None = None,
    ) -> None:
        """把一个 epoch 跑完。任何异常都只终止本次 epoch，不让后台任务悬空。"""
        try:
            await self._run(group_id, epoch_id, mentioned or set())
        except Exception:  # noqa: BLE001 - 后台任务不能把异常抛给事件循环
            logger.exception("群聊 epoch 执行失败 group_id=%s epoch_id=%s", group_id, epoch_id)
            await self._repo.set_epoch_status(epoch_id, "done", finished=True)

    async def _run(
        self, group_id: uuid.UUID, epoch_id: uuid.UUID, mentioned: set[uuid.UUID]
    ) -> None:
        group = await self._repo.get(group_id)
        epoch = await self._repo.get_epoch(epoch_id)
        if epoch is None:
            return
        members = await self._repo.list_members(group_id)
        if not members:
            await self._repo.set_epoch_status(epoch_id, "done", rounds=0, finished=True)
            return

        max_rounds = max(1, min(int(epoch.max_rounds or group.max_rounds or 2), 5))
        memory_block = await self._memory_block(group)
        admin = next((bot for member, bot in members if member.role == "admin"), None)

        if admin is None:
            rounds = await self._run_legacy(
                group, epoch_id, members, mentioned, memory_block, max_rounds
            )
        else:
            rounds = await self._run_waves(
                group, epoch_id, members, mentioned, memory_block, admin, max_rounds
            )
        if rounds is None:
            # epoch 被用户打断：不再写记忆，把状态留给 interrupt 一侧
            return

        await self._repo.set_epoch_status(epoch_id, "done", rounds=rounds, finished=True)
        await self._refresh_memory(group)
        await self._remember_bots(group, epoch_id, members)

    async def _run_legacy(
        self,
        group: ChatGroup,
        epoch_id: uuid.UUID,
        members: list[tuple[ChatGroupMember, Bot]],
        mentioned: set[uuid.UUID],
        memory_block: str,
        max_rounds: int,
    ) -> int | None:
        """没有群主：每轮给全员一次机会，全员沉默即结束（旧行为）。"""
        ordered = sorted(members, key=lambda item: 0 if item[1].id in mentioned else 1)
        rounds = 0
        for _round in range(max_rounds):
            if not await self._alive(epoch_id):
                return None
            spoke = False
            for _member, bot in ordered:
                if not await self._alive(epoch_id):
                    return None
                reply = await self._ask(
                    bot,
                    group,
                    members,
                    memory_block=memory_block,
                    addressed=bot.id in mentioned,
                    user_mentioned=bot.id in mentioned,
                    round_no=rounds + 1,
                )
                if reply is None:
                    continue
                await self._publish_reply(group, epoch_id, bot, reply, rounds + 1)
                spoke = True
            rounds += 1
            if not spoke:
                break
        return rounds

    async def _run_waves(
        self,
        group: ChatGroup,
        epoch_id: uuid.UUID,
        members: list[tuple[ChatGroupMember, Bot]],
        mentioned: set[uuid.UUID],
        memory_block: str,
        admin: Bot,
        max_rounds: int,
    ) -> int | None:
        """有群主：按发言权推进，`@成员` 把发言权交给下一个/下一波。"""
        by_id = {bot.id: bot for _member, bot in members}
        if mentioned:
            # 保持入群顺序，避免 set 迭代顺序让发言权随机漂移
            wave = [bot for _member, bot in members if bot.id in mentioned]
            if admin.id not in {bot.id for bot in wave}:
                wave.append(admin)
        else:
            wave = [admin]

        spoken: set[uuid.UUID] = set()
        rounds = 0
        while wave and rounds < max_rounds:
            if not await self._alive(epoch_id):
                return None
            rounds += 1
            spoke = False
            next_ids: list[uuid.UUID] = []
            for bot in wave:
                if not await self._alive(epoch_id):
                    return None
                reply = await self._ask(
                    bot,
                    group,
                    members,
                    memory_block=memory_block,
                    addressed=rounds > 1,
                    user_mentioned=bot.id in mentioned,
                    round_no=rounds,
                )
                spoken.add(bot.id)
                if reply is None:
                    continue
                await self._publish_reply(group, epoch_id, bot, reply, rounds)
                spoke = True
                for target in _detect_mentions(reply, members):
                    if target not in spoken and target not in next_ids:
                        next_ids.append(target)
            if not spoke or not next_ids:
                break
            wave = [by_id[item] for item in next_ids if item in by_id]
        return rounds

    async def _publish_reply(
        self,
        group: ChatGroup,
        epoch_id: uuid.UUID,
        bot: Bot,
        reply: str,
        round_no: int,
    ) -> None:
        created = await self._repo.add_messages(
            group.id,
            [
                GroupMessageDraft(
                    role="bot",
                    content=reply,
                    author_name=_bot_name(bot),
                    bot_id=bot.id,
                    epoch_id=epoch_id,
                    payload={"round": round_no},
                )
            ],
        )
        if created:
            await self._repo.record_member_turn(
                group.id,
                bot.id,
                seq=created[0].seq,
                note=f"[第{round_no}轮] {reply[:200]}",
            )

    async def _alive(self, epoch_id: uuid.UUID) -> bool:
        epoch = await self._repo.get_epoch(epoch_id)
        return epoch is not None and epoch.status == "running"

    async def _ask(
        self,
        bot: Bot,
        group: ChatGroup,
        members: list[tuple[ChatGroupMember, Bot]],
        *,
        memory_block: str,
        addressed: bool,
        user_mentioned: bool,
        round_no: int,
    ) -> str | None:
        """让一个 bot 决定是否发言；返回要发到群里的文本，或 None 表示沉默。"""
        try:
            transcript = await self._repo.recent_messages(group.id, limit=TRANSCRIPT_LIMIT)
            private = await self._private_history(bot)
            state = await self._repo.get_member_state(group.id, bot.id)
        except Exception:  # noqa: BLE001 - 单个 bot 读上下文失败只影响它自己
            logger.warning("拼装群聊上下文失败 bot_id=%s", bot.id, exc_info=True)
            return None

        roster = "\n".join(
            f"- {_bot_name(member_bot)}"
            + ("（群主）" if member.role == "admin" else "")
            + ("（你）" if member_bot.id == bot.id else "")
            for member, member_bot in members
        )
        description = (bot.description or "").strip()
        description_line = f"你的定位：{description}\n" if description else ""
        project_line = ""
        if group.project_id is not None:
            project_line = "本群关联了一个项目，讨论时以项目实际情况为准。\n"
        is_admin = any(
            member.role == "admin" and member_bot.id == bot.id for member, member_bot in members
        )
        role_line = (
            "你在本群是群主：用户没有点名别人时，由你先判断要不要回应；"
            "需要同伴接手时用 @成员名 把话题交出去。\n"
            if is_admin
            else ""
        )
        notes_block = ""
        if state is not None and (state.memory or "").strip():
            notes_block = f"\n你在本群的历史笔记（只有你能看到）：\n{state.memory.strip()}\n"
        bot_memory_block = await self._bot_memory_block(bot, group, transcript)

        system = SYSTEM_PROMPT.format(
            bot_name=_bot_name(bot),
            group_name=group.name,
            description_line=description_line,
            project_line=project_line,
            role_line=role_line,
            roster=roster or "- （暂无）",
            memory_block=memory_block,
            bot_memory_block=bot_memory_block,
            notes_block=notes_block,
        )

        messages = [ChatMessage(role="system", content=system)]
        if private:
            lines: list[str] = []
            for message in private:
                text = (message.content or "").strip()
                if not text or message.kind != "text":
                    continue
                who = "用户" if message.role == "user" else "你"
                lines.append(f"{who}：{text}")
            if lines:
                messages.append(
                    ChatMessage(
                        role="user",
                        content="（这是你与用户的私聊记录，只有你能看到）\n" + "\n".join(lines),
                    )
                )
                messages.append(ChatMessage(role="assistant", content="收到，我记得这些私聊内容。"))

        transcript_text = _render_transcript(transcript)
        if transcript_text:
            messages.append(ChatMessage(role="user", content=transcript_text))
        else:
            messages.append(ChatMessage(role="user", content="（群里还没有消息）"))

        if user_mentioned:
            messages.append(ChatMessage(role="user", content="用户刚才点名了你。"))
        elif addressed:
            messages.append(ChatMessage(role="user", content="刚才有同伴把话题交给了你。"))
        messages.append(
            ChatMessage(
                role="user",
                content=f"现在是第 {round_no} 轮，轮到你：说，还是不说（不说就回 [PASS]）？",
            )
        )

        try:
            result = await asyncio.wait_for(
                self._chat(bot, messages), timeout=SPEAK_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.info("群聊发言超时 bot_id=%s", bot.id)
            return None
        except Exception:  # noqa: BLE001 - 模型通道故障按沉默处理
            logger.warning("群聊发言调用失败 bot_id=%s", bot.id, exc_info=True)
            return None

        decision = _parse_decision(result.content)
        if not decision.speak:
            return None
        reply = decision.reply.strip()[:MAX_REPLY_CHARS]
        if not reply:
            return None
        if await self._is_duplicate(group.id, reply):
            logger.info("群聊发言重复，已丢弃 bot_id=%s", bot.id)
            return None
        return reply

    async def _private_history(self, bot: Bot) -> list[TaskMessage]:
        """该 Bot 最近一次开工的私聊记录（没有开工过则为空）。"""
        tasks = await self._tasks.list(bot_id=bot.id, limit=1)
        if not tasks:
            return []
        messages, _ = await self._tasks.list_messages(tasks[0].id, limit=PRIVATE_HISTORY_LIMIT)
        return messages

    async def _chat(self, bot: Bot, messages: list[ChatMessage]) -> ChatResult:
        """按 bot 自己的模型发言：有独立密钥时直连该通道，否则走平台路由器。"""
        if self._key_vault is not None and bot.api_key_id is not None:
            channel = await self._key_vault.channel_for_bot(bot)
            if channel is not None and (bot.model or "").strip():
                provider = build_channel_provider(channel, model=(bot.model or "").strip())
                return await provider.chat(messages)
        kwargs: dict[str, Any] = {}
        if (bot.model or "").strip():
            kwargs["model"] = (bot.model or "").strip()
        return await self._router.chat(messages, **kwargs)

    async def _is_duplicate(self, group_id: uuid.UUID, reply: str) -> bool:
        needle = _normalize(reply)
        if not needle:
            return True
        recent = await self._repo.recent_messages(group_id, limit=10)
        return any(_normalize(message.content) == needle for message in recent)

    # --- 记忆 ---

    async def _memory_block(self, group: ChatGroup) -> str:
        blocks: list[str] = []
        try:
            group_memory = await self._repo.get_memory(group.id, scope="group")
            team_memory = await self._repo.get_memory(None, scope="team")
        except Exception:  # noqa: BLE001 - 记忆读失败不能挡住正常讨论
            logger.warning("读取群记忆失败 group_id=%s", group.id, exc_info=True)
            return ""
        if group_memory is not None and group_memory.content.strip():
            blocks.append(f"本群共享记忆：\n{group_memory.content.strip()}")
        if team_memory is not None and team_memory.content.strip():
            blocks.append(f"团队共享记忆（跨群可见）：\n{team_memory.content.strip()}")
        return ("\n" + "\n\n".join(blocks) + "\n") if blocks else ""

    async def _bot_memory_block(
        self, bot: Bot, group: ChatGroup, transcript: list[ChatGroupMessage]
    ) -> str:
        """该 Bot 自己的长期记忆（第一人称）；按最近一条用户发言做关键词召回。"""
        if self._bot_memories is None:
            return ""
        query = ""
        for message in reversed(transcript):
            if message.role == "user" and (message.content or "").strip():
                query = message.content
                break
        try:
            return await self._bot_memories.render_block(bot, group_id=group.id, query=query)
        except Exception:  # noqa: BLE001 - 记忆读失败不能挡住正常发言
            logger.warning("读取 Bot 记忆失败 bot_id=%s", bot.id, exc_info=True)
            return ""

    async def _remember_bots(
        self,
        group: ChatGroup,
        epoch_id: uuid.UUID,
        members: list[tuple[ChatGroupMember, Bot]],
    ) -> None:
        """把本 epoch 的消息沉淀进每个成员自己的长期记忆。"""
        if self._bot_memories is None:
            return
        try:
            messages = await self._repo.messages_for_epoch(group.id, epoch_id)
            await self._bot_memories.remember_epoch(group, members, messages)
        except Exception:  # noqa: BLE001 - 记忆写入失败不影响已完成的讨论
            logger.warning("Bot 记忆沉淀失败 group_id=%s", group.id, exc_info=True)

    async def _refresh_memory(self, group: ChatGroup) -> None:
        """把尚未压缩的群消息压成群记忆，并沉淀一条团队记忆。"""
        try:
            latest = await self._repo.latest_seq(group.id)
            existing = await self._repo.get_memory(group.id, scope="group")
            from_seq = existing.up_to_seq if existing is not None else 0
            if latest <= from_seq:
                return
            messages, _ = await self._repo.list_messages(
                group.id, after_seq=from_seq, limit=MEMORY_TRANSCRIPT_LIMIT
            )
            pending = sum(len(message.content or "") for message in messages)
            if pending < MEMORY_TRIGGER_CHARS:
                return
            pairs: list[tuple[str, str]] = []
            for message in messages:
                text = (message.content or "").strip()
                if not text:
                    continue
                who = message.author_name or ("用户" if message.role == "user" else "系统")
                pairs.append(("user" if message.role == "user" else "assistant", f"{who}：{text}"))
            if not pairs:
                return
            summary = await self._summarize_memory(
                group, existing.content if existing is not None else "", pairs
            )
            up_to_seq = messages[-1].seq
            await self._repo.upsert_memory(
                group.id, scope="group", content=summary, up_to_seq=up_to_seq
            )
            await self._merge_team_memory(group, summary)
        except Exception:  # noqa: BLE001 - 记忆压缩失败不影响已完成的消息
            logger.warning("群记忆压缩失败 group_id=%s", group.id, exc_info=True)

    async def _summarize_memory(
        self,
        group: ChatGroup,
        previous: str,
        pairs: list[tuple[str, str]],
    ) -> str:
        description = (
            f"这是内部协作群聊「{group.name}」的讨论记录，请压缩成可复用的群记忆。\n"
            f"已有群记忆：\n{previous.strip() or '无'}"
        )
        if self._summarizer is not None:
            try:
                result = await self._summarizer.summarize(description=description, messages=pairs)
                return result.text
            except Exception:  # noqa: BLE001 - 模型不可用时退化为确定性摘录
                logger.warning("群记忆摘要失败，改用摘录 group_id=%s", group.id, exc_info=True)
        return _excerpt(pairs, MEMORY_EXCERPT_CHARS)

    async def _merge_team_memory(self, group: ChatGroup, summary: str) -> None:
        """团队记忆 = 各群最近一次沉淀的拼接（确定性合并，不再调模型）。"""
        team = await self._repo.get_memory(None, scope="team")
        merged = (
            f"{team.content.strip()}\n【{group.name}】{summary.strip()}"
            if team
            else (f"【{group.name}】{summary.strip()}")
        )
        merged = merged.strip()
        if len(merged) > TEAM_MEMORY_MAX_CHARS:
            merged = merged[-TEAM_MEMORY_MAX_CHARS:]
            newline = merged.find("\n")
            if 0 <= newline < 200:
                merged = merged[newline + 1 :]
        await self._repo.upsert_memory(None, scope="team", content=merged)


def _bot_name(bot: Bot) -> str:
    name = (bot.name or "").strip()
    return name or "未命名 Bot"


def _render_transcript(messages: list[ChatGroupMessage]) -> str:
    """从新到旧按字符预算收消息，更早的丢弃并标注。"""
    lines: list[str] = []
    used = 0
    truncated = False
    for message in reversed(messages):
        who = message.author_name or ("用户" if message.role == "user" else "系统")
        line = f"[{who}] {message.content}"
        if used + len(line) > TRANSCRIPT_CHAR_BUDGET:
            truncated = True
            break
        lines.append(line)
        used += len(line)
    lines.reverse()
    if not lines:
        return ""
    prefix = "（群聊记录，按时间顺序）"
    if truncated:
        prefix += "\n（更早的消息因长度限制已省略）"
    return prefix + "\n" + "\n".join(lines)


def _detect_mentions(content: str, members: list[tuple[ChatGroupMember, Bot]]) -> set[uuid.UUID]:
    """按 bot 名字匹配 `@名字`。"""
    mentioned: set[uuid.UUID] = set()
    for _member, bot in members:
        name = _bot_name(bot)
        if name and f"@{name}" in content:
            mentioned.add(bot.id)
    return mentioned


def _excerpt(pairs: list[tuple[str, str]], limit: int) -> str:
    """模型不可用时的兜底：保留最近 limit 个字符的原话。"""
    text = "\n".join(line for _role, line in pairs).strip()
    return text[-limit:].strip()


def _parse_decision(raw: str) -> _Decision:
    """解析结构化输出；解析不了时按纯文本发言处理（模型说人话就当作要说）。"""
    text = (raw or "").strip()
    payload = _load_json_object(text)
    if payload is not None:
        speak = payload.get("speak")
        reply = payload.get("reply")
        if speak is False:
            return _Decision(speak=False, reply="")
        if isinstance(reply, str) and reply.strip():
            return _Decision(speak=True, reply=reply)
        if isinstance(speak, bool):
            return _Decision(speak=False, reply="")
        return _Decision(speak=False, reply="")
    upper = text.upper()
    if not text or upper in _SILENT_TOKENS:
        return _Decision(speak=False, reply="")
    return _Decision(speak=True, reply=text)


#: 明确的「不发言」标记（`[PASS]` 与 `SILENT` 等价）。
_SILENT_TOKENS = frozenset(
    {"SILENT", "[SILENT]", "(SILENT)", "PASS", "[PASS]", "(PASS)", "无", "沉默", "跳过"}
)


def _load_json_object(text: str) -> dict[str, Any] | None:
    candidate = text
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        first_line, _, rest = candidate.partition("\n")
        candidate = rest if first_line.strip().lower() in {"json", "json5"} else candidate
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        loaded = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’\-—…~`（）()\[\]【】]+")


def _normalize(text: str) -> str:
    return _PUNCT_RE.sub("", _SPACE_RE.sub("", text or "")).lower()
