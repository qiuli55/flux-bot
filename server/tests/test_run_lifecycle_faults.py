"""Run 生命周期故障注入矩阵（P0-2，设计 §3.2）。

设计要求的 8 项注入场景与覆盖关系（在一个矩阵里逐条可执行；`tsup` = test_run_supervisor.py）：

| # | 场景 | 覆盖用例 |
|---|---|---|
| 1 | 启动超时 | tsup::test_startup_timeout_is_not_stuck_running |
| 2 | 空闲超时 | tsup::test_idle_timeout_after_no_progress |
| 3 | 硬超时 | tsup::test_hard_timeout_terminates_long_run |
| 4 | Cancel 升级（抗 SIGTERM） | 本文件::test_cancel_escalates_for_stubborn_process_tree |
| 5 | 属主死亡 + 孤儿进程树 | tsup::test_restart_recovery_fixes_leftover_rows |
| 6 | 进程死而 Run 非终态 | 本文件::test_dead_process_is_settled_within_one_reconcile_cycle |
| 7 | 终态竞争（迟到写入） | 本文件::test_late_finish_cannot_overwrite_cancelled |
| 8 | 幂等边界（终态再 cancel） | 本文件::test_cancel_on_terminal_run_is_idempotent |

本文件补齐现状缺口（4 / 6 / 7 / 8），全部用真实子进程与真实库，不靠 sleep 猜测。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from flux.config import Settings
from flux.core.agent_runtime import platforms
from flux.core.agent_runtime.run_repository import AgentRunRepository
from flux.core.agent_runtime.supervisor import RunSupervisor
from flux.core.event.bus import EventBus, Events
from flux.db.session import create_engine, create_session_factory
from flux.enums import DshRunStatus
from flux.models import Base

#: 极不可能存在的 PID：代表"进程已经不在了"
_DEAD_PID = 999_999

#: 忽略 SIGTERM 的进程树升级语义只在 POSIX 下成立（Windows 没有可忽略的温和信号，
#: 且探活/清理走的另一套原语，由 test_agent_runtime_platforms.py 单独覆盖）
POSIX_ONLY = pytest.mark.skipif(platforms.IS_WINDOWS, reason="POSIX 信号语义")


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    base = Settings(
        env="test",
        log_level="WARNING",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'faults.db'}",
        openai_api_key=None,
        anthropic_api_key=None,
        deepseek_api_key=None,
        default_provider="local",
        dsh_enabled=True,
        dsh_home=str(tmp_path / "dsh-home"),
        dsh_workspace=str(tmp_path / "dsh-ws"),
        dsh_mcp_enabled=False,
    )
    return base.model_copy(update=overrides) if overrides else base


async def _repo(settings: Settings) -> tuple[AgentRunRepository, AsyncEngine]:
    engine = create_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return AgentRunRepository(create_session_factory(engine)), engine


def _group_alive(pgid: int) -> bool:
    """进程组是否仍有成员（平台原语：POSIX killpg / Windows 组长进程探活）。"""
    return platforms.is_group_alive(pgid)


@POSIX_ONLY
async def test_cancel_escalates_for_stubborn_process_tree(tmp_path: Path) -> None:
    """注入 #4：子进程忽略 SIGTERM → 优雅期后升级强杀，确认进程组消失、终态 CANCELLED。

    这是设计 §3.4 的硬要求：正常 Stop 不奏效时必须能升级，且不允许"UI 已停止而进程还在"。
    """
    settings = _settings(tmp_path, dsh_cancel_grace_seconds=0.3)
    repo, engine = await _repo(settings)
    bus = EventBus()
    sup = RunSupervisor(settings=settings, repository=repo, bus=bus)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
        ],
        **platforms.popen_kwargs(),
    )
    # 后台线程立即回收：僵尸进程会让 killpg 仍然"看得到"进程组
    threading.Thread(target=proc.wait, daemon=True).start()
    try:
        await sup.create_run(run_id="r-stubborn", session_id="s", instruction="抗 SIGTERM")
        await sup.mark_starting("r-stubborn")
        await sup.mark_running("r-stubborn", pid=proc.pid, pgid=proc.pid)

        status = await sup.cancel("r-stubborn")

        assert status is DshRunStatus.CANCELLED
        for _ in range(300):
            if not _group_alive(proc.pid):
                break
            await asyncio.sleep(0.01)
        assert not _group_alive(proc.pid), "强杀后进程组必须确认消失"
        row = await repo.get("r-stubborn")
        assert row is not None
        assert row.status == DshRunStatus.CANCELLED.value
        assert Events.DSH_RUN_CANCELLED in [name for name, _ in bus.history]
    finally:
        if proc.poll() is None:
            proc.kill()
        await engine.dispose()


async def test_dead_process_is_settled_within_one_reconcile_cycle(tmp_path: Path) -> None:
    """注入 #6：进程已死但 Run 仍非终态 → 一个对账周期内落 FAILED，不留僵尸 running。"""
    settings = _settings(tmp_path)
    repo, engine = await _repo(settings)
    sup = RunSupervisor(settings=settings, repository=repo, owner_pid=os.getpid())
    await repo.create(
        run_id="r-dead",
        session_id="s",
        instruction="进程已死",
        status=DshRunStatus.RUNNING,
        owner_pid=os.getpid(),
    )
    await repo.set_process("r-dead", pid=_DEAD_PID, pgid=_DEAD_PID)

    await sup.reconcile_once()

    row = await repo.get("r-dead")
    assert row is not None
    assert row.status == DshRunStatus.FAILED.value
    assert row.finished_at is not None
    await engine.dispose()


