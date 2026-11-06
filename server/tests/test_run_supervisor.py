"""Run 生命周期看护的回归测试（修复方案 §2 / §3：TC-15A~F）。

覆盖六条硬判据：
- TC-15A startup timeout：启动卡死不得永久 running；
- TC-15B idle timeout：存活但长期无有效进展要被判定超时；
- TC-15C hard timeout：绝对上限到点即 TIMEOUT；
- TC-15D cancel：真实进程组的整棵树被清理，落 CANCELLED；
- TC-15E 卡住的 CANCELLING 被自行收尾，不留永久中间态；
- TC-15F Flux 重启后按真实进程/属主状态对账修正遗留行；
- TC-15G running 僵尸任务对账（P2-04 / TC-502）：没有活 Run 支撑的任务被收尾，不再永久 running。

除 TC-15D 用真实子进程外，其余用例注入可控时钟，不靠 sleep 造成 flaky。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from flux.config import Settings
from flux.container import Container
from flux.core.agent_runtime import platforms
from flux.core.agent_runtime.run_repository import AgentRunRepository, utcnow
from flux.core.agent_runtime.supervisor import RunSupervisor
from flux.core.event.bus import EventBus, Events
from flux.core.task_engine import dsh_bridge
from flux.core.task_engine.dsh_bridge import TaskRunBridge
from flux.core.task_engine.repository import TaskRepository
from flux.db.session import create_engine, create_session_factory
from flux.enums import BrainSection, DshRunStatus, TaskStatus
from flux.models import Base


class _Clock:
    """可控单调时钟：手动推进，避免用真实 sleep 等待超时。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    base = Settings(
        env="test",
        log_level="WARNING",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'supervisor.db'}",
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


async def test_startup_timeout_is_not_stuck_running(tmp_path: Path) -> None:
    """TC-15A：PENDING/STARTING 超过 startup 上限 → TIMEOUT（cleanup 一并完成）。"""
    clock = _Clock()
    settings = _settings(
        tmp_path,
        dsh_startup_timeout_seconds=0.5,
        dsh_idle_timeout_seconds=0,
        dsh_hard_timeout_seconds=0,
    )
    repo, engine = await _repo(settings)
    bus = EventBus()
    sup = RunSupervisor(settings=settings, repository=repo, bus=bus, clock=clock)

    await sup.create_run(run_id="r-a", session_id="s-a", instruction="起不来")
    clock.now += 1.0
    await sup.heartbeat_once()

    row = await repo.get("r-a")
    assert row is not None
    assert row.status == DshRunStatus.TIMEOUT.value
    assert row.timeout_kind == "startup"
    assert "启动超时" in (row.error or "")
    assert row.finished_at is not None
    assert sup.monitor_snapshot("r-a")["terminal"] is True  # type: ignore[index]
    assert Events.DSH_RUN_TIMEOUT in [name for name, _ in bus.history]
    await engine.dispose()


async def test_idle_timeout_after_no_progress(tmp_path: Path) -> None:
    """TC-15B：有输出重置计时；长时间无进展才判 idle 超时。"""
    clock = _Clock()
    settings = _settings(
        tmp_path,
        dsh_startup_timeout_seconds=0,
        dsh_idle_timeout_seconds=0.5,
        dsh_hard_timeout_seconds=0,
    )
    repo, engine = await _repo(settings)
    sup = RunSupervisor(settings=settings, repository=repo, clock=clock)

    await sup.create_run(run_id="r-b", session_id="s-b", instruction="慢慢来")
    await sup.mark_starting("r-b")
    await sup.mark_running("r-b", pid=None, pgid=None)

    clock.now += 0.4
    await sup.note_output("r-b", {"event_type": "assistant/message"})
    clock.now += 0.4  # 距上次有效进展 0.4s < 0.5s，不该超时
    await sup.heartbeat_once()
    assert (await repo.get("r-b")).status == DshRunStatus.RUNNING.value  # type: ignore[union-attr]

    clock.now += 0.3  # 合计 0.7s 无进展 → idle 超时
    await sup.heartbeat_once()
    row = await repo.get("r-b")
    assert row is not None
    assert row.status == DshRunStatus.TIMEOUT.value
    assert row.timeout_kind == "idle"
    assert "空闲超时" in (row.error or "")
    await engine.dispose()


