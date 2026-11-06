"""任务 ↔ 外部 Agent 会话 id 的读写（`task_runtime_sessions`）。

opencode / codex 跨轮复用会话时，平台要记住上一轮用的是哪个 external session。
写入按 `(task_id, agent_id, runtime_id)` upsert：同一个任务、同一个 Agent、同一个
runtime 只保留最新一条。agent_id 为空表示「任务未指定 Agent」，与加列之前的旧绑定
同一口径（只有这类任务会续用旧会话）。
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.models.runtime_session import TaskRuntimeSession


def _as_uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _agent_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    return None if value in (None, "") else _as_uuid(value)  # type: ignore[arg-type]


class TaskRuntimeSessionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _scope(task_uuid: uuid.UUID, runtime_id: str, agent_uuid: uuid.UUID | None) -> tuple:
        clause = (
            TaskRuntimeSession.task_id == task_uuid,
            TaskRuntimeSession.runtime_id == runtime_id,
        )
        if agent_uuid is None:
            return (*clause, TaskRuntimeSession.agent_id.is_(None))
        return (*clause, TaskRuntimeSession.agent_id == agent_uuid)

    async def get(
        self,
        task_id: str | uuid.UUID,
        runtime_id: str,
        agent_id: str | uuid.UUID | None = None,
    ) -> str:
        """取该任务在该 Agent + runtime 上的会话 id；没有则返回空串。"""
        task_uuid = _as_uuid(task_id)
        statement = select(TaskRuntimeSession).where(
            *self._scope(task_uuid, runtime_id, _agent_uuid(agent_id))
        )
        async with self._session_factory() as session:
            row = await session.scalar(statement)
            return row.session_id if row is not None else ""

    async def get_context_tokens(
        self,
        task_id: str | uuid.UUID,
        runtime_id: str,
        agent_id: str | uuid.UUID | None = None,
    ) -> int:
        """取上一轮该会话的上下文规模（token）；没有绑定或未报送时返回 0。"""
        task_uuid = _as_uuid(task_id)
        statement = select(TaskRuntimeSession).where(
            *self._scope(task_uuid, runtime_id, _agent_uuid(agent_id))
        )
        async with self._session_factory() as session:
            row = await session.scalar(statement)
            return int(row.context_tokens or 0) if row is not None else 0

    async def set(
        self,
        task_id: str | uuid.UUID,
        runtime_id: str,
        session_id: str,
        agent_id: str | uuid.UUID | None = None,
        context_tokens: int | None = None,
    ) -> None:
        """记住会话 id（upsert）；空 id 视为无需记录。

        `context_tokens` 是上一轮该会话的上下文规模：None = 本次不报送（保留旧值，
        新建行记 0），供下一轮判断要不要压缩 / 重开会话。
        """
        if not session_id:
            return
        task_uuid = _as_uuid(task_id)
        agent_uuid = _agent_uuid(agent_id)
        async with self._session_factory() as session:
            statement = select(TaskRuntimeSession).where(
                *self._scope(task_uuid, runtime_id, agent_uuid)
            )
            row = await session.scalar(statement)
            if row is None:
                session.add(
                    TaskRuntimeSession(
                        task_id=task_uuid,
                        agent_id=agent_uuid,
                        runtime_id=runtime_id,
                        session_id=session_id,
                        context_tokens=max(0, int(context_tokens or 0)),
                    )
                )
            else:
                row.session_id = session_id
                if context_tokens is not None:
                    row.context_tokens = max(0, int(context_tokens))
            await session.commit()

    async def clear(
        self,
        task_id: str | uuid.UUID,
        runtime_id: str,
        agent_id: str | uuid.UUID | None = None,
    ) -> None:
        """丢弃绑定（会话真的失效时用；下一轮会重新新建）。"""
        task_uuid = _as_uuid(task_id)
        async with self._session_factory() as session:
            statement = select(TaskRuntimeSession).where(
                *self._scope(task_uuid, runtime_id, _agent_uuid(agent_id))
            )
            row = await session.scalar(statement)
            if row is not None:
                await session.delete(row)
                await session.commit()

    async def delete_for_task(self, task_id: str | uuid.UUID) -> int:
        """删除该任务的全部会话绑定（任务被彻底删除时调用），返回删除条数。"""
        task_uuid = _as_uuid(task_id)
        async with self._session_factory() as session:
            result = await session.execute(
                delete(TaskRuntimeSession).where(TaskRuntimeSession.task_id == task_uuid)
            )
            await session.commit()
            return int(result.rowcount or 0)


__all__ = ["TaskRuntimeSessionRepository"]
