"""Supervisor 的事务性 outbox。

事件落库与"接下来要做什么"写在同一个事务里，然后由后台 worker 按 topic 领取。
这样进程即使在第一件事做完后立刻崩溃，重启后也能从 outbox 继续，不依赖内存队列。

状态机：pending → processing → done / dead。失败按 attempts 退避重试，
超过上限落 dead（保留错误信息供排查），绝不静默丢弃。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.models.supervisor import SupervisorOutbox as SupervisorOutboxRow

__all__ = ["REPORT_READY_TOPIC", "TRIGGER_TOPIC", "SupervisorOutbox"]

#: 事件入库后要求判断层处理的 topic
TRIGGER_TOPIC = "supervisor.trigger_received"
#: 报告落库后要求投递到订阅线程的 topic
REPORT_READY_TOPIC = "supervisor.report_ready"

#: dedupe_key 列宽；超长键用前缀 + 摘要，既稳定又不撞库
_DEDUPE_KEY_LIMIT = 64


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_dedupe_key(key: str) -> str:
    """把任意幂等键压到列宽以内（同一输入永远得到同一结果）。"""
    if len(key) <= _DEDUPE_KEY_LIMIT:
        return key
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"{key[:24]}:{digest[:32]}"


class SupervisorOutbox:
    """outbox 行的入队 / 领取 / 完成 / 失败。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_attempts: int = 5,
    ) -> None:
        self._session_factory = session_factory
        self._max_attempts = max(1, max_attempts)

    async def enqueue(
        self,
        topic: str,
        payload: dict,
        *,
        dedupe_key: str | None = None,
    ) -> bool:
        """排一件事；给了 dedupe_key 且已存在时静默跳过（返回 False）。

        只用于"独立于其它事务"的入队；需要与业务写入同事务时，调用方应直接
        `session.add(SupervisorOutboxRow(...))`（如报告落库）。
        """
        key = normalize_dedupe_key(dedupe_key) if dedupe_key else None
        async with self._session_factory() as session:
            if key is not None:
                exists = await session.scalar(
                    select(SupervisorOutboxRow.id).where(SupervisorOutboxRow.dedupe_key == key)
                )
                if exists is not None:
                    return False
            session.add(
                SupervisorOutboxRow(
                    id=uuid.uuid4(),
                    topic=topic,
                    payload=payload,
                    dedupe_key=key,
                    status="pending",
                    attempts=0,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                # 并发入队同一件事：唯一索引挡下后到者，视作已排队。
                await session.rollback()
                return False
            return True

    async def claim(self, *, limit: int = 10) -> list[SupervisorOutboxRow]:
        """领取到期的 pending 行并标记 processing。

        领取不是数据库级的原子操作，但单进程 worker（当前唯一形态）足够；
        多 worker 时由 `requeue_stale` 兜底，不会永久卡住。
        """
        now = _utcnow()
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(SupervisorOutboxRow)
                    .where(
                        SupervisorOutboxRow.status == "pending",
                        or_(
                            SupervisorOutboxRow.available_at.is_(None),
                            SupervisorOutboxRow.available_at <= now,
                        ),
                    )
                    .order_by(SupervisorOutboxRow.created_at, SupervisorOutboxRow.id)
                    .limit(max(1, limit))
                )
            )
            for row in rows:
                row.status = "processing"
            await session.commit()
            return rows

    async def complete(self, outbox_id: uuid.UUID) -> None:
        await self._set_status(outbox_id, "done", error=None)

    async def fail(self, outbox_id: uuid.UUID, error: str) -> None:
        """记一次失败：未到上限则退避重试，到上限落 dead。"""
        now = _utcnow()
        async with self._session_factory() as session:
            row = await session.get(SupervisorOutboxRow, outbox_id)
            if row is None:
                return
            row.attempts = int(row.attempts or 0) + 1
            row.last_error = error[:2000]
            if row.attempts >= self._max_attempts:
                row.status = "dead"
                row.processed_at = now
            else:
                row.status = "pending"
                # 线性退避，封顶 5 分钟：既给上游恢复时间，又不让队列长期停摆。
                delay = min(30 * row.attempts, 300)
                row.available_at = now + timedelta(seconds=delay)
            await session.commit()

    async def requeue_stale(self, *, older_than_seconds: int = 600) -> int:
        """把卡在 processing 太久的行退回 pending（进程崩溃恢复）。"""
        cutoff = _utcnow() - timedelta(seconds=max(0, older_than_seconds))
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(SupervisorOutboxRow).where(
                        SupervisorOutboxRow.status == "processing",
                        SupervisorOutboxRow.updated_at <= cutoff,
                    )
                )
            )
            for row in rows:
                row.status = "pending"
                row.last_error = "处理超时，已退回队列"
            await session.commit()
            return len(rows)

    async def pending_count(self) -> int:
        async with self._session_factory() as session:
            value = await session.scalar(
                select(func.count())
                .select_from(SupervisorOutboxRow)
                .where(SupervisorOutboxRow.status.in_(("pending", "processing")))
            )
            return int(value or 0)

    async def _set_status(self, outbox_id: uuid.UUID, status: str, *, error: str | None) -> None:
        async with self._session_factory() as session:
            row = await session.get(SupervisorOutboxRow, outbox_id)
            if row is None:
                return
            row.status = status
            row.processed_at = _utcnow()
            if error is not None:
                row.last_error = error[:2000]
            await session.commit()
