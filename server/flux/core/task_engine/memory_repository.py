"""工作记忆卡的读写（`task_memory_cards`）。

写入按 `(task_id, run_id)` 幂等：同一个 Run 的终态事件可能重复投递，
下一轮不能看到两张互相矛盾的记忆卡。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.models.task_memory import TaskMemoryCard


def _as_uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class TaskMemoryRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, task_id: str | uuid.UUID, run_id: str, content: str) -> TaskMemoryCard:
        """写一张卡；同任务同 Run 已存在时返回已有卡（不覆盖、不重复）。"""
        task_uuid = _as_uuid(task_id)
        async with self._session_factory() as session:
            existing = await self._find(session, task_uuid, run_id)
            if existing is not None:
                return existing
            # 显式写入微秒级时间：SQLite 的 CURRENT_TIMESTAMP 只到秒，
            # 同一秒内连续写两张卡时「最新」会退化成随机 id 排序。
            now = datetime.now(timezone.utc)
            card = TaskMemoryCard(
                task_id=task_uuid,
                run_id=run_id,
                content=content,
                created_at=now,
                updated_at=now,
            )
            session.add(card)
            await session.commit()
            await session.refresh(card)
            return card

    async def latest(self, task_id: str | uuid.UUID) -> TaskMemoryCard | None:
        task_uuid = _as_uuid(task_id)
        statement = (
            select(TaskMemoryCard)
            .where(TaskMemoryCard.task_id == task_uuid)
            .order_by(TaskMemoryCard.created_at.desc(), TaskMemoryCard.id.desc())
            .limit(1)
        )
        async with self._session_factory() as session:
            return await session.scalar(statement)

    async def list_recent(
        self, task_id: str | uuid.UUID, *, limit: int = 5
    ) -> list[TaskMemoryCard]:
        task_uuid = _as_uuid(task_id)
        statement = (
            select(TaskMemoryCard)
            .where(TaskMemoryCard.task_id == task_uuid)
            .order_by(TaskMemoryCard.created_at.desc(), TaskMemoryCard.id.desc())
            .limit(max(1, limit))
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def exists(self, task_id: str | uuid.UUID, run_id: str) -> bool:
        task_uuid = _as_uuid(task_id)
        async with self._session_factory() as session:
            return await self._find(session, task_uuid, run_id) is not None

    async def delete_for_task(self, task_id: str | uuid.UUID) -> int:
        """删除该任务的全部记忆卡（任务被彻底删除时调用），返回删除条数。"""
        task_uuid = _as_uuid(task_id)
        async with self._session_factory() as session:
            result = await session.execute(
                delete(TaskMemoryCard).where(TaskMemoryCard.task_id == task_uuid)
            )
            await session.commit()
            return int(result.rowcount or 0)

    @staticmethod
    async def _find(
        session: AsyncSession, task_uuid: uuid.UUID, run_id: str
    ) -> TaskMemoryCard | None:
        statement = select(TaskMemoryCard).where(
            TaskMemoryCard.task_id == task_uuid,
            TaskMemoryCard.run_id == run_id,
        )
        return await session.scalar(statement)


__all__ = ["TaskMemoryRepository"]