async def test_hard_timeout_terminates_long_run(tmp_path: Path) -> None:
    """TC-15C：无论是否有进展，超过绝对上限即 TIMEOUT。"""
    clock = _Clock()
    settings = _settings(
        tmp_path,
        dsh_startup_timeout_seconds=0,
        dsh_idle_timeout_seconds=0,
        dsh_hard_timeout_seconds=0.5,
    )
    repo, engine = await _repo(settings)
    sup = RunSupervisor(settings=settings, repository=repo, clock=clock)

    await sup.create_run(run_id="r-c", session_id="s-c", instruction="跑太久")
    await sup.mark_starting("r-c")
    await sup.mark_running("r-c", pid=None, pgid=None)
    clock.now += 0.6
    await sup.heartbeat_once()

    row = await repo.get("r-c")
    assert row is not None
    assert row.status == DshRunStatus.TIMEOUT.value
    assert row.timeout_kind == "hard"
    assert "hard" in (row.error or "")
    await engine.dispose()


async def test_cancel_cleans_the_whole_process_group(tmp_path: Path) -> None:
    """TC-15D：取消后主进程（进程组组长）确实退出，终态为 CANCELLED。

    进程用平台原语启动：POSIX 下自成会话（pid == pgid），Windows 下 CREATE_NEW_PROCESS_GROUP
    （组标识同样是组长 pid）。探活用 `platforms.is_alive`——Windows 的 `os.kill(pid, 0)`
    会抛 ValueError（只支持 SIGTERM 与 CTRL_* 事件），旧写法在 Windows 上根本跑不了。
    """
    settings = _settings(tmp_path, dsh_cancel_grace_seconds=0.3)
    repo, engine = await _repo(settings)
    sup = RunSupervisor(settings=settings, repository=repo)

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        **platforms.popen_kwargs(),
    )
    # 后台线程立即回收，避免僵尸进程让进程组探活仍"看得到"它
    threading.Thread(target=proc.wait, daemon=True).start()
    try:
        await sup.create_run(run_id="r-d", session_id="s-d", instruction="干很久")
        await sup.mark_starting("r-d")
        await sup.mark_running("r-d", pid=proc.pid, pgid=proc.pid)

        status = await sup.cancel("r-d")
        assert status is DshRunStatus.CANCELLED
        for _ in range(300):
            if not platforms.is_alive(proc.pid):
                break
            await asyncio.sleep(0.01)
        assert not platforms.is_alive(proc.pid)
        assert not platforms.is_group_alive(proc.pid)
        row = await repo.get("r-d")
        assert row is not None and row.status == DshRunStatus.CANCELLED.value
    finally:
        if proc.poll() is None:
            proc.kill()
        await engine.dispose()


async def test_stuck_cancelling_is_self_healed(tmp_path: Path) -> None:
    """TC-15E：CANCELLING 卡住超过阈值 → 自行收尾为 CANCELLED，绝不永久停留。"""
    clock = _Clock()
    settings = _settings(
        tmp_path,
        dsh_startup_timeout_seconds=0,
        dsh_idle_timeout_seconds=0,
        dsh_hard_timeout_seconds=0,
        dsh_cancel_grace_seconds=0,
    )
    repo, engine = await _repo(settings)
    sup = RunSupervisor(settings=settings, repository=repo, clock=clock)

    await sup.create_run(run_id="r-e", session_id="s-e", instruction="卡在取消")
    await sup.mark_starting("r-e")
    await sup.mark_running("r-e", pid=None, pgid=None)
    # 白盒构造"取消流程被掐断"的中间态：进程内监视器停在 CANCELLING 且久未变化
    monitor = sup._monitors["r-e"]
    monitor.status = DshRunStatus.CANCELLING
    monitor.cancel_requested = True
    monitor.last_state_mono = clock.now
    clock.now += 100.0
    await sup.heartbeat_once()

    assert (await repo.get("r-e")).status == DshRunStatus.CANCELLED.value  # type: ignore[union-attr]
    await engine.dispose()


async def test_restart_recovery_fixes_leftover_rows(tmp_path: Path) -> None:
    """TC-15F：重启后按真实进程/属主修正遗留非终态行，不留僵尸 running。"""
    settings = _settings(
        tmp_path,
        dsh_startup_timeout_seconds=0,
        dsh_idle_timeout_seconds=0,
        dsh_hard_timeout_seconds=0,
    )
    repo, engine = await _repo(settings)
    sup = RunSupervisor(settings=settings, repository=repo, owner_pid=os.getpid())

    dead_owner = 999_999  # 极不可能存在的 PID：属主已死的孤儿 Run
    await repo.create(
        run_id="r-f1",
        session_id="s-f1",
        instruction="孤儿",
        status=DshRunStatus.RUNNING,
        owner_pid=dead_owner,
    )
    # 属主就是本进程（活着），但 Run 的进程已不存在 → 对账判 FAILED
    await repo.create(
        run_id="r-f2",
        session_id="s-f2",
        instruction="属主在但进程没了",
        status=DshRunStatus.RUNNING,
        owner_pid=os.getpid(),
    )

    recovered = await sup.startup_recovery()
    assert set(recovered) == {"r-f1", "r-f2"}
    r1 = await repo.get("r-f1")
    r2 = await repo.get("r-f2")
    assert r1 is not None and r1.status == DshRunStatus.INTERRUPTED.value
    assert r2 is not None and r2.status == DshRunStatus.FAILED.value
    await engine.dispose()


