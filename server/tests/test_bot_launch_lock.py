"""M4：「查忙 + 开工」必须对同一个 Bot 原子。

定时 tick、事件触发、手动 run_now 会并发走到同一条开工链路；这里同时拉起它们，
断言最终只有一个活跃任务，且输掉手动开工的一方仍按原语义得到 ConflictError。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from flux.container import Container
from flux.core.agent_runtime.runtimes.service import TaskRunService
from flux.core.bot.repository import BotRepository
from flux.enums import TaskStatus
from flux.errors import ConflictError

_ACTIVE_STATUSES = {
    TaskStatus.PENDING.value,
    TaskStatus.RUNNING.value,
    TaskStatus.WAITING_FOR_USER_DECISION.value,
}


@pytest.fixture(autouse=True)
def quiet_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """起 Run 换成空实现：任务停在 pending，正好观察同一时刻是否只开了一轮。"""

    async def _noop(self: TaskRunService, task: object, *args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(TaskRunService, "start", _noop)


def test_concurrent_launch_paths_create_only_one_active_task(
    container: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_is_busy = BotRepository.is_busy

    async def slow_is_busy(self: BotRepository, bot_id: uuid.UUID) -> bool:
        # 拉长查忙窗口：没有 Bot 级锁时，各路径会同时看到“空闲”并各自开工。
        await asyncio.sleep(0.01)
        return await original_is_busy(self, bot_id)

    monkeypatch.setattr(BotRepository, "is_busy", slow_is_busy)

    async def _scenario() -> None:
        bot = await container.bots.create(
            name="并发巡检",
            description="检查服务健康",
            schedule="*/5 * * * *",
            event_triggers=["task.completed"],
        )
        moment = datetime.now(timezone.utc) + timedelta(days=1)
        losers = await asyncio.gather(
            container.bots.tick(moment),
            container.bots._dispatch_event("task.completed", {"task_id": "other-task"}),
            *(
                container.bots.run_now(bot.id, instruction=f"手动开工 {index}")
                for index in range(5)
            ),
            return_exceptions=True,
        )

        tasks = await container.task_repo.list(bot_id=bot.id, limit=100)
        assert len(tasks) == 1, [(str(task.id), task.status) for task in tasks]
        assert tasks[0].status in _ACTIVE_STATUSES
        assert (await container.bots.get(bot.id)).last_task_id == tasks[0].id

        for result in losers:
            if isinstance(result, Exception):
                assert isinstance(result, ConflictError), result

    asyncio.run(_scenario())
