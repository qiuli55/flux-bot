"""共享用户记忆的持久化读写。

仓储层只做 CRUD 与去重所需的最小查询，键/值校验、写入策略与注入预算都在
`flux.core.memory.shared_user` 服务层。日志内容不在这里打日志，避免误把用户
原文写进日志。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.logging import get_logger
from flux.models.shared_user_memory import UserJournalEntry, UserProfileEntry

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SharedUserMemoryRepository:
    """`user_profile_entries` / `user_journal_entries` 的读写入口。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # --- 用户画像 ---

    async def get_profile(self) -> dict[str, str]:
        statement = select(UserProfileEntry).order_by(UserProfileEntry.key)
        async with self._session_factory() as session:
            rows = list(await session.scalars(statement))
        return {row.key: row.value for row in rows}

    async def upsert_profile(self, values: dict[str, str]) -> dict[str, str]:
        """按 key 覆盖（last-write-wins）；返回覆盖后的完整画像。"""
        now = _utcnow()
        async with self._session_factory() as session:
            for key, value in values.items():
                row = await session.scalar(
                    select(UserProfileEntry).where(UserProfileEntry.key == key)
                )
                if row is None:
                    session.add(
                        UserProfileEntry(key=key, value=value, created_at=now, updated_at=now)
                    )
                else:
                    row.value = value
                    row.updated_at = now
            await session.commit()
        return await self.get_profile()

    async def delete_profile(self, key: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(UserProfileEntry).where(UserProfileEntry.key == key)
            )
            await session.commit()
            return bool(result.rowcount)

    # --- 共享日志 ---

    async def find_journal_by_hash(self, content_hash: str) -> UserJournalEntry | None:
        statement = select(UserJournalEntry).where(UserJournalEntry.content_hash == content_hash)
        async with self._session_factory() as session:
            return await session.scalar(statement)

    async def insert_journal(
        self,
        *,
        content: str,
        content_hash: str,
        source: str,
        source_bot_id: uuid.UUID | None = None,
    ) -> tuple[UserJournalEntry, bool]:
        """插入一条日志；哈希命中已有行时返回它，created=False。"""
        existing = await self.find_journal_by_hash(content_hash)
        if existing is not None:
            return existing, False
        now = _utcnow()
        row = UserJournalEntry(
            content=content,
            content_hash=content_hash,
            source=source,
            source_bot_id=source_bot_id,
            created_at=now,
            updated_at=now,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                # 并发插入同一内容：唯一约束兜底，回滚后返回先落库的那条。
                await session.rollback()
                existing = await self.find_journal_by_hash(content_hash)
                if existing is None:
                    raise
                return existing, False
            await session.refresh(row)
            return row, True

    async def list_journal(self, *, limit: int = 20, offset: int = 0) -> list[UserJournalEntry]:
        statement = (
            select(UserJournalEntry)
            .order_by(UserJournalEntry.created_at.desc(), UserJournalEntry.id.desc())
            .limit(limit)
            .offset(offset)
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def get_journal(self, entry_id: uuid.UUID) -> UserJournalEntry | None:
        async with self._session_factory() as session:
            return await session.get(UserJournalEntry, entry_id)

    async def delete_journal(self, entry_id: uuid.UUID) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(UserJournalEntry).where(UserJournalEntry.id == entry_id)
            )
            await session.commit()
            return bool(result.rowcount)
