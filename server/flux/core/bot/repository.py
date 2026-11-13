"""bots 表的读写入口。

与 TaskRepository 同一风格：一次调用一个会话、一个工作单元。
`next_run_at` 是调度器的唯一依据，落库保证进程重启后定时不丢。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.enums import TaskOrigin
from flux.errors import BadRequestError, NotFoundError
from flux.logging import get_logger
from flux.models.bot import Bot
from flux.models.task import Task

logger = get_logger(__name__)

#: 这些状态的任务仍在占用 Bot（同一时刻只允许一轮开工）
_ACTIVE_TASK_STATUSES = ("pending", "running", "waiting_for_user_decision")


class BotRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        name: str,
        description: str,
        avatar_shape: str,
        avatar_color: str,
        agent_id: uuid.UUID | None,
        model: str | None,
        api_key_id: uuid.UUID | None,
        permission_mode: str,
        decision_mode: str,
        workspace: str | None,
        status: str,
        schedule: str | None,
        next_run_at: datetime | None,
        timezone: str | None = None,
        missed_run_policy: str = "skip",
        failure_threshold: int = 3,
        token_budget: int | None = None,
        notify_on_failure: bool = True,
        event_triggers: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        allowed_commands: list[str] | None = None,
        allowed_paths: list[str] | None = None,
    ) -> Bot:
        async with self._session_factory() as session:
            bot = Bot(
                id=uuid.uuid4(),
                name=name.strip(),
                description=(description or "").strip(),
                avatar_shape=avatar_shape,
                avatar_color=avatar_color,
                agent_id=agent_id,
                model=(model or "").strip() or None,
                api_key_id=api_key_id,
                permission_mode=permission_mode,
                decision_mode=decision_mode,
                workspace=(workspace or "").strip() or None,
                status=status,
                schedule=schedule,
                next_run_at=next_run_at,
                timezone=timezone,
                missed_run_policy=missed_run_policy,
                failure_threshold=failure_threshold,
                token_budget=token_budget,
                notify_on_failure=notify_on_failure,
                event_triggers=event_triggers,
                allowed_tools=allowed_tools,
                allowed_commands=allowed_commands,
                allowed_paths=allowed_paths,
            )
            session.add(bot)
            await self._commit(session)
            await session.refresh(bot)
            return bot

    async def get(self, bot_id: uuid.UUID) -> Bot:
        async with self._session_factory() as session:
            return await self._require(session, bot_id)

    async def list(self, *, status: str | None = None, limit: int = 100) -> list[Bot]:
        statement = select(Bot).order_by(Bot.created_at.asc()).limit(limit)
        if status is not None:
            statement = statement.where(Bot.status == status)
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def list_due(self, now: datetime, *, limit: int = 20) -> list[Bot]:
        """取到期该开工的 Bot（active 且有 next_run_at <= now）。"""
        statement = (
            select(Bot)
            .where(
                Bot.status == "active",
                Bot.next_run_at.is_not(None),
                Bot.next_run_at <= now,
            )
            .order_by(Bot.next_run_at.asc())
            .limit(limit)
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def list_unsettled(self, *, limit: int = 50) -> list[Bot]:
        """有终态任务还没计入健康统计的 Bot（含已暂停的，用于结算自愈）。"""
        statement = (
            select(Bot)
            .where(
                Bot.last_task_id.is_not(None),
                Bot.last_settled_task_id.is_distinct_from(Bot.last_task_id),
            )
            .limit(limit)
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def is_busy(self, bot_id: uuid.UUID) -> bool:
        """Bot 是否还有一轮开工没跑完（防重叠：定时撞上上一轮时跳过本次）。

        对话任务是同一 Bot 名下的普通 tasks 行，但它由 Bot 对话队列串行处理，
        不代表「开工中的一轮」；结算与定时调度必须把它排除，否则聊天会永远占着忙。
        后台子任务同理：它们独立于开工链路运行，用户还能继续聊天、定时/事件/手动开工
        也不该被一条长活锁住，因此只看 origin 不是 subtask 的任务。
        """
        async with self._session_factory() as session:
            chat_task_id = select(Bot.chat_task_id).where(Bot.id == bot_id).scalar_subquery()
            task_id = await session.scalar(
                select(Task.id)
                .where(
                    Task.bot_id == bot_id,
                    Task.status.in_(_ACTIVE_TASK_STATUSES),
                    Task.origin != TaskOrigin.SUBTASK.value,
                    or_(chat_task_id.is_(None), Task.id != chat_task_id),
                )
                .limit(1)
            )
            return task_id is not None

    async def update(self, bot_id: uuid.UUID, values: dict[str, Any]) -> Bot:
        """按字段字典更新（只写调用方给出的键）。"""
        async with self._session_factory() as session:
            bot = await self._require(session, bot_id)
            for key, value in values.items():
                setattr(bot, key, value)
            await self._commit(session)
            await session.refresh(bot)
            return bot

    async def mark_run(
        self,
        bot_id: uuid.UUID,
        *,
        task_id: uuid.UUID,
        started_at: datetime,
        next_run_at: datetime | None,
    ) -> Bot:
        """记下本次开工的任务与下一次触发时间（先推进 next_run_at，再起 Run）。"""
        return await self.update(
            bot_id,
            {"last_task_id": task_id, "last_run_at": started_at, "next_run_at": next_run_at},
        )

    async def delete(self, bot_id: uuid.UUID) -> None:
        async with self._session_factory() as session:
            bot = await self._require(session, bot_id)
            # 历史任务保留（tasks.bot_id 会被外键置空），Bot 本身彻底删除
            await session.execute(delete(Bot).where(Bot.id == bot.id))
            await self._commit(session)

    async def _require(self, session: AsyncSession, bot_id: uuid.UUID) -> Bot:
        bot = await session.get(Bot, bot_id)
        if bot is None:
            raise NotFoundError(f"Bot {bot_id} 不存在", details={"bot_id": str(bot_id)})
        return bot

    async def _commit(self, session: AsyncSession) -> None:
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            logger.warning("Bot 写入违反数据库约束：%s", exc.orig)
            raise BadRequestError(f"Bot 写入违反数据库约束：{exc.orig}") from exc