# --- TC-15G：running 僵尸任务对账（P2-04 / TC-502）---


async def _task_env(
    tmp_path: Path, **overrides: object
) -> tuple[TaskRepository, AgentRunRepository, AsyncEngine]:
    settings = _settings(tmp_path, **overrides)
    engine = create_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    return TaskRepository(factory), AgentRunRepository(factory), engine


async def _make_running_task(tasks: TaskRepository, description: str) -> uuid.UUID:
    task = await tasks.create(
        task_id=uuid.uuid4(),
        description=description,
        priority=1,
        project_id=None,
        agent_id=None,
    )
    await tasks.set_status(task.id, TaskStatus.RUNNING)
    return task.id


def _age_orphan(monkeypatch: pytest.MonkeyPatch) -> None:
    """把宽限期清零，等价于"这条任务已经安静足够久"。"""
    monkeypatch.setattr(dsh_bridge, "_ORPHAN_GRACE_SECONDS", 0.0)


async def test_orphan_task_without_run_returns_to_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_id 为空 = 从没有 Run 被起过：running 不成立，退回 pending 可再执行。"""
    _age_orphan(monkeypatch)
    tasks, runs, engine = await _task_env(tmp_path)
    task_id = await _make_running_task(tasks, "给项目增加用户登录功能")
    bridge = TaskRunBridge(tasks, runs)

    fixed = await bridge.reconcile_orphan_tasks()

    assert fixed == [str(task_id)]
    assert (await tasks.get(task_id)).status == TaskStatus.PENDING.value
    await engine.dispose()


async def test_orphan_task_with_terminal_run_is_settled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_id 指向已终态的 Run：事件丢了，按 Run 真实终态补落任务终态。"""
    _age_orphan(monkeypatch)
    tasks, runs, engine = await _task_env(tmp_path)
    task_id = await _make_running_task(tasks, "给项目补一个 /ping 接口")
    run_id = "run-orphan-1"
    await runs.create(
        run_id=run_id,
        session_id="s",
        instruction="i",
        status=DshRunStatus.RUNNING,
        owner_pid=os.getpid(),
        started_at=utcnow(),
    )
    await runs.set_status(run_id, DshRunStatus.FAILED, error="runtime 崩溃", finished=True)
    await tasks.set_run_id(task_id, run_id)
    bridge = TaskRunBridge(tasks, runs)

    fixed = await bridge.reconcile_orphan_tasks()

    assert fixed == [str(task_id)]
    assert (await tasks.get(task_id)).status == TaskStatus.FAILED.value
    await engine.dispose()


