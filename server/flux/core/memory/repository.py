"""User / Environment 记忆的持久化读写（批次② §4.2）。

只做纯 CRUD，不判断过期与容量：TTL 过滤与容量裁剪属于服务层的策略
（`flux.core.memory.service`），仓储层保持"库里有几条就返回几条"。

写入时显式给 `created_at` / `updated_at` 填 Python 端 UTC 时间：SQLite 的
`CURRENT_TIMESTAMP` 只有秒精度，同一秒内连写多条时"谁更新"无法排序，
容量裁剪会变成随机淘汰。微秒级时间戳让"保留最新"在任何库上都确定。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.enums import MemoryLayer
from flux.errors import BadRequestError, NotFoundError
from flux.logging import get_logger
from flux.models.memory import MemoryEntry

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MemoryRepository:
    """`memories` 表的读写入口。每次调用自开一个会话并提交。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def insert(
        self,
        *,
        layer: MemoryLayer,
        content: str,
        source: str = "",
        key: str | None = None,
        meta: dict | None = None,
        expires_at: datetime | None = None,
    ) -> MemoryEntry:
        now = _utcnow()
        entry = MemoryEntry(
            id=uuid.uuid4(),
            layer=layer.value,
            key=key,
            content=content,
            source=source,
            meta=meta or {},
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        async with self._session_factory() as session:
            session.add(entry)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                logger.warning("记忆写入违反数据库约束：%s", exc.orig)
                raise BadRequestError(f"记忆写入违反数据库约束：{exc.orig}") from exc
            await session.refresh(entry)
            return entry

    async def get(self, entry_id: str | uuid.UUID) -> MemoryEntry:
        async with self._session_factory() as session:
            entry = await session.get(MemoryEntry, _as_uuid(entry_id))
            if entry is None:
                raise NotFoundError(f"记忆 {entry_id} 不存在", details={"memory_id": str(entry_id)})
            return entry

    async def get_by_key(self, layer: MemoryLayer, key: str) -> MemoryEntry | None:
        statement = select(MemoryEntry).where(
            MemoryEntry.layer == layer.value, MemoryEntry.key == key
        )
        async with self._session_factory() as session:
            return await session.scalar(statement)

    async def update_content(self, entry_id: str | uuid.UUID, *, content: str) -> MemoryEntry:
        """刷新一条平台条目的内容（Environment 种子随平台版本升级）。"""
        async with self._session_factory() as session:
            entry = await session.get(MemoryEntry, _as_uuid(entry_id))
            if entry is None:
                raise NotFoundError(f"记忆 {entry_id} 不存在", details={"memory_id": str(entry_id)})
            entry.content = content
            entry.updated_at = _utcnow()
            await session.commit()
            await session.refresh(entry)
            return entry

    async def update_meta(self, entry_id: str | uuid.UUID, *, meta: dict) -> MemoryEntry:
        """更新条目的附加信息（云端同步回填 cloud_id 等）。"""
        async with self._session_factory() as session:
            entry = await session.get(MemoryEntry, _as_uuid(entry_id))
            if entry is None:
                raise NotFoundError(f"记忆 {entry_id} 不存在", details={"memory_id": str(entry_id)})
            entry.meta = meta
            entry.updated_at = _utcnow()
            await session.commit()
            await session.refresh(entry)
            return entry

    async def list_layer(self, layer: MemoryLayer) -> list[MemoryEntry]:
        statement = (
            select(MemoryEntry)
            .where(MemoryEntry.layer == layer.value)
            .order_by(MemoryEntry.created_at, MemoryEntry.id)
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def delete(self, entry_id: str | uuid.UUID) -> None:
        """删除一条；不存在即报错（fail-closed，避免"删了个寂寞"静默通过）。"""
        key = _as_uuid(entry_id)
        async with self._session_factory() as session:
            result = await session.execute(delete(MemoryEntry).where(MemoryEntry.id == key))
            if result.rowcount == 0:
                await session.rollback()
                raise NotFoundError(f"记忆 {entry_id} 不存在", details={"memory_id": str(entry_id)})
            await session.commit()

    async def delete_many(self, entry_ids: Sequence[uuid.UUID]) -> int:
        if not entry_ids:
            return 0
        async with self._session_factory() as session:
            result = await session.execute(
                delete(MemoryEntry).where(MemoryEntry.id.in_(list(entry_ids)))
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def prune_layer(self, layer: MemoryLayer, *, keep: int) -> int:
        """容量清理：只保留最新 keep 条，多余的最旧条目删除，返回删除数。"""
        async with self._session_factory() as session:
            stale = list(
                await session.scalars(
                    select(MemoryEntry.id)
                    .where(MemoryEntry.layer == layer.value)
                    .order_by(MemoryEntry.created_at.desc(), MemoryEntry.id.desc())
                    .offset(keep)
                )
            )
            if not stale:
                return 0
            await session.execute(delete(MemoryEntry).where(MemoryEntry.id.in_(stale)))
            await session.commit()
            return len(stale)


def _as_uuid(value: str | uuid.UUID) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise NotFoundError(f"记忆 {value} 不存在", details={"memory_id": str(value)}) from exc
