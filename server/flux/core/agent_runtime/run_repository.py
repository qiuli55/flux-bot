"""Agent Run 的持久化仓储（修复方案 §2.2 / §2.7）。

Run 生命周期状态的权威存储。所有写入都用定向 UPDATE（而不是"读出 ORM 对象 → 改字段 →
提交"）：多个后端实例可能共享同一个数据库，读改写会把别人刚写的心跳/状态整体覆盖回去。

时间戳统一以 `datetime.now(timezone.utc)` 写入；SQLite 读回来会丢 tzinfo，
需要用时间做减法的地方由调用方按 `_as_utc` 归一（心跳判据本身走进程内单调时钟，不读库）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.enums import DshRunStatus
from flux.models.agent_run import AgentRun

#: 终态：进入后生命周期结束，不再接受 cancel / 心跳
TERMINAL_RUN_STATUSES: frozenset[str] = frozenset(
    {
        DshRunStatus.COMPLETED.value,
        DshRunStatus.FAILED.value,
        DshRunStatus.TIMEOUT.value,
        DshRunStatus.CANCELLED.value,
        DshRunStatus.INTERRUPTED.value,
    }
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """库里的时间归一到 aware UTC（SQLite 不保存时区）。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def is_terminal(status: str) -> bool:
    return status in TERMINAL_RUN_STATUSES


class AgentRunRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        run_id: str,
        session_id: str,
        instruction: str,
        status: DshRunStatus,
        agent_id: str | None = None,
        task_id: uuid.UUID | None = None,
        owner_pid: int | None = None,
        started_at: datetime | None = None,
        permission_mode: str = "normal",
    ) -> AgentRun:
        now = utcnow()
        run = AgentRun(
            id=run_id,
            session_id=session_id,
            instruction=instruction,
            status=str(status),
            permission_mode=permission_mode,
            agent_id=agent_id,
            task_id=task_id,
            owner_pid=owner_pid,
            started_at=started_at,
            last_state_change_at=now,
            last_heartbeat_at=now,
        )
        async with self._session_factory() as session:
            session.add(run)
            await session.commit()
        return run

    async def get(self, run_id: str) -> AgentRun | None:
        async with self._session_factory() as session:
            return await session.get(AgentRun, run_id)

    async def list_non_terminal(self) -> list[AgentRun]:
        statement = (
            select(AgentRun)
            .where(AgentRun.status.not_in(tuple(TERMINAL_RUN_STATUSES)))
            .order_by(AgentRun.created_at)
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def list_by_task(self, task_id: uuid.UUID, *, limit: int = 5) -> list[AgentRun]:
        """任务名下的最近若干 Run（按创建时间取最新 limit 条，返回时翻成升序）。

        下一次 `context.get` 靠它把上一轮的终止原因（finish_reason / error）带回给 Agent，
        否则"上一轮为什么挂"只留在人看的表里，Agent 永远读不到。
        """
        statement = (
            select(AgentRun)
            .where(AgentRun.task_id == task_id)
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .limit(max(limit, 1))
        )
        async with self._session_factory() as session:
            newest_first = list(await session.scalars(statement))
        return list(reversed(newest_first))

    async def set_status(
        self,
        run_id: str,
        status: DshRunStatus,
        *,
        error: str | None = None,
        finish_reason: str | None = None,
        timeout_kind: str | None = None,
        finished: bool = False,
    ) -> None:
        """更新状态并刷新 last_state_change_at；终态同时写 finished_at。"""
        now = utcnow()
        values: dict[str, object] = {
            "status": str(status),
            "last_state_change_at": now,
            "updated_at": now,
        }
        if error is not None:
            values["error"] = error
        if finish_reason is not None:
            values["finish_reason"] = finish_reason
        if timeout_kind is not None:
            values["timeout_kind"] = timeout_kind
        if finished:
            values["finished_at"] = now
        await self._update(run_id, values)

    async def set_process(self, run_id: str, *, pid: int, pgid: int) -> None:
        await self._update(run_id, {"pid": pid, "pgid": pgid, "updated_at": utcnow()})

    async def request_cancel(self, run_id: str) -> None:
        await self._update(run_id, {"cancel_requested": True, "updated_at": utcnow()})

    async def touch_heartbeat(self, run_id: str) -> None:
        now = utcnow()
        await self._update(run_id, {"last_heartbeat_at": now, "updated_at": now})

    async def touch_output(self, run_id: str) -> None:
        """有事件到达即算一次有效进展（不能只看 stdout 是否有输出）。"""
        now = utcnow()
        await self._update(
            run_id, {"last_output_at": now, "last_heartbeat_at": now, "updated_at": now}
        )

    async def touch_mcp_activity(self, run_id: str) -> None:
        now = utcnow()
        await self._update(
            run_id,
            {"last_mcp_activity_at": now, "last_heartbeat_at": now, "updated_at": now},
        )

    async def set_last_event(self, run_id: str, event: dict | None) -> None:
        await self._update(run_id, {"last_event": event, "updated_at": utcnow()})

    async def _update(self, run_id: str, values: dict[str, object]) -> None:
        # 终态一旦写下就不再被非终态写回（cancel 收尾后 _execute 的迟到写入必须落空）
        async with self._session_factory() as session:
            await session.execute(update(AgentRun).where(AgentRun.id == run_id).values(**values))
            await session.commit()


__all__ = [
    "AgentRunRepository",
    "TERMINAL_RUN_STATUSES",
    "as_utc",
    "is_terminal",
    "utcnow",
]