async def test_late_finish_cannot_overwrite_cancelled(tmp_path: Path) -> None:
    """注入 #7：取消与正常完成竞争 → 先落定的 CANCELLED 不被迟到的 COMPLETED 覆盖。"""
    settings = _settings(tmp_path)
    repo, engine = await _repo(settings)
    bus = EventBus()
    sup = RunSupervisor(settings=settings, repository=repo, bus=bus)

    await sup.create_run(run_id="r-race", session_id="s", instruction="竞争")
    await sup.mark_starting("r-race")
    await sup.mark_running("r-race", pid=None, pgid=None)

    assert await sup.cancel("r-race") is DshRunStatus.CANCELLED
    # 执行线程的迟到收尾：不得改写终态、不得把结果写进库
    late = await sup.finish("r-race", DshRunStatus.COMPLETED, final_response="迟到的成功")

    assert late is DshRunStatus.CANCELLED
    row = await repo.get("r-race")
    assert row is not None
    assert row.status == DshRunStatus.CANCELLED.value
    terminal_events = [
        name
        for name, _ in bus.history
        if name in (Events.DSH_RUN_COMPLETED, Events.DSH_RUN_CANCELLED, Events.DSH_RUN_FAILED)
    ]
    assert terminal_events == [Events.DSH_RUN_CANCELLED]
    await engine.dispose()


async def test_cancel_on_terminal_run_is_idempotent(tmp_path: Path) -> None:
    """注入 #8：对已终态 Run 再 cancel → 幂等返回原终态，无任何副作用与重复事件。"""
    settings = _settings(tmp_path)
    repo, engine = await _repo(settings)
    bus = EventBus()
    sup = RunSupervisor(settings=settings, repository=repo, bus=bus)

    await sup.create_run(run_id="r-idem", session_id="s", instruction="正常完成")
    await sup.mark_starting("r-idem")
    await sup.mark_running("r-idem", pid=None, pgid=None)
    assert await sup.finish("r-idem", DshRunStatus.COMPLETED, final_response="正常完成") is (
        DshRunStatus.COMPLETED
    )

    again = await sup.cancel("r-idem")

    assert again is DshRunStatus.COMPLETED
    row = await repo.get("r-idem")
    assert row is not None
    assert row.status == DshRunStatus.COMPLETED.value
    assert row.cancel_requested is False
    assert Events.DSH_RUN_CANCELLED not in [name for name, _ in bus.history]

    # 运行中的 Run 连点两次 cancel：两次都返回 CANCELLED，只发一次取消事件
    await sup.create_run(run_id="r-idem2", session_id="s", instruction="连点取消")
    await sup.mark_starting("r-idem2")
    await sup.mark_running("r-idem2", pid=None, pgid=None)
    first = await sup.cancel("r-idem2")
    second = await sup.cancel("r-idem2")
    assert first is DshRunStatus.CANCELLED
    assert second is DshRunStatus.CANCELLED
    assert [name for name, _ in bus.history].count(Events.DSH_RUN_CANCELLED) == 1
    await engine.dispose()
