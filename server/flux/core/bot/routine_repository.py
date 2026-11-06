"""bot_routines 表的读写入口。

会话边界与 `BotRepository` 一致：一次调用一个会话、一次提交。
`next_run_at` 是例程调度的唯一依据，落库保证重启后定时不丢。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.errors import BadRequestError, NotFoundError
from flux.logging import get_logger
from flux.models.bot_routine import BotRoutine

logger = get_logger(__name__)


class BotRoutineRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        bot_id: uuid.UUID,
        name: str,
        instruction: str,
        schedule: str | None,
        next_run_at: datetime | None,
        enabled: bool = True,
    ) -> BotRoutine:
        async with self._session_factory() as session:
            row = BotRoutine(
                id=uuid.uuid4(),
                bot_id=bot_id,
                name=name.strip(),
                instruction=(instruction or "").strip(),
                schedule=schedule,
                next_run_at=next_run_at,
                enabled=enabled,
            )
            session.add(row)
            await self._commit(session)
            await session.refresh(row)
            return row

    async def get(self, routine_id: uuid.UUID) -> BotRoutine:
        async with self._session_factory() as session:
            return await self._require(session, routine_id)

    async def list_for_bot(self, bot_id: uuid.UUID) -> list[BotRoutine]:
        statement = (
            select(BotRoutine)
            .where(BotRoutine.bot_id == bot_id)
            .order_by(BotRoutine.created_at.asc())
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def list_due(self, now: datetime, *, limit: int = 20) -> list[BotRoutine]:
        """取到期该开工的例程（enabled 且有 next_run_at <= now）。"""
        statement = (
            select(BotRoutine)
            .where(
                BotRoutine.enabled.is_(True),
                BotRoutine.next_run_at.is_not(None),
                BotRoutine.next_run_at <= now,
            )
            .order_by(BotRoutine.next_run_at.asc())
            .limit(limit)
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def update(self, routine_id: uuid.UUID, values: dict[str, Any]) -> BotRoutine:
        async with self._session_factory() as session:
            row = await self._require(session, routine_id)
            for key, value in values.items():
                setattr(row, key, value)
            await self._commit(session)
            await session.refresh(row)
            return row

    async def delete(self, routine_id: uuid.UUID) -> None:
        async with self._session_factory() as session:
            row = await self._require(session, routine_id)
            await session.execute(delete(BotRoutine).where(BotRoutine.id == row.id))
            await self._commit(session)

    async def _require(self, session: AsyncSession, routine_id: uuid.UUID) -> BotRoutine:
        row = await session.get(BotRoutine, routine_id)
        if row is None:
            raise NotFoundError(
                f"例程 {routine_id} 不存在", details={"routine_id": str(routine_id)}
            )
        return row

    async def _commit(self, session: AsyncSession) -> None:
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            logger.warning("例程写入违反数据库约束：%s", exc.orig)
            raise BadRequestError(f"例程写入违反数据库约束：{exc.orig}") from exc
