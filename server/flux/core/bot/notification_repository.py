"""bot_notifications 表的读写入口。

通知只追加、只由「已读」推进状态，没有删除语义：
Bot 失败的历史提醒保留下来，用户随时能回看。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.models.bot_notification import BotNotification


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BotNotificationRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        bot_id: uuid.UUID | None,
        bot_name: str,
        task_id: uuid.UUID | None,
        level: str,
        title: str,
        body: str,
    ) -> BotNotification:
        async with self._session_factory() as session:
            row = BotNotification(
                id=uuid.uuid4(),
                bot_id=bot_id,
                bot_name=bot_name,
                task_id=task_id,
                level=level,
                title=title,
                body=body,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def list(self, *, unread_only: bool = False, limit: int = 100) -> list[BotNotification]:
        statement = select(BotNotification).order_by(BotNotification.created_at.desc()).limit(limit)
        if unread_only:
            statement = statement.where(BotNotification.read_at.is_(None))
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def unread_count(self) -> int:
        async with self._session_factory() as session:
            total = await session.scalar(
                select(func.count())
                .select_from(BotNotification)
                .where(BotNotification.read_at.is_(None))
            )
            return int(total or 0)

    async def mark_read(self, notification_id: uuid.UUID) -> BotNotification | None:
        async with self._session_factory() as session:
            row = await session.get(BotNotification, notification_id)
            if row is None:
                return None
            if row.read_at is None:
                row.read_at = _utcnow()
                await session.commit()
                await session.refresh(row)
            return row

    async def mark_all_read(self) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                update(BotNotification)
                .where(BotNotification.read_at.is_(None))
                .values(read_at=_utcnow())
            )
            await session.commit()
            return int(result.rowcount or 0)
