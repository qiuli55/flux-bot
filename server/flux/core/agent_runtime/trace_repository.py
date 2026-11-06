"""执行轨迹表的读写（`run_trace_items`）。

写入是「整轮替换」：Run 终态时轨迹才落库，用 delete + insert 保证重放/重试幂等。
读取分两种：详情取整轮全部字段，任务列表只取统计需要的轻量列（不拉命令输出）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.models.run_trace import RunTraceItem


def _as_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


class RunTraceRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def replace(
        self,
        run_id: str,
        task_id: str | uuid.UUID | None,
        items: Iterable[dict[str, Any]],
    ) -> None:
        task_uuid = _as_uuid(task_id)
        rows = [
            RunTraceItem(
                run_id=run_id,
                task_id=task_uuid,
                seq=index + 1,
                kind=str(item.get("kind") or ""),
                category=str(item.get("category") or ""),
                text=str(item.get("text") or ""),
                name=str(item.get("name") or ""),
                detail=str(item.get("detail") or ""),
                output=str(item.get("output") or ""),
                status=str(item.get("status") or ""),
                call_id=str(item.get("call_id") or ""),
                paths=item.get("paths") or None,
            )
            for index, item in enumerate(items)
        ]
        async with self._session_factory() as session:
            await session.execute(delete(RunTraceItem).where(RunTraceItem.run_id == run_id))
            session.add_all(rows)
            await session.commit()

    async def list_for_run(self, run_id: str) -> list[RunTraceItem]:
        statement = (
            select(RunTraceItem).where(RunTraceItem.run_id == run_id).order_by(RunTraceItem.seq)
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def list_for_runs(self, run_ids: Iterable[str]) -> list[dict[str, Any]]:
        """只取统计列：概览不需要命令输出与思考正文。"""
        ids = [run_id for run_id in run_ids if run_id]
        if not ids:
            return []
        statement = (
            select(
                RunTraceItem.run_id,
                RunTraceItem.kind,
                RunTraceItem.category,
                RunTraceItem.paths,
                RunTraceItem.call_id,
            )
            .where(RunTraceItem.run_id.in_(ids))
            .order_by(RunTraceItem.run_id, RunTraceItem.seq)
        )
        async with self._session_factory() as session:
            result = await session.execute(statement)
            return [dict(row._mapping) for row in result]


__all__ = ["RunTraceRepository"]
