"""Bot 的分层长期记忆：写入、合并与注入。

一句话模型：记忆属于 Bot 自己（第一人称），按「与我有多相关」分两层——
direct（自己说过的话、别人 @ 我）全权重；indirect（群里的规则/结论、
别人提到我的事）在本群也全权重。Bot 离开这个群之后，indirect 不再默认
注入，只在当前话题命中关键词时低权重召回（`weight=0.4`）。

存储是双层：
- 摘要层（`bot_memories`）合并覆盖，注入上下文只读这一层；
- 全文层（`bot_memory_chunks`）只追加，用来追溯原话、做全文检索。

写入时机：
- 群聊每个 epoch 跑完后批量提炼（`remember_epoch`）——确定性规则切分，
  不额外调模型；只有摘要超过 `SUMMARY_MAX_CHARS` 才用摘要模型压缩一次。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence

from flux.core.memory.bot_memory_repository import BotMemoryRepository, MemoryChunkDraft
from flux.core.memory.secrets import ensure_no_secret
from flux.core.task_engine.conversation_summary import ConversationSummarizer
from flux.enums import BotMemoryKind, BotMemoryRelevance, BotMemorySource
from flux.errors import ValidationError
from flux.logging import get_logger
from flux.models.bot import Bot
from flux.models.bot_memory import BotMemory
from flux.models.group import ChatGroup, ChatGroupMember, ChatGroupMessage

logger = get_logger(__name__)

#: 摘要层的字符上限；超过后压缩，压缩失败退化为尾部截断。
SUMMARY_MAX_CHARS = 4_000
#: 出群后 indirect 记忆的召回权重（低于 1 表示"参考，不当作事实"）。
LOW_WEIGHT = 0.4
#: 注入上下文的记忆字符预算。
BLOCK_MAX_CHARS = 2_500
#: 单次注入最多带几条摘要。
RECALL_LIMIT = 8
#: 一个 epoch 里每个 Bot 每层最多沉淀多少条原话。
MAX_CHUNKS_PER_EPOCH = 40
#: 单条全文记忆的字符上限（群发言本身已限制在 4000，这里再收一次）。
MAX_CHUNK_CHARS = 800

#: 关键词提取：ASCII 词与连续中文串。
_WORD_RE = re.compile(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}")

#: 高频虚词，命中它们没有区分度，直接不参与召回。
_STOPWORDS = frozenset(
    {
        "我们",
        "你们",
        "他们",
        "这个",
        "那个",
        "什么",
        "怎么",
        "可以",
        "需要",
        "一下",
        "现在",
        "然后",
        "所以",
        "但是",
        "如果",
        "因为",
        "就是",
        "不是",
        "没有",
        "已经",
        "还是",
        "一个",
        "这样",
        "那样",
        "这些",
        "那些",
        "以及",
        "或者",
        "进行",
        "目前",
        "应该",
    }
)


def extract_keywords(text: str, *, limit: int = 12) -> list[str]:
    """把一句话切成召回用的关键词（中文补 2-gram，避免整句匹配不上）。"""
    words: list[str] = []
    seen: set[str] = set()

    def add(word: str) -> None:
        word = word.strip().lower()
        if len(word) < 2 or word in _STOPWORDS or word in seen:
            return
        seen.add(word)
        words.append(word)

    for match in _WORD_RE.finditer(text or ""):
        token = match.group(0)
        if token.isascii():
            add(token)
            continue
        if len(token) <= 4:
            add(token)
        for index in range(len(token) - 1):
            add(token[index : index + 2])
        if len(words) >= limit * 2:
            break
    return words[:limit]


def _clip(text: str, limit: int = MAX_CHUNK_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else f"{text[:limit]}…"


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    clipped = text[-limit:]
    newline = clipped.find("\n")
    return clipped[newline + 1 :] if 0 <= newline < 200 else clipped


def _topic(group_id: uuid.UUID) -> str:
    return f"group:{group_id}"


class BotMemoryService:
    def __init__(
        self,
        repository: BotMemoryRepository,
        *,
        summarizer: ConversationSummarizer | None = None,
    ) -> None:
        self._repo = repository
        self._summarizer = summarizer

    # --- 注入 ---

    async def render_block(
        self,
        bot: Bot,
        *,
        group_id: uuid.UUID | None,
        query: str = "",
        limit: int = RECALL_LIMIT,
    ) -> str:
        """渲染成一段可以拼进 system prompt 的记忆文本；没有记忆时返回空串。"""
        keywords = extract_keywords(query)
        rows = await self._repo.context(bot.id, group_id=group_id, keywords=keywords, limit=limit)
        if not rows:
            return ""
        # 出群后的 indirect 即便被关键词召回，也排在直接相关/本群记忆之后
        rows = sorted(rows, key=lambda row: _effective_weight(row, group_id), reverse=True)
        lines: list[str] = []
        used = 0
        for row in rows:
            content = (row.content or "").strip()
            if not content:
                continue
            line = f"- 【{_tag(row, group_id)}】{content}"
            if used + len(line) > BLOCK_MAX_CHARS:
                break
            lines.append(line)
            used += len(line)
        if not lines:
            return ""
        await self._repo.touch([row.id for row in rows])
        return (
            "\n你的长期记忆（第一人称视角，只作参考；不要在群里复述这些记忆的来源）：\n"
            + "\n".join(lines)
            + "\n"
        )

    async def search(self, bot_id: uuid.UUID, query: str, *, limit: int = 20) -> list[dict]:
        """全文检索：用于「这句话我到底是怎么说的」这类追溯。"""
        rows = await self._repo.search_chunks(bot_id, keywords=extract_keywords(query), limit=limit)
        return [row.to_dict() for row in rows]

    # --- 群聊写入 ---

    async def remember_epoch(
        self,
        group: ChatGroup,
        members: Sequence[tuple[ChatGroupMember, Bot]],
        messages: Sequence[ChatGroupMessage],
    ) -> int:
        """把一个 epoch 的消息沉淀进每个成员的 Bot 记忆，返回新增的全文条数。"""
        if not messages or not members:
            return 0
        written = 0
        for _member, bot in members:
            try:
                direct, indirect = _split(messages, bot)
                written += await self._store(
                    bot,
                    group,
                    relevance=BotMemoryRelevance.DIRECT,
                    kind=BotMemoryKind.SUMMARY,
                    drafts=direct,
                    self_authored=True,
                )
                written += await self._store(
                    bot,
                    group,
                    relevance=BotMemoryRelevance.INDIRECT,
                    kind=BotMemoryKind.DECISION,
                    drafts=indirect,
                    self_authored=False,
                )
            except Exception:  # noqa: BLE001 - 单个 Bot 记忆失败不影响其他成员
                logger.warning(
                    "写入 Bot 记忆失败 bot_id=%s group_id=%s", bot.id, group.id, exc_info=True
                )
        return written

    async def _store(
        self,
        bot: Bot,
        group: ChatGroup,
        *,
        relevance: BotMemoryRelevance,
        kind: BotMemoryKind,
        drafts: list[MemoryChunkDraft],
        self_authored: bool,
    ) -> int:
        if not drafts:
            return 0
        topic = _topic(group.id)
        summary = await self._repo.find_summary(bot.id, relevance=relevance.value, topic=topic)
        created = summary is None
        if summary is None:
            summary = await self._repo.insert(
                bot_id=bot.id,
                kind=kind.value,
                relevance=relevance.value,
                topic=topic,
                self_authored=self_authored,
                source=BotMemorySource.GROUP.value,
                source_group_id=group.id,
                source_group_name=group.name,
                weight=1.0,
            )
        added = await self._repo.append_chunks(
            summary.id,
            drafts,
            bot_id=bot.id,
            source=BotMemorySource.GROUP.value,
            source_group_id=group.id,
        )
        if added <= 0:
            return 0
        merged = _merge(summary.content, [draft.content for draft in drafts])
        if len(merged) > SUMMARY_MAX_CHARS:
            merged = await self._compress(bot, group, relevance, merged)
        await self._repo.update_summary(
            summary.id,
            content=merged,
            chunk_growth=added,
            bump_version=not created,
        )
        return added

    async def _compress(
        self,
        bot: Bot,
        group: ChatGroup,
        relevance: BotMemoryRelevance,
        content: str,
    ) -> str:
        """摘要超长时压缩；模型不可用就保留最近的内容（不丢最新事实）。"""
        if self._summarizer is None:
            return _tail(content, SUMMARY_MAX_CHARS)
        angle = (
            "你说过的话" if relevance is BotMemoryRelevance.DIRECT else "群里与你有关的规则/结论"
        )
        description = (
            f"这是 Bot「{(bot.name or '').strip() or bot.id}」在群聊「{group.name}」里的长期记忆"
            f"（{angle}），请压缩成更短的要点，保留仍然有用的信息，删掉重复与过时内容。"
        )
        try:
            result = await self._summarizer.summarize(
                description=description, messages=[("assistant", content)]
            )
            return _tail(result.text, SUMMARY_MAX_CHARS)
        except Exception:  # noqa: BLE001 - 压缩失败不影响写入
            logger.warning(
                "Bot 记忆压缩失败 bot_id=%s group_id=%s，改用尾部截断",
                bot.id,
                group.id,
                exc_info=True,
            )
            return _tail(content, SUMMARY_MAX_CHARS)

    # --- 手动管理（API）---

    async def list_memories(
        self,
        bot_id: uuid.UUID,
        *,
        relevance: str | None = None,
        group_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[BotMemory]:
        return await self._repo.list(
            bot_id, relevance=relevance, group_id=group_id, limit=limit, offset=offset
        )

    async def detail(self, memory_id: uuid.UUID, *, chunk_limit: int = 50) -> dict:
        row = await self._repo.get(memory_id)
        chunks = await self._repo.list_chunks(memory_id, limit=chunk_limit)
        return {**row.to_dict(), "chunks": [chunk.to_dict() for chunk in chunks]}

    async def add_manual(
        self,
        bot_id: uuid.UUID,
        *,
        content: str,
        kind: BotMemoryKind = BotMemoryKind.FACT,
        relevance: BotMemoryRelevance = BotMemoryRelevance.DIRECT,
    ) -> BotMemory:
        text = (content or "").strip()
        if not text:
            raise ValidationError("记忆内容不能为空", details={"where": "Bot Memory"})
        ensure_no_secret(text, where="Bot Memory")
        text = _clip(text, SUMMARY_MAX_CHARS)
        row = await self._repo.insert(
            bot_id=bot_id,
            content=text,
            kind=kind.value,
            relevance=relevance.value,
            topic=None,
            self_authored=relevance is BotMemoryRelevance.DIRECT,
            source=BotMemorySource.MANUAL.value,
            weight=1.0,
        )
        await self._repo.append_chunks(
            row.id,
            [MemoryChunkDraft(content=text)],
            bot_id=bot_id,
            source=BotMemorySource.MANUAL.value,
        )
        return row

    async def delete(self, memory_id: uuid.UUID) -> None:
        await self._repo.delete(memory_id)

    async def delete_by_group(self, bot_id: uuid.UUID, group_id: uuid.UUID) -> int:
        return await self._repo.delete_by_group(bot_id, group_id)

    async def clear(self, bot_id: uuid.UUID) -> int:
        return await self._repo.clear(bot_id)


def _effective_weight(row: BotMemory, group_id: uuid.UUID | None) -> float:
    """注入权重：直接相关与本群记忆满权重，出群后的 indirect 低权重。"""
    if row.relevance == BotMemoryRelevance.DIRECT.value:
        return 1.0
    if group_id is not None and row.source_group_id == group_id:
        return 1.0
    return LOW_WEIGHT


def _tag(row: BotMemory, group_id: uuid.UUID | None) -> str:
    if row.relevance == BotMemoryRelevance.DIRECT.value:
        return "与你直接相关"
    if group_id is not None and row.source_group_id == group_id:
        return "本群中与你有关"
    name = (row.source_group_name or "其他群").strip()
    return f"低权重召回 · {name}"


def _split(
    messages: Sequence[ChatGroupMessage], bot: Bot
) -> tuple[list[MemoryChunkDraft], list[MemoryChunkDraft]]:
    """按确定性规则把本 epoch 消息切成 direct / indirect 两份原话。

    - direct：Bot 自己的发言、别人 @ 它的发言；
    - indirect：用户发的规则/决定（除非这条正好 @ 了它）。
    """
    name = (bot.name or "").strip()
    direct: list[MemoryChunkDraft] = []
    indirect: list[MemoryChunkDraft] = []
    for message in messages:
        content = (message.content or "").strip()
        if not content:
            continue
        is_mine = message.role == "bot" and message.bot_id == bot.id
        mentioned = bool(name) and f"@{name}" in content and not is_mine
        if is_mine:
            direct.append(MemoryChunkDraft(content=_clip(content), source_message_id=message.id))
        elif mentioned:
            who = message.author_name or ("用户" if message.role == "user" else "同伴")
            direct.append(
                MemoryChunkDraft(content=_clip(f"{who}：{content}"), source_message_id=message.id)
            )
        elif message.role == "user":
            indirect.append(MemoryChunkDraft(content=_clip(content), source_message_id=message.id))
    return direct[-MAX_CHUNKS_PER_EPOCH:], indirect[-MAX_CHUNKS_PER_EPOCH:]


def _merge(previous: str, lines: Sequence[str]) -> str:
    """把新原话追加成要点列表，重复行不再写第二遍。"""
    merged = (previous or "").strip()
    seen = {line.strip() for line in merged.splitlines() if line.strip()}
    for content in lines:
        text = f"- {(content or '').strip()}"
        if text == "-" or text in seen:
            continue
        seen.add(text)
        merged = f"{merged}\n{text}".strip()
    return merged


__all__ = [
    "BLOCK_MAX_CHARS",
    "LOW_WEIGHT",
    "SUMMARY_MAX_CHARS",
    "BotMemoryService",
    "extract_keywords",
]