async def test_orphan_task_with_live_run_is_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run 还活着（非终态）：任务确实在执行，对账不得动它。"""
    _age_orphan(monkeypatch)
    tasks, runs, engine = await _task_env(tmp_path)
    task_id = await _make_running_task(tasks, "给项目增加健康检查")
    run_id = "run-live-1"
    await runs.create(
        run_id=run_id,
        session_id="s",
        instruction="i",
        status=DshRunStatus.RUNNING,
        owner_pid=os.getpid(),
        started_at=utcnow(),
    )
    await tasks.set_run_id(task_id, run_id)
    bridge = TaskRunBridge(tasks, runs)

    assert await bridge.reconcile_orphan_tasks() == []
    assert (await tasks.get(task_id)).status == TaskStatus.RUNNING.value
    await engine.dispose()


# --- 上下文闭环：任务收尾自动沉淀到项目 Brain ---


async def test_completed_task_is_sedimented_into_project_brain(tmp_path: Path) -> None:
    """Run 终态 → 任务终态时，把"这轮做了什么"写进 Project Brain（同一 Run 幂等）。"""
    container = Container(_settings(tmp_path))
    try:
        async with container.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        project = await container.brain.create_project(name="登录服务")
        task = await container.task_repo.create(
            task_id=uuid.uuid4(),
            description="给登录服务加验证码",
            priority=1,
            project_id=project.id,
            agent_id=None,
        )
        run_id = "run-sediment-1"
        await container.run_repo.create(
            run_id=run_id,
            session_id="s",
            instruction="给登录服务加验证码",
            status=DshRunStatus.RUNNING,
            task_id=task.id,
        )
        await container.task_repo.set_run_id(task.id, run_id)
        await container.task_repo.set_status(task.id, TaskStatus.RUNNING)
        bridge = TaskRunBridge(
            container.task_repo,
            container.run_repo,
            brain=container.brain,
            workspace=container.workspace,
        )

        await bridge._on_run_settled(
            Events.DSH_RUN_COMPLETED,
            {"run_id": run_id, "final_response": "已提交 2 个提案"},
        )

        notes = (await container.brain.sections(project.id))[BrainSection.AGENT_NOTES.value]
        assert len(notes) == 1
        assert notes[0]["metadata"]["run_id"] == run_id
        assert "给登录服务加验证码" in notes[0]["content"]
        assert (await container.task_repo.get(task.id)).status == TaskStatus.COMPLETED.value

        # 同一 Run 重复沉淀不新增第二条（事件重复投递 / 直接重调都幂等）
        await bridge._sediment(task, status=TaskStatus.COMPLETED, run_id=run_id, body={})
        again = (await container.brain.sections(project.id))[BrainSection.AGENT_NOTES.value]
        assert len(again) == 1
    finally:
        await container.dispose()


# --- 上游瞬时故障（429 / ReadTimeout）自动重试 ---


async def _retry_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, delay: float = 0.0
) -> tuple[
    TaskRepository,
    AgentRunRepository,
    AsyncEngine,
    TaskRunBridge,
    uuid.UUID,
    str,
    list[uuid.UUID],
]:
    """一套「任务已在跑、Run 刚失败」的环境，handler 只记录被调用的任务。"""
    monkeypatch.setattr(dsh_bridge, "_AUTO_RETRY_DELAY_SECONDS", delay)
    tasks, runs, engine = await _task_env(tmp_path)
    task_id = await _make_running_task(tasks, "给项目补一个 /ping 接口")
    run_id = "run-retry-1"
    await runs.create(
        run_id=run_id,
        session_id="s",
        instruction="i",
        status=DshRunStatus.RUNNING,
        owner_pid=os.getpid(),
        started_at=utcnow(),
    )
    await tasks.set_run_id(task_id, run_id)
    bridge = TaskRunBridge(tasks, runs)
    calls: list[uuid.UUID] = []

    async def _handler(task: object) -> None:
        calls.append(task.id)  # type: ignore[attr-defined]

    bridge.set_retry_handler(_handler)
    return tasks, runs, engine, bridge, task_id, run_id, calls


async def _drain_retries(bridge: TaskRunBridge) -> None:
    while bridge._retry_jobs:
        await asyncio.gather(*list(bridge._retry_jobs))


async def test_transient_upstream_failure_auto_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """429 触发的失败不落终态：任务保持 running，写一条重试提示，冷却后重新起一轮。"""
    tasks, _runs, engine, bridge, task_id, run_id, calls = await _retry_env(tmp_path, monkeypatch)

    await bridge._on_run_settled(
        Events.DSH_RUN_FAILED,
        {
            "run_id": run_id,
            "error": "AI_APICallError: Too Many Requests",
            "finish_reason": "upstream_error",
        },
    )

    assert (await tasks.get(task_id)).status == TaskStatus.RUNNING.value
    messages, _ = await tasks.list_messages(task_id)
    assert [message.kind for message in messages] == ["run_retry"]

    await _drain_retries(bridge)
    assert calls == [task_id]
    await engine.dispose()


async def test_read_timeout_is_treated_as_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """流挂住后 Flux 侧报的 ReadTimeout 同样是瞬时故障，不能算任务失败。"""
    tasks, _runs, engine, bridge, task_id, run_id, calls = await _retry_env(tmp_path, monkeypatch)

    await bridge._on_run_settled(
        Events.DSH_RUN_FAILED,
        {
            "run_id": run_id,
            "error": "httpx.ReadTimeout: timed out",
            "finish_reason": "upstream_error",
        },
    )

    assert (await tasks.get(task_id)).status == TaskStatus.RUNNING.value
    await _drain_retries(bridge)
    assert calls == [task_id]
    await engine.dispose()


async def test_non_transient_failure_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """业务失败（鉴权、配置）照常落 FAILED；错误里的普通数字不触发重试。"""
    tasks, _runs, engine, bridge, task_id, run_id, calls = await _retry_env(tmp_path, monkeypatch)

    await bridge._on_run_settled(
        Events.DSH_RUN_FAILED,
        {"run_id": run_id, "error": "鉴权失败：invalid api key (step 4291)", "finish_reason": "1"},
    )

    assert (await tasks.get(task_id)).status == TaskStatus.FAILED.value
    messages, _ = await tasks.list_messages(task_id)
    assert [message.kind for message in messages] == ["run_finished"]
    await _drain_retries(bridge)
    assert calls == []
    await engine.dispose()


async def test_retry_budget_stops_after_second_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每轮失败链只自动重试一次：重试后仍被限流就直接落 FAILED，不无限重试。"""
    tasks, _runs, engine, bridge, task_id, run_id, calls = await _retry_env(tmp_path, monkeypatch)
    failure = {
        "error": "AI_APICallError: status 429",
        "finish_reason": "upstream_error",
    }

    await bridge._on_run_settled(Events.DSH_RUN_FAILED, {"run_id": run_id, **failure})
    await _drain_retries(bridge)
    assert calls == [task_id]

    # 重试那一轮重新登记了新的 run_id，又失败一次：这次必须收尾
    second_run = "run-retry-2"
    await tasks.set_run_id(task_id, second_run)
    await bridge._on_run_settled(Events.DSH_RUN_FAILED, {"run_id": second_run, **failure})
    await _drain_retries(bridge)

    assert calls == [task_id]
    assert (await tasks.get(task_id)).status == TaskStatus.FAILED.value
    await engine.dispose()


