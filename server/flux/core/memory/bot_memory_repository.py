"""Bot 分层记忆的持久化读写。

- 摘要层 `bot_memories`：按 `topic` 合并覆盖，`version` 记录合并次数；
- 全文层 `bot_memory_chunks`：只追加，`source_message_id` 用于同一轮重试去重。

会话边界与 `flux.core.group.repository` 一致：一次调用一个会话、一次提交。
检索就用 SQLite 也能跑的 `LIKE`：记忆条数按「一个 Bot 的长期沉淀」量级，
不需要引入向量库；关键词命中数在 Python 侧排序。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, case, delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.errors import BadRequestError, NotFoundError
from flux.logging import get_logger
from flux.models.bot_memory import BotMemory, BotMemoryChunk

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class MemoryChunkDraft:
    """待追加的一条全文记忆。"""

    content: str
    source_message_id: uuid.UUID | None = None


def _like(keyword: str) -> str:
    return f"%{keyword}%"


class BotMemoryRepository:
    """`bot_memories` / `bot_memory_chunks` 的读写入口。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # --- 摘要层 ---

    async def insert(
        self,
        *,
        bot_id: uuid.UUID,
        content: str = "",
        kind: str = "summary",
        relevance: str = "direct",
        topic: str | None = None,
        self_authored: bool = False,
        source: str = "group",
        source_group_id: uuid.UUID | None = None,
        source_group_name: str | None = None,
        weight: float = 1.0,
    ) -> BotMemory:
        now = _utcnow()
        row = BotMemory(
            id=uuid.uuid4(),
            bot_id=bot_id,
            content=content,
            kind=kind,
            relevance=relevance,
            topic=topic,
            self_authored=self_authored,
            source=source,
            source_group_id=source_group_id,
            source_group_name=(source_group_name or "")[:128] or None,
            weight=weight,
            version=1,
            chunk_count=0,
            created_at=now,
            updated_at=now,
        )
        async with self._session_factory() as session:
            session.add(row)
            await self._commit(session)
            await session.refresh(row)
            return row

    async def get(self, memory_id: uuid.UUID) -> BotMemory:
        async with self._session_factory() as session:
            row = await session.get(BotMemory, memory_id)
            if row is None:
                raise NotFoundError(
                    f"Bot 记忆 {memory_id} 不存在", details={"memory_id": str(memory_id)}
                )
            return row

    async def find_summary(
        self, bot_id: uuid.UUID, *, relevance: str, topic: str
    ) -> BotMemory | None:
        """按合并键找摘要：同 Bot + 同相关性 + 同 topic。"""
        async with self._session_factory() as session:
            return await session.scalar(
                select(BotMemory).where(
                    BotMemory.bot_id == bot_id,
                    BotMemory.relevance == relevance,
                    BotMemory.topic == topic,
                )
            )

    async def update_summary(
        self,
        memory_id: uuid.UUID,
        *,
        content: str,
        chunk_growth: int = 0,
        bump_version: bool = True,
        weight: float | None = None,
        self_authored: bool | None = None,
    ) -> BotMemory:
        async with self._session_factory() as session:
            row = await session.get(BotMemory, memory_id)
            if row is None:
                raise NotFoundError(
                    f"Bot 记忆 {memory_id} 不存在", details={"memory_id": str(memory_id)}
                )
            row.content = content
            if chunk_growth:
                row.chunk_count += chunk_growth
                # 首次落内容不算「合并」：version 从 1 开始，之后每合并一次 +1
                if bump_version:
                    row.version += 1
            if weight is not None:
                row.weight = weight
            if self_authored is not None:
                row.self_authored = self_authored
            row.updated_at = _utcnow()
            await self._commit(session)
            await session.refresh(row)
            return row

    async def list(
        self,
        bot_id: uuid.UUID,
        *,
        relevance: str | None = None,
        group_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[BotMemory]:
        statement = select(BotMemory).where(BotMemory.bot_id == bot_id)
        if relevance is not None:
            statement = statement.where(BotMemory.relevance == relevance)
        if group_id is not None:
            statement = statement.where(BotMemory.source_group_id == group_id)
        async with self._session_factory() as session:
            rows = await session.scalars(
                statement.order_by(BotMemory.updated_at.desc(), BotMemory.id.desc())
                .limit(limit)
                .offset(offset)
            )
            return list(rows)

    async def context(
        self,
        bot_id: uuid.UUID,
        *,
        group_id: uuid.UUID | None,
        keywords: Sequence[str],
        limit: int = 8,
    ) -> list[BotMemory]:
        """自动注入用的记忆集合。

        - direct：无条件全权重；
        - 同群 indirect：无条件全权重；
        - 其他群的 indirect：只有关键词命中才召回（出群后的低权重召回）。
        """
        likes = (
            or_(*[BotMemory.content.like(_like(word)) for word in keywords]) if keywords else None
        )
        branches = [BotMemory.relevance == "direct"]
        if group_id is not None:
            branches.append(
                and_(
                    BotMemory.relevance == "indirect",
                    BotMemory.source_group_id == group_id,
                )
            )
            else_where = or_(
                BotMemory.source_group_id.is_(None),
                BotMemory.source_group_id != group_id,
            )
        else:
            else_where = BotMemory.relevance == "indirect"
        if likes is not None:
            branches.append(and_(BotMemory.relevance == "indirect", else_where, likes))
        # 先 direct、再按权重与新鲜度排序，最后按上限裁剪
        direct_first = case((BotMemory.relevance == "direct", 0), else_=1)
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(BotMemory)
                .where(BotMemory.bot_id == bot_id, or_(*branches))
                .order_by(
                    direct_first,
                    BotMemory.weight.desc(),
                    BotMemory.updated_at.desc(),
                )
                .limit(limit)
            )
            return list(rows)

    async def touch(self, memory_ids: Sequence[uuid.UUID]) -> None:
        if not memory_ids:
            return
        async with self._session_factory() as session:
            await session.execute(
                update(BotMemory)
                .where(BotMemory.id.in_(list(memory_ids)))
                .values(last_used_at=_utcnow())
            )
            await session.commit()

    # --- 全文层 ---

    async def append_chunks(
        self,
        memory_id: uuid.UUID,
        drafts: Sequence[MemoryChunkDraft],
        *,
        bot_id: uuid.UUID,
        source: str = "group",
        source_group_id: uuid.UUID | None = None,
    ) -> int:
        """追加全文，返回真正新增的条数（按 `source_message_id` 去重）。"""
        items = [draft for draft in drafts if (draft.content or "").strip()]
        if not items:
            return 0
        async with self._session_factory() as session:
            message_ids = [
                draft.source_message_id for draft in items if draft.source_message_id is not None
            ]
            seen: set[uuid.UUID] = set()
            if message_ids:
                seen = set(
                    await session.scalars(
                        select(BotMemoryChunk.source_message_id).where(
                            BotMemoryChunk.memory_id == memory_id,
                            BotMemoryChunk.source_message_id.in_(message_ids),
                        )
                    )
                )
            added = 0
            now = _utcnow()
            for draft in items:
                if draft.source_message_id is not None and draft.source_message_id in seen:
                    continue
                session.add(
                    BotMemoryChunk(
                        id=uuid.uuid4(),
                        memory_id=memory_id,
                        bot_id=bot_id,
                        source=source,
                        source_group_id=source_group_id,
                        source_message_id=draft.source_message_id,
                        content=draft.content.strip(),
                        created_at=now,
                        updated_at=now,
                    )
                )
                if draft.source_message_id is not None:
                    seen.add(draft.source_message_id)
                added += 1
            if added == 0:
                return 0
            await self._commit(session)
            return added

    async def list_chunks(self, memory_id: uuid.UUID, *, limit: int = 50) -> list[BotMemoryChunk]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(BotMemoryChunk)
                .where(BotMemoryChunk.memory_id == memory_id)
                .order_by(BotMemoryChunk.created_at.asc(), BotMemoryChunk.id.asc())
                .limit(limit)
            )
            return list(rows)

    async def search_chunks(
        self,
        bot_id: uuid.UUID,
        *,
        keywords: Sequence[str],
        limit: int = 20,
    ) -> list[BotMemoryChunk]:
        """全文检索：任意关键词命中即返回，按命中数从多到少。"""
        if not keywords:
            return []
        likes = [_like(word) for word in keywords]
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(BotMemoryChunk)
                .where(
                    BotMemoryChunk.bot_id == bot_id,
                    or_(*[BotMemoryChunk.content.like(item) for item in likes]),
                )
                .order_by(BotMemoryChunk.created_at.desc())
                .limit(limit * 3)
            )
            found = list(rows)
        ranked = sorted(
            found,
            key=lambda row: (
                sum(1 for word in keywords if word in (row.content or "")),
                row.created_at,
            ),
            reverse=True,
        )
        return ranked[:limit]

    # --- 删除 ---

    async def delete(self, memory_id: uuid.UUID) -> None:
        async with self._session_factory() as session:
            result = await session.execute(delete(BotMemory).where(BotMemory.id == memory_id))
            if result.rowcount == 0:
                await session.rollback()
                raise NotFoundError(
                    f"Bot 记忆 {memory_id} 不存在", details={"memory_id": str(memory_id)}
                )
            await session.commit()

    async def delete_by_group(self, bot_id: uuid.UUID, group_id: uuid.UUID) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(BotMemory).where(
                    BotMemory.bot_id == bot_id,
                    BotMemory.source_group_id == group_id,
                )
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def clear(self, bot_id: uuid.UUID) -> int:
        async with self._session_factory() as session:
            result = await session.execute(delete(BotMemory).where(BotMemory.bot_id == bot_id))
            await session.commit()
            return int(result.rowcount or 0)

    # --- 内部 ---

    async def _commit(self, session: AsyncSession) -> None:
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            logger.warning("Bot 记忆写入违反数据库约束：%s", exc.orig)
            raise BadRequestError(f"Bot 记忆写入违反数据库约束：{exc.orig}") from exc
