"""`bot_turns` 的持久化读写。

会话边界与其余仓储一致：一次调用一个会话、一次提交。状态迁移只允许通过这里，
调用方拿不到「只改内存里的 ORM 对象」的捷径，断线重连与重启恢复才有统一口径。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.core.user_scope import normalize_cloud_user_id, owned_clause, owns
from flux.errors import BadRequestError, NotFoundError
from flux.logging import get_logger
from flux.models.bot_turn import BotTurn

logger = get_logger(__name__)

#: 一次 turn 已经结束、不会再变化的状态。
TERMINAL_TURN_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BotTurnRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        bot_id: uuid.UUID,
        task_id: uuid.UUID,
        client_turn_id: str | None = None,
        cloud_user_id: str | None = None,
    ) -> tuple[BotTurn, bool]:
        """写入一条 queued turn，返回 (行, 是否新建)。

        `client_turn_id` 撞到同一 Bot 的唯一约束时，按幂等语义返回已有行——
        调用方不需要因为并发重试而看到 500。
        """
        now = _utcnow()
        row = BotTurn(
            id=uuid.uuid4(),
            bot_id=bot_id,
            task_id=task_id,
            status="queued",
            client_turn_id=(client_turn_id or "").strip() or None,
            cloud_user_id=(cloud_user_id or "").strip() or None,
            created_at=now,
            updated_at=now,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if row.client_turn_id is not None:
                    existing = await self._by_client_id(session, bot_id, row.client_turn_id)
                    if existing is not None:
                        return existing, False
                logger.warning("Bot turn 写入违反数据库约束 bot=%s", bot_id)
                raise BadRequestError("Bot turn 写入违反数据库约束") from None
            await session.refresh(row)
            return row, True

    async def get(
        self,
        bot_id: uuid.UUID,
        turn_id: uuid.UUID,
        *,
        cloud_user_id: str | None = None,
        owner_cloud_user_id: str | None = None,
        scoped: bool = False,
    ) -> BotTurn:
        """取一轮对话；`scoped=True` 时只返回当前用户自己的轮次（含主用户的历史 NULL）。"""
        async with self._session_factory() as session:
            row = await session.get(BotTurn, turn_id)
            if row is None or row.bot_id != bot_id:
                raise NotFoundError(
                    f"Turn {turn_id} 不存在",
                    details={"turn_id": str(turn_id), "bot_id": str(bot_id)},
                )
            if scoped:
                current = normalize_cloud_user_id(cloud_user_id) or normalize_cloud_user_id(
                    owner_cloud_user_id
                )
                if not owns(row.cloud_user_id, current, owner_cloud_user_id):
                    raise NotFoundError(
                        f"Turn {turn_id} 不存在",
                        details={"turn_id": str(turn_id), "bot_id": str(bot_id)},
                    )
            return row

    async def get_by_id(self, turn_id: uuid.UUID) -> BotTurn | None:
        async with self._session_factory() as session:
            return await session.get(BotTurn, turn_id)

    async def find_by_client_id(
        self,
        bot_id: uuid.UUID,
        client_turn_id: str | None,
        *,
        cloud_user_id: str | None = None,
        owner_cloud_user_id: str | None = None,
        scoped: bool = False,
    ) -> BotTurn | None:
        text = (client_turn_id or "").strip()
        if not text:
            return None
        async with self._session_factory() as session:
            return await self._by_client_id(
                session,
                bot_id,
                text,
                cloud_user_id=cloud_user_id,
                owner_cloud_user_id=owner_cloud_user_id,
                scoped=scoped,
            )

    async def list(
        self,
        bot_id: uuid.UUID,
        *,
        limit: int = 50,
        before: datetime | None = None,
        cloud_user_id: str | None = None,
        owner_cloud_user_id: str | None = None,
        scoped: bool = False,
    ) -> list[BotTurn]:
        statement = select(BotTurn).where(BotTurn.bot_id == bot_id)
        if scoped:
            current = normalize_cloud_user_id(cloud_user_id) or normalize_cloud_user_id(
                owner_cloud_user_id
            )
            if current is None:
                return []
            statement = statement.where(
                owned_clause(BotTurn.cloud_user_id, current, owner_cloud_user_id)
            )
        if before is not None:
            statement = statement.where(BotTurn.created_at < before)
        statement = statement.order_by(BotTurn.created_at.desc(), BotTurn.id.desc()).limit(limit)
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def set_running(self, turn_id: uuid.UUID) -> BotTurn:
        return await self._update(turn_id, status="running", error=None, error_code=None)

    async def set_user_message(self, turn_id: uuid.UUID, message_id: uuid.UUID) -> BotTurn:
        return await self._update(turn_id, user_message_id=message_id)

    async def complete(self, turn_id: uuid.UUID, reply_message_id: uuid.UUID) -> BotTurn:
        return await self._update(
            turn_id,
            status="completed",
            reply_message_id=reply_message_id,
            error=None,
            error_code=None,
            completed_at=_utcnow(),
        )

    async def fail(self, turn_id: uuid.UUID, *, error: str, error_code: str) -> BotTurn:
        return await self._update(
            turn_id,
            status="failed",
            error=(error or "")[:2000] or "Turn 执行失败",
            error_code=error_code[:64],
            completed_at=_utcnow(),
        )

    async def mark_interrupted(self, turn_ids: Iterable[uuid.UUID] | None = None) -> int:
        """把遗留的 queued/running 收成 failed。

        不传 `turn_ids` 时用于启动恢复，收拾所有 queued/running；传入时用于 shutdown，
        只收拾这些 id 中仍为 running 的行，避免覆盖刚好已经完成的轮次。
        """
        now = _utcnow()
        statement = update(BotTurn)
        if turn_ids is None:
            statement = statement.where(BotTurn.status.in_(("queued", "running")))
        else:
            ids = list(turn_ids)
            if not ids:
                return 0
            statement = statement.where(BotTurn.id.in_(ids), BotTurn.status == "running")
        async with self._session_factory() as session:
            result = await session.execute(
                statement.values(
                    status="failed",
                    error="服务重启时这一轮还未完成，已标记为中断；请重新发送。",
                    error_code="interrupted",
                    completed_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def _update(self, turn_id: uuid.UUID, **values: object) -> BotTurn:
        async with self._session_factory() as session:
            row = await session.get(BotTurn, turn_id)
            if row is None:
                raise NotFoundError(f"Turn {turn_id} 不存在", details={"turn_id": str(turn_id)})
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_at = _utcnow()
            await session.commit()
            await session.refresh(row)
            return row

    async def _by_client_id(
        self,
        session: AsyncSession,
        bot_id: uuid.UUID,
        client_turn_id: str,
        *,
        cloud_user_id: str | None = None,
        owner_cloud_user_id: str | None = None,
        scoped: bool = False,
    ) -> BotTurn | None:
        statement = select(BotTurn).where(
            BotTurn.bot_id == bot_id,
            BotTurn.client_turn_id == client_turn_id,
        )
        if scoped:
            current = normalize_cloud_user_id(cloud_user_id) or normalize_cloud_user_id(
                owner_cloud_user_id
            )
            if current is None:
                return None
            statement = statement.where(
                owned_clause(BotTurn.cloud_user_id, current, owner_cloud_user_id)
            )
        return await session.scalar(statement)