async def test_cancelled_task_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户取消是明确意图，不在自动重试范围。"""
    tasks, _runs, engine, bridge, task_id, run_id, calls = await _retry_env(tmp_path, monkeypatch)

    await bridge._on_run_settled(
        Events.DSH_RUN_CANCELLED, {"run_id": run_id, "error": "cancelled by user"}
    )
    await _drain_retries(bridge)

    assert calls == []
    assert (await tasks.get(task_id)).status == TaskStatus.CANCELLED.value
    await engine.dispose()


async def test_orphan_reconcile_skips_task_waiting_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """冷却期内 run_id 仍指向已失败的 Run：对账不能把它当孤儿判失败。"""
    _age_orphan(monkeypatch)
    tasks, runs, engine, bridge, task_id, run_id, calls = await _retry_env(
        tmp_path, monkeypatch, delay=1.0
    )
    await bridge._on_run_settled(
        Events.DSH_RUN_FAILED, {"run_id": run_id, "error": "Too Many Requests"}
    )
    await runs.set_status(run_id, DshRunStatus.FAILED, error="Too Many Requests", finished=True)

    assert await bridge.reconcile_orphan_tasks() == []
    assert (await tasks.get(task_id)).status == TaskStatus.RUNNING.value

    await _drain_retries(bridge)
    assert calls == [task_id]
    await engine.dispose()


async def test_retry_is_abandoned_when_task_stops_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """冷却期内用户取消/改状态：到点后发现任务已不在 running，放弃重试。"""
    tasks, _runs, engine, bridge, task_id, run_id, calls = await _retry_env(
        tmp_path, monkeypatch, delay=0.05
    )

    await bridge._on_run_settled(
        Events.DSH_RUN_FAILED, {"run_id": run_id, "error": "Too Many Requests"}
    )
    await tasks.set_status(task_id, TaskStatus.CANCELLED)
    await _drain_retries(bridge)

    assert calls == []
    assert (await tasks.get(task_id)).status == TaskStatus.CANCELLED.value
    await engine.dispose()


async def test_reconcile_hook_runs_after_row_reconcile(tmp_path: Path) -> None:
    """对账钩子在 Run 行对账之后执行：下游拿到的已是修正后的真实状态。"""
    settings = _settings(tmp_path)
    repo, engine = await _repo(settings)
    await repo.create(
        run_id="r-hook",
        session_id="s",
        instruction="遗留",
        status=DshRunStatus.RUNNING,
        owner_pid=os.getpid(),
    )
    sup = RunSupervisor(settings=settings, repository=repo)
    seen: list[str] = []

    async def _hook() -> None:
        row = await repo.get("r-hook")
        seen.append(row.status if row is not None else "missing")

    sup.add_reconcile_hook(_hook)
    await sup.reconcile_once()

    assert seen == [DshRunStatus.FAILED.value]
    await engine.dispose()
