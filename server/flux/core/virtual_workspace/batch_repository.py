"""apply_batches 的读写入口（P0-1 崩溃恢复 / P1-2 整批回滚）。

写入顺序是这套机制的核心：批记录先于任何磁盘操作落库，阶段推进逐次提交——
崩溃时"盘上做到哪一步"最多只领先"日志里记的"一步，恢复算法据此对账。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.enums import ApplyBatchStatus
from flux.errors import NotFoundError
from flux.models.apply_batch import ApplyBatch


class ApplyBatchRepository:
    """每次调用自开一个会话并提交（与 ProposalRepository 同一模式）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        change_ids: Sequence[str | uuid.UUID],
        backup_root: str | None = None,
        phase: str = "prepared",
        batch_id: uuid.UUID | None = None,
    ) -> ApplyBatch:
        batch = ApplyBatch(
            id=batch_id or uuid.uuid4(),
            status=ApplyBatchStatus.IN_PROGRESS.value,
            change_ids=[str(change_id) for change_id in change_ids],
            phase=phase,
            backup_root=backup_root,
        )
        async with self._session_factory() as session:
            session.add(batch)
            await session.commit()
            return batch

    async def get(self, batch_id: str | uuid.UUID) -> ApplyBatch:
        key = self._as_uuid(batch_id)
        async with self._session_factory() as session:
            batch = await session.get(ApplyBatch, key)
            if batch is None:
                raise NotFoundError(
                    f"Apply 批次 {batch_id} 不存在", details={"batch_id": str(batch_id)}
                )
            return batch

    async def list_by_status(self, status: ApplyBatchStatus) -> list[ApplyBatch]:
        """按批状态列出（待人工处理的 needs_attention 批靠它捞出）。"""
        statement = (
            select(ApplyBatch)
            .where(ApplyBatch.status == status.value)
            .order_by(ApplyBatch.created_at, ApplyBatch.id)
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def list_in_progress(self) -> list[ApplyBatch]:
        """所有未对账的批（含正在跑的与崩溃遗留的），按创建时间排序。"""
        statement = (
            select(ApplyBatch)
            .where(ApplyBatch.status == ApplyBatchStatus.IN_PROGRESS.value)
            .order_by(ApplyBatch.created_at, ApplyBatch.id)
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def list_recent(self, *, limit: int = 20) -> list[ApplyBatch]:
        """最近若干批（前端"回滚上一次"与人工排查用），最新的在前。"""
        statement = (
            select(ApplyBatch)
            .order_by(ApplyBatch.created_at.desc(), ApplyBatch.id.desc())
            .limit(limit)
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def find_for_change(self, change_id: str | uuid.UUID) -> ApplyBatch | None:
        """找出包含某条 change 的最近一批（单条回滚需要定位它的批日志）。"""
        key = str(change_id)
        statement = select(ApplyBatch).order_by(ApplyBatch.created_at.desc(), ApplyBatch.id.desc())
        async with self._session_factory() as session:
            for batch in await session.scalars(statement):
                if key in (batch.change_ids or []):
                    return batch
        return None

    async def set_phase(self, batch_id: str | uuid.UUID, phase: str) -> ApplyBatch:
        key = self._as_uuid(batch_id)
        async with self._session_factory() as session:
            batch = await session.get(ApplyBatch, key)
            if batch is None:
                raise NotFoundError(
                    f"Apply 批次 {batch_id} 不存在", details={"batch_id": str(batch_id)}
                )
            batch.phase = phase
            await session.commit()
            return batch

    async def finish(
        self,
        batch_id: str | uuid.UUID,
        *,
        status: ApplyBatchStatus,
        error: str | None = None,
        recovery_note: str | None = None,
    ) -> ApplyBatch:
        """收尾一批：写终态、失败原因/恢复说明与完成时间。"""
        key = self._as_uuid(batch_id)
        async with self._session_factory() as session:
            batch = await session.get(ApplyBatch, key)
            if batch is None:
                raise NotFoundError(
                    f"Apply 批次 {batch_id} 不存在", details={"batch_id": str(batch_id)}
                )
            batch.status = status.value
            batch.error = error
            batch.recovery_note = recovery_note
            batch.finished_at = datetime.now(timezone.utc)
            await session.commit()
            return batch

    @staticmethod
    def _as_uuid(value: str | uuid.UUID) -> uuid.UUID:
        if isinstance(value, uuid.UUID):
            return value
        try:
            return uuid.UUID(str(value))
        except ValueError as exc:
            raise NotFoundError(
                f"Apply 批次 {value} 不存在", details={"batch_id": str(value)}
            ) from exc
