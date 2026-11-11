"""Bot 时区配置：cron / 每日预算按 FLUX_TIMEZONE 解释（M5）。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from flux.config import Settings
from flux.container import Container
from flux.core.agent_runtime.runtimes.service import TaskRunService
from flux.core.bot.cron import parse_cron
from flux.core.bot.service import _day_start
from flux.enums import TaskStatus

UTC = timezone.utc


@pytest.fixture(autouse=True)
def quiet_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """本文件只验证调度/预算口径，起 Run 换成空实现。"""

    async def _noop(self: TaskRunService, task: object, *args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(TaskRunService, "start", _noop)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def test_cron_in_shanghai_fires_at_utc_0100() -> None:
    schedule = parse_cron("0 9 * * *", tz="Asia/Shanghai")
    assert schedule.next_after(datetime(2026, 1, 1, 0, 0, tzinfo=UTC)) == datetime(
        2026, 1, 1, 1, 0, tzinfo=UTC
    )


def test_cron_defaults_to_utc_and_supports_override() -> None:
    # 不传时区保持旧行为：字段按 UTC 解释。
    assert parse_cron("0 9 * * *").next_after(datetime(2026, 1, 1, 0, 0, tzinfo=UTC)) == datetime(
        2026, 1, 1, 9, 0, tzinfo=UTC
    )
    # next_after 也可临时覆盖解析时绑定的时区。
    assert parse_cron("0 9 * * *").next_after(
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC), tz="Asia/Shanghai"
    ) == datetime(2026, 1, 1, 1, 0, tzinfo=UTC)


def test_dst_spring_forward_skips_nonexistent_local_time() -> None:
    # 2026-03-08 02:00 America/New_York 跳到 03:00，02:30 不存在，顺延到 03-09。
    schedule = parse_cron("30 2 * * *", tz="America/New_York")
    assert schedule.next_after(datetime(2026, 3, 7, 12, 0, tzinfo=UTC)) == datetime(
        2026, 3, 9, 6, 30, tzinfo=UTC
    )


def test_dst_fall_back_fires_once_at_first_occurrence() -> None:
    # 2026-11-01 01:30 在纽约出现两次；只按 fold=0（EDT）触发第一次。
    schedule = parse_cron("30 1 * * *", tz="America/New_York")
    first = schedule.next_after(datetime(2026, 10, 31, 12, 0, tzinfo=UTC))
    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    # 第二次 01:30 EST（06:30 UTC）不再触发，直接进入次日 01:30 EST。
    assert schedule.next_after(first) == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)


def test_day_start_is_local_midnight_in_shanghai() -> None:
    # 2026-01-01 08:30 上海 = 2026-01-01 00:30 UTC；当日零点 = 2025-12-31 16:00 UTC。
    assert _day_start(
        datetime(2026, 1, 1, 0, 30, tzinfo=UTC), ZoneInfo("Asia/Shanghai")
    ) == datetime(2025, 12, 31, 16, 0, tzinfo=UTC)


def test_settings_validates_timezone_and_mentions_env_var() -> None:
    assert Settings(timezone="Asia/Shanghai").timezone == "Asia/Shanghai"
    with pytest.raises(ValidationError, match="FLUX_TIMEZONE"):
        Settings(timezone="Not/AZone")


def test_settings_falls_back_to_valid_tz_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLUX_TIMEZONE", raising=False)
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    assert Settings().timezone == "Asia/Shanghai"
    # 非法 TZ 不参与回退，保持默认 UTC。
    monkeypatch.setenv("TZ", "Not/AZone")
    assert Settings().timezone == "UTC"


def test_explicit_flux_timezone_wins_over_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLUX_TIMEZONE", "America/New_York")
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    assert Settings().timezone == "America/New_York"


def test_bot_and_routine_schedule_use_configured_timezone(
    settings: Settings, db_schema: None
) -> None:
    shanghai = ZoneInfo("Asia/Shanghai")

    async def _scenario() -> None:
        container = Container(settings.model_copy(update={"timezone": "Asia/Shanghai"}))
        try:
            bot = await container.bots.create(
                name="上海定时", description="检查磁盘", schedule="0 9 * * *"
            )
            local = _as_utc(bot.next_run_at).astimezone(shanghai)
            assert (local.hour, local.minute) == (9, 0)

            routine = await container.bots.create_routine(
                bot.id, name="上海例程", instruction="写日报", schedule="0 9 * * *"
            )
            local_routine = _as_utc(routine.next_run_at).astimezone(shanghai)
            assert (local_routine.hour, local_routine.minute) == (9, 0)
        finally:
            await container.dispose()

    asyncio.run(_scenario())


def test_daily_budget_window_uses_local_day(settings: Settings, db_schema: None) -> None:
    """上海本地零点切换预算窗口：UTC 前一天的用量不计入新一天。"""

    async def _scenario() -> None:
        container = Container(settings.model_copy(update={"timezone": "Asia/Shanghai"}))
        try:
            bot = await container.bots.create(
                name="上海预算", description="检查磁盘", token_budget=100
            )
            task = await container.bots.run_now(bot.id)
            await container.task_repo.set_status(task.id, TaskStatus.COMPLETED)

            # 2025-12-31 15:30 UTC = 上海 23:30，属于 12-31 当天。
            await container.billing.record_step(
                run_id="old-day",
                model="gpt-5",
                input_tokens=80,
                task_id=str(task.id),
                occurred_at=datetime(2025, 12, 31, 15, 30, tzinfo=UTC),
            )
            # 2025-12-31 16:30 UTC = 上海 2026-01-01 00:30，属于新一天。
            await container.billing.record_step(
                run_id="new-day",
                model="gpt-5",
                input_tokens=30,
                task_id=str(task.id),
                occurred_at=datetime(2025, 12, 31, 16, 30, tzinfo=UTC),
            )

            now = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)  # 上海 08:30
            assert (await container.bots.tokens_used_today(now=now)).get(bot.id) == 30
        finally:
            await container.dispose()

    asyncio.run(_scenario())
