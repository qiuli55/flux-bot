"""Per-Bot schedule timezone and missed-run policy."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from flux.container import Container
from flux.core.agent_runtime.runtimes.service import TaskRunService
from flux.enums import TaskStatus

PREFIX = "/api/v1"
UTC = timezone.utc


@pytest.fixture(autouse=True)
def quiet_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """调度测试不真正起 Run，任务停在 pending 即可。"""

    async def _noop(self: TaskRunService, task: object, *args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(TaskRunService, "start", _noop)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _create(client: TestClient, **overrides: object) -> dict:
    payload: dict[str, object] = {"name": "调度测试", "description": "检查磁盘"}
    payload.update(overrides)
    response = client.post(f"{PREFIX}/bots", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_bot_timezone_overrides_process_timezone(client: TestClient) -> None:
    bot = _create(client, schedule="0 9 * * *", timezone="Asia/Shanghai")
    assert bot["timezone"] == "Asia/Shanghai"
    local = _as_utc(datetime.fromisoformat(bot["next_run_at"])).astimezone(
        ZoneInfo("Asia/Shanghai")
    )
    assert (local.hour, local.minute) == (9, 0)


def test_patch_timezone_shifts_next_run_from_new_wall_clock(client: TestClient) -> None:
    bot = _create(client, schedule="0 9 * * *", timezone="Asia/Shanghai")
    before = _as_utc(datetime.fromisoformat(bot["next_run_at"]))
    assert before.astimezone(ZoneInfo("Asia/Shanghai")).hour == 9

    patched = client.patch(f"{PREFIX}/bots/{bot['id']}", json={"timezone": "America/New_York"})
    assert patched.status_code == 200, patched.text
    data = patched.json()["data"]
    assert data["timezone"] == "America/New_York"
    after = _as_utc(datetime.fromisoformat(data["next_run_at"]))
    assert after.astimezone(ZoneInfo("America/New_York")).hour == 9
    assert after.hour != before.hour


def test_empty_timezone_inherits_and_invalid_values_are_rejected(client: TestClient) -> None:
    bot = _create(client, timezone="   ")
    assert bot["timezone"] == "UTC"
    assert bot["missed_run_policy"] == "skip"

    assert (
        client.post(f"{PREFIX}/bots", json={"name": "坏时区", "timezone": "Not/AZone"}).status_code
        == 400
    )
    assert (
        client.patch(f"{PREFIX}/bots/{bot['id']}", json={"timezone": "Not/AZone"}).status_code
        == 400
    )
    assert (
        client.post(
            f"{PREFIX}/bots", json={"name": "坏策略", "missed_run_policy": "bogus"}
        ).status_code
        == 400
    )
    assert (
        client.patch(f"{PREFIX}/bots/{bot['id']}", json={"missed_run_policy": "bogus"}).status_code
        == 400
    )


def test_routine_inherits_parent_bot_timezone(container: Container) -> None:
    async def _scenario() -> None:
        bot = await container.bots.create(
            name="时区例程",
            description="检查磁盘",
            schedule="0 9 * * *",
            timezone="Asia/Shanghai",
        )
        routine = await container.bots.create_routine(
            bot.id, name="日报", instruction="写日报", schedule="0 9 * * *"
        )
        local = _as_utc(routine.next_run_at).astimezone(ZoneInfo("Asia/Shanghai"))
        assert (local.hour, local.minute) == (9, 0)

        await container.bots.update(bot.id, {"timezone": "America/New_York"})
        refreshed = (await container.bots.list_routines(bot.id))[0]
        local = _as_utc(refreshed.next_run_at).astimezone(ZoneInfo("America/New_York"))
        assert (local.hour, local.minute) == (9, 0)

    asyncio.run(_scenario())


def test_skip_silent_still_launches_on_time(container: Container) -> None:
    async def _scenario() -> None:
        bot = await container.bots.create(
            name="准点仍要跑",
            description="检查磁盘",
            schedule="*/10 * * * *",
            missed_run_policy="skip_silent",
        )
        due = _as_utc(bot.next_run_at)
        started = await container.bots.tick(due)
        assert len(started) == 1
        refreshed = await container.bots.get(bot.id)
        assert refreshed.last_task_id is not None
        assert _as_utc(refreshed.next_run_at) > due

    asyncio.run(_scenario())


def test_skip_silent_does_not_launch_missed_slots(container: Container) -> None:
    async def _scenario() -> None:
        bot = await container.bots.create(
            name="静默跳过",
            description="检查磁盘",
            schedule="*/10 * * * *",
            missed_run_policy="skip_silent",
        )
        due = _as_utc(bot.next_run_at)
        # 已经错过至少一个后续槽位，才静默跳过。
        moment = due + timedelta(minutes=15)
        started = await container.bots.tick(moment)
        assert started == []

        refreshed = await container.bots.get(bot.id)
        assert refreshed.last_task_id is None
        assert refreshed.next_run_at is not None
        assert _as_utc(refreshed.next_run_at) > moment

    asyncio.run(_scenario())


def test_catch_up_advances_to_next_overdue_slot(container: Container) -> None:
    async def _scenario() -> None:
        bot = await container.bots.create(
            name="逐个补跑",
            description="检查磁盘",
            schedule="*/10 * * * *",
            missed_run_policy="catch_up",
        )
        first_due = _as_utc(bot.next_run_at)
        moment = first_due + timedelta(minutes=25)
        started = await container.bots.tick(moment)
        assert len(started) == 1

        refreshed = await container.bots.get(bot.id)
        assert _as_utc(refreshed.next_run_at) == first_due + timedelta(minutes=10)

        # 完成上一轮后下一轮 tick 继续补下一个错过槽位，仍然是一轮一次。
        await container.task_repo.set_status(uuid.UUID(started[0]), TaskStatus.COMPLETED)
        assert len(await container.bots.tick(moment)) == 1
        refreshed = await container.bots.get(bot.id)
        assert _as_utc(refreshed.next_run_at) == first_due + timedelta(minutes=20)

    asyncio.run(_scenario())


def test_catch_up_caps_large_backlog(container: Container) -> None:
    async def _scenario() -> None:
        bot = await container.bots.create(
            name="补跑上限",
            description="检查磁盘",
            schedule="*/10 * * * *",
            missed_run_policy="catch_up",
        )
        moment = _as_utc(bot.next_run_at) + timedelta(days=1)
        started = await container.bots.tick(moment)
        assert len(started) == 1

        refreshed = await container.bots.get(bot.id)
        assert _as_utc(refreshed.next_run_at) > moment

    asyncio.run(_scenario())
