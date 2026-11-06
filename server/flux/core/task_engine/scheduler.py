"""任务调度器（主规格 §5.1 Scheduler 职责：队列、优先级、资源分配、重试策略）。

M0 为进程内优先队列。优先级数字越小越优先（调度依据：任务复杂度、模型成本、Agent 可用性，
见主规格 §5.1 —— 复杂度/成本的量化打分属 M6 成本里程碑）。
"""

from __future__ import annotations

import heapq
import itertools
import time
from dataclasses import dataclass, field

from flux.errors import NotFoundError

DEFAULT_PRIORITY = 100


@dataclass(order=True)
class ScheduledTask:
    priority: int
    sequence: int
    created_at: float = field(compare=False)
    task_id: str = field(compare=False, default="")
    instruction: str = field(compare=False, default="")
    agent_id: str | None = field(compare=False, default=None)

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "priority": self.priority,
            "agent_id": self.agent_id,
            "instruction": self.instruction,
        }


class TaskScheduler:
    def __init__(self) -> None:
        self._queue: list[ScheduledTask] = []
        self._by_id: dict[str, ScheduledTask] = {}
        self._cancelled: set[str] = set()
        self._counter = itertools.count()

    def submit(
        self,
        task_id: str,
        instruction: str,
        *,
        priority: int = DEFAULT_PRIORITY,
        agent_id: str | None = None,
    ) -> ScheduledTask:
        task = ScheduledTask(
            priority=priority,
            sequence=next(self._counter),
            created_at=time.time(),
            task_id=task_id,
            instruction=instruction,
            agent_id=agent_id,
        )
        heapq.heappush(self._queue, task)
        self._by_id[task_id] = task
        return task

    def next(self) -> ScheduledTask | None:
        """取出下一个待执行任务；已取消的任务被跳过。"""
        while self._queue:
            task = heapq.heappop(self._queue)
            if task.task_id in self._cancelled:
                self._cancelled.discard(task.task_id)
                continue
            return task
        return None

    def cancel(self, task_id: str) -> bool:
        if task_id not in self._by_id:
            raise NotFoundError(f"任务 {task_id} 不在调度队列中", details={"task_id": task_id})
        if task_id in self._cancelled:
            return False
        self._cancelled.add(task_id)
        return True

    def pending(self) -> list[ScheduledTask]:
        return sorted(t for t in self._queue if t.task_id not in self._cancelled)

    @property
    def pending_count(self) -> int:
        return len(self.pending())
