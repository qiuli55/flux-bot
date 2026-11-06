"""知识库后台索引 worker：按固定间隔做一轮增量索引。"""

from __future__ import annotations

import asyncio
import contextlib

from flux.core.knowledge.indexer import KnowledgeIndexer
from flux.logging import get_logger

logger = get_logger(__name__)

__all__ = ["KnowledgeWorker"]


class KnowledgeWorker:
    def __init__(
        self,
        indexer: KnowledgeIndexer,
        *,
        interval_seconds: float = 300.0,
        initial_delay_seconds: float = 5.0,
    ) -> None:
        self._indexer = indexer
        self._interval_seconds = max(10.0, float(interval_seconds))
        self._initial_delay_seconds = max(0.0, float(initial_delay_seconds))
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="flux-knowledge-worker")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def run_once(self) -> dict[str, int]:
        """手动触发一轮索引（测试与运维共用）。异常不向上抛，只记日志。"""
        try:
            return await self._indexer.index_all()
        except Exception as exc:  # noqa: BLE001
            logger.warning("知识库索引轮次失败：%s", exc)
            return {}

    async def _loop(self) -> None:
        if self._initial_delay_seconds:
            await asyncio.sleep(self._initial_delay_seconds)
        while True:
            await self.run_once()
            await asyncio.sleep(self._interval_seconds)
