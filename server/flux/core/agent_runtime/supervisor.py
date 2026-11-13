"""Run 生命周期看护（修复方案 §2：P2-15）。

Agent Run 以前只有"起一次、等它自己结束"：Provider 无响应就一直 running，用户取消后
子进程还活着，Flux 重启后状态也没人对账。本模块把这些收进一个统一看护器：

```text
create → STARTING → RUNNING ─┬─ COMPLETED
                             ├─ FAILED
                             ├─ TIMEOUT（startup / idle / hard）
                             ├─ CANCELLING → CANCELLED（进程树确认清理完成）
                             └─ INTERRUPTED（Flux 重启接管遗留 Run）
```

三条硬规则：

1. **CANCELLED 表示进程树已确认清理**，不是"收到了取消请求"；清理不掉就报 FAILED，
   绝不把残留进程伪装成取消成功（§2.5）。
2. **心跳不等于"stdout 有输出"**：状态变化、事件到达、MCP 工具调用都算有效进展（§2.4）。
3. **对账以真实进程为准**：Reconciler 与重启恢复只处理"本进程拥有的行"或"属主进程已死
   的行"，多实例共享同一个数据库时不误伤别人正在跑的 Run（§2.6 / §2.7）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from flux.config import Settings
from flux.core.agent_runtime.platforms import (
    is_alive,
    is_group_alive,
    is_self_group,
    kill_group,
    kill_process,
    signal_group_graceful,
    signal_process_graceful,
)
from flux.core.agent_runtime.run_repository import (
    TERMINAL_RUN_STATUSES,
    AgentRunRepository,
    is_terminal,
)
from flux.core.event.bus import EventBus, Events
from flux.enums import DshRunStatus
from flux.logging import get_logger
from flux.models.agent_run import AgentRun

if TYPE_CHECKING:  # 仅类型标注：直接导入 runtimes 包会触发 cli ↔ supervisor 循环
    from flux.core.agent_runtime.runtimes.base import RuntimeLimits

logger = get_logger(__name__)

#: 终态 → 对外发布的事件名
EVENT_BY_STATUS: dict[DshRunStatus, str] = {
    DshRunStatus.COMPLETED: Events.DSH_RUN_COMPLETED,
    DshRunStatus.FAILED: Events.DSH_RUN_FAILED,
    DshRunStatus.CANCELLED: Events.DSH_RUN_CANCELLED,
    DshRunStatus.TIMEOUT: Events.DSH_RUN_TIMEOUT,
    DshRunStatus.INTERRUPTED: Events.DSH_RUN_INTERRUPTED,
}

#: SIGKILL 之后的确认等待（秒）
_KILL_CONFIRM_SECONDS = 2.0
#: 等待"进程登记 / Run 收尾"的轮询步长（秒）
_POLL_STEP = 0.01
#: "有进展"写库的最小间隔（秒）：事件可能成百上千，内存判定不受此限制
_OUTPUT_PERSIST_INTERVAL = 1.0


class _NoopRunRepository:
    """未装配数据库时的空实现：单测直接构造 RunSupervisor 也不该被迫建表。

    生产路径（Container）永远装配真实仓储；空实现只让纯内存用例少一层依赖。
    """

    async def create(self, **_kwargs: object) -> None:
        return None

    async def get(self, _run_id: str) -> None:
        return None

    async def list_non_terminal(self) -> list[AgentRun]:
        return []

    async def set_status(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def set_process(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def request_cancel(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def touch_heartbeat(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def touch_output(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def touch_mcp_activity(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def set_last_event(self, *_args: object, **_kwargs: object) -> None:
        return None


@dataclass
class _Monitor:
    """本进程正在看护的一次 Run（进程内状态；权威状态仍在数据库）。"""

    run_id: str
    status: DshRunStatus
    created_mono: float
    last_state_mono: float
    last_output_mono: float
    last_mcp_mono: float
    agent_id: str | None = None
    started_mono: float | None = None
    pid: int | None = None
    pgid: int | None = None
    cancel_requested: bool = False
    terminal: bool = False
    #: 上一次把"有进展"写进库的单调时刻（事件可能成百上千，落库要节流）
    last_output_persist_mono: float = 0.0
    #: 运行时的优雅中断回调（Claude 的 SIGINT / 各 CLI 的 cancel）；未登记前为 None
    interrupt: Callable[[], None] | None = None
    #: 本 Run 的执行时限（runtime 能力表 + Settings 覆盖）；None = 用全局 run_* 配置
    limits: RuntimeLimits | None = None
    #: 已生成正文的提供者（runtime 侧实时增量）：超时/取消时据此保留产出
    partial_output: Callable[[], str] | None = None
    #: 本 Run 的进程是否由 runtime 自己复用（池化）：终止时不得整组杀掉
    reusable_process: bool = False
    registered: asyncio.Event = field(default_factory=asyncio.Event)
    settled: asyncio.Event = field(default_factory=asyncio.Event)


class RunSupervisor:
    """Run 状态机 + 心跳/超时 + 进程树清理 + 对账 + 重启恢复。"""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: AgentRunRepository | None = None,
        bus: EventBus | None = None,
        owner_pid: int | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._settings = settings
        self._repo: AgentRunRepository | _NoopRunRepository = repository or _NoopRunRepository()
        self._bus = bus
        self._owner_pid = owner_pid if owner_pid is not None else os.getpid()
        self._clock = clock or _monotonic
        self._monitors: dict[str, _Monitor] = {}
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._reconcile_task: asyncio.Task[None] | None = None
        #: 每轮对账末尾追加执行的钩子（任务侧僵尸收尾等），失败只记日志
        self._reconcile_hooks: list[Callable[[], Awaitable[object]]] = []

    def add_reconcile_hook(self, hook: Callable[[], Awaitable[object]]) -> None:
        """注册"每轮对账后也要跑一次"的钩子；Run 状态先对完，下游才好据真实状态收尾。"""
        self._reconcile_hooks.append(hook)

    # --- 生命周期 ---

    @property
    def repository(self) -> AgentRunRepository | _NoopRunRepository:
        """仓储只读视图：Runtime 读库刷新 API 快照时用。"""
        return self._repo

    async def create_run(
        self,
        *,
        run_id: str,
        session_id: str,
        instruction: str,
        agent_id: str | None = None,
        task_id: object | None = None,
        permission_mode: str = "normal",
        limits: RuntimeLimits | None = None,
    ) -> None:
        """登记一次 Run：建库行（PENDING）+ 建内存监视器，并发布 started 事件。

        `limits` 是 runtime 声明的执行时限（见 runtimes/base.py）；None = 沿用全局
        `run_*` 配置（未登记时限的 runtime 同样适用）。
        """
        now_mono = self._clock()
        self._monitors[run_id] = _Monitor(
            run_id=run_id,
            status=DshRunStatus.PENDING,
            created_mono=now_mono,
            last_state_mono=now_mono,
            last_output_mono=now_mono,
            last_mcp_mono=now_mono,
            agent_id=agent_id,
            limits=limits,
            reusable_process=bool(limits and limits.reusable_process),
        )
        await self._repo.create(
            run_id=run_id,
            session_id=session_id,
            instruction=instruction,
            status=DshRunStatus.PENDING,
            agent_id=agent_id,
            task_id=_as_uuid_or_none(task_id),
            owner_pid=self._owner_pid,
            started_at=datetime.now(timezone.utc),
            permission_mode=permission_mode,
        )
        await self._publish(
            Events.DSH_RUN_STARTED,
            {
                "run_id": run_id,
                "session_id": session_id,
                "instruction": instruction,
                "status": str(DshRunStatus.PENDING),
                "agent_id": agent_id,
                "task_id": str(task_id) if task_id else None,
                "permission_mode": permission_mode,
            },
        )

    async def mark_starting(self, run_id: str) -> None:
        """进入 STARTING：进程尚未拉起（startup timeout 从这一刻起算）。"""
        monitor = self._monitors.get(run_id)
        if monitor is not None:
            if monitor.terminal:
                return
            monitor.status = DshRunStatus.STARTING
            monitor.started_mono = self._clock()
            monitor.last_state_mono = monitor.started_mono
        await self._repo.set_status(run_id, DshRunStatus.STARTING)

    async def mark_running(self, run_id: str, *, pid: int | None, pgid: int | None) -> None:
        """进程已起来：记 pid/pgid，进入 RUNNING。"""
        monitor = self._monitors.get(run_id)
        if monitor is not None:
            if monitor.terminal:
                return
            monitor.status = DshRunStatus.RUNNING
            monitor.pid = pid
            monitor.pgid = pgid
            monitor.last_state_mono = self._clock()
        if pid is not None and pgid is not None:
            await self._repo.set_process(run_id, pid=pid, pgid=pgid)
        await self._repo.set_status(run_id, DshRunStatus.RUNNING)

    def register_interrupt(self, run_id: str, callback: Callable[[], None]) -> None:
        """登记运行时的优雅中断回调；若取消/超时已先到，立刻补一次通知，避免漏掉。"""
        monitor = self._monitors.get(run_id)
        if monitor is None:
            return
        monitor.interrupt = callback
        monitor.registered.set()
        if monitor.cancel_requested and not monitor.terminal:
            self._safe_call(callback, run_id)

    def register_partial_output(self, run_id: str, provider: Callable[[], str]) -> None:
        """登记"已生成正文"的读取器：超时/取消时把产出带进终态，而不是整轮作废。

        任何 runtime 都能用（事件累积或增量缓冲）；不登记则维持原行为
        （超时/取消不保留正文）。读取器必须无副作用、同步、可重复调用。
        """
        monitor = self._monitors.get(run_id)
        if monitor is not None:
            monitor.partial_output = provider

    def is_settled(self, run_id: str) -> bool:
        """该 Run 是否已被看护器落定终态（_execute 据此放弃后续动作）。"""
        monitor = self._monitors.get(run_id)
        if monitor is not None:
            return monitor.terminal
        return True

    def release(self, run_id: str) -> None:
        """_execute 收尾后释放监视器；终态已写库，内存记录不必长期保留。"""
        self._monitors.pop(run_id, None)

    async def note_output(self, run_id: str, event: dict | None = None) -> None:
        """收到 Agent 事件：算一次有效进展。

        事件流可能每秒来几十条，库里的 `last_output_at` 只需反映"最近还在动"，
        故按 1 秒节流落库；内存里的 `last_output_mono` 每条都更新，超时判定不受影响。
        """
        now = self._clock()
        monitor = self._monitors.get(run_id)
        if monitor is not None:
            monitor.last_output_mono = now
            if now - monitor.last_output_persist_mono < _OUTPUT_PERSIST_INTERVAL:
                return
            monitor.last_output_persist_mono = now
        await self._repo.touch_output(run_id)
        if event is not None:
            await self._repo.set_last_event(run_id, event)

    async def note_mcp_activity(self, agent_id: str | None) -> None:
        """MCP 工具调用成功 → 该 Agent 名下正在跑的 Run 都算有进展。

        只认本进程监视器里的 Run：多实例共享同一个库时，别的实例的 Run 由别人看护。
        """
        if not agent_id:
            return
        for monitor in list(self._monitors.values()):
            if monitor.terminal or monitor.agent_id != agent_id:
                continue
            monitor.last_mcp_mono = self._clock()
            await self._repo.touch_mcp_activity(monitor.run_id)

    async def record_metrics(self, run_id: str, metrics: dict[str, Any]) -> None:
        """把 Run 原生用量写进 run 记录（复用 last_event，不新增表/迁移）。

        终态落定后调用；`last_event` 由此变成一条 `run_metrics` 汇总，
        运行详情和预算报表可直接读到步数、工具成功/失败与 token。
        """
        await self._repo.set_last_event(run_id, metrics)

    async def finish(
        self,
        run_id: str,
        status: DshRunStatus,
        *,
        final_response: str = "",
        finish_reason: str | None = None,
        error: str | None = None,
        timeout_kind: str | None = None,
    ) -> DshRunStatus:
        """落定终态。已是终态则原样返回——cancel/timeout 先落定的结果不会被迟到写入覆盖。

        终态在第一个 await 之前同步占位（terminal=True）：用户取消的同时 runtime 被
        killpg 掐断、执行线程抛异常也来收尾时，并发的第二个 finish 只会拿到已落定的
        状态返回，不会再写一条终态事件 / 结果消息（TC-N-502r 曾因此把任务写成 failed）。
        """
        monitor = self._monitors.get(run_id)
        if monitor is not None and monitor.terminal:
            return monitor.status
        if monitor is None:
            row = await self._repo.get(run_id)
            if row is not None and is_terminal(row.status):
                return DshRunStatus(row.status)
        # 已请求取消的 Run 以取消收尾为准：正常返回（COMPLETED）与"被清理掐断"抛出的
        # 异常（FAILED）都不是新结果。能否记 CANCELLED 仍由进程树是否清理干净决定（§2.5）。
        as_cancel = (
            monitor is not None
            and monitor.cancel_requested
            and status
            in (
                DshRunStatus.COMPLETED,
                DshRunStatus.FAILED,
            )
        )
        if monitor is not None:
            monitor.status = DshRunStatus.CANCELLED if as_cancel else status
            monitor.terminal = True
            monitor.last_state_mono = self._clock()
        if as_cancel and monitor is not None:
            row = await self._repo.get(run_id)
            if not await self._ensure_process_gone(monitor, row):
                return await self._finish_cancel_failure(run_id, monitor)
            # 取消也保留已生成正文：会话绑定与部分产出对下一轮仍然有价值
            partial = (final_response or self._collect_partial(monitor)).strip()
            await self._record_terminal(
                run_id,
                DshRunStatus.CANCELLED,
                final_response=partial,
                finish_reason=finish_reason or "cancelled",
            )
            monitor.settled.set()
            return DshRunStatus.CANCELLED
        await self._record_terminal(
            run_id,
            status,
            final_response=final_response,
            finish_reason=finish_reason,
            error=error,
            timeout_kind=timeout_kind,
        )
        if monitor is not None:
            monitor.settled.set()
        return status

    async def cancel(self, run_id: str) -> DshRunStatus:
        """取消一次 Run：CANCELLING → 优雅通知 → 清理整个进程树 → CANCELLED。

        清理不掉时落到 FAILED，并在 error 里写明"进程树清理失败"——不伪装成取消成功。
        """
        monitor = self._monitors.get(run_id)
        row = await self._repo.get(run_id)
        if monitor is None and row is None:
            from flux.errors import NotFoundError

            raise NotFoundError(f"Run {run_id} 不存在", details={"run_id": run_id})
        if monitor is not None and monitor.terminal:
            return monitor.status
        if monitor is None and row is not None and is_terminal(row.status):
            return DshRunStatus(row.status)
        if (
            monitor is None
            and row is not None
            and row.owner_pid not in (None, self._owner_pid)
            and _pid_alive(row.owner_pid)
        ):
            # 别人实例正在跑的行：不碰它的进程，也不改写它的状态
            logger.warning("拒绝跨实例取消 run=%s owner=%s", run_id, row.owner_pid)
            return DshRunStatus(row.status)

        if monitor is not None:
            monitor.status = DshRunStatus.CANCELLING
            monitor.cancel_requested = True
            monitor.last_state_mono = self._clock()
        await self._repo.request_cancel(run_id)
        await self._repo.set_status(run_id, DshRunStatus.CANCELLING)

        # 先请运行时自己收尾（各 CLI runtime 的中断回调），再动刀。
        if monitor is not None:
            if monitor.interrupt is not None:
                self._safe_call(monitor.interrupt, run_id)
            else:
                await self._wait_for_registration(monitor)

        # 已有监视器且已被 _execute 干净收尾 → 只需核对进程是否真没了。
        if monitor is not None and monitor.terminal:
            if await self._ensure_process_gone(monitor, row):
                return monitor.status
            return await self._finish_cancel_failure(run_id, monitor)

        if await self._ensure_process_gone(monitor, row):
            return await self.finish(run_id, DshRunStatus.CANCELLED)
        return await self._finish_cancel_failure(run_id, monitor)

    # --- 心跳 / 超时 ---

    async def heartbeat_once(self) -> None:
        """一拍心跳：刷新 last_heartbeat_at，并检查三类超时。"""
        now = self._clock()
        stuck_after = (
            max(self._settings.run_cancel_grace_seconds, 0.0) + _KILL_CONFIRM_SECONDS + 3.0
        )
        for monitor in list(self._monitors.values()):
            if monitor.terminal:
                continue
            if (
                monitor.status is DshRunStatus.CANCELLING
                and now - monitor.last_state_mono > stuck_after
            ):
                # 取消流程卡住（协程被掐断等）：自己把终态收掉，不允许永久 CANCELLING
                await self._recover_stuck_cancelling(monitor)
                continue
            kind = self._timeout_kind(monitor, now)
            if kind is not None:
                await self._apply_timeout(monitor, kind)
                continue
            await self._repo.touch_heartbeat(monitor.run_id)

    async def _apply_timeout(self, monitor: _Monitor, kind: str) -> None:
        """超时：先落终态（防止 _execute 的迟到收尾改写结果），再清理进程树。

        只要 runtime 已经产出过正文，就按「截断完成」收尾（COMPLETED +
        finish_reason=truncated + timeout_kind）：已有产出与会话绑定留给下一轮，
        不再把整轮作废；完全没产出才记 TIMEOUT。
        """
        if monitor.terminal:
            return
        reason = _timeout_reason(kind, monitor, self._settings, self._clock, monitor.limits)
        partial = self._collect_partial(monitor)
        truncated = bool(partial)
        monitor.status = DshRunStatus.COMPLETED if truncated else DshRunStatus.TIMEOUT
        monitor.terminal = True
        monitor.last_state_mono = self._clock()
        logger.warning(
            "run 超时 run=%s kind=%s partial_chars=%d", monitor.run_id, kind, len(partial)
        )
        if monitor.interrupt is not None:
            self._safe_call(monitor.interrupt, monitor.run_id)
        row = await self._repo.get(monitor.run_id)
        await self._ensure_process_gone(monitor, row)
        await self._record_terminal(
            monitor.run_id,
            monitor.status,
            final_response=partial,
            finish_reason="truncated" if truncated else None,
            error=None if truncated else reason,
            timeout_kind=kind,
        )
        monitor.settled.set()

    def _timeout_kind(self, monitor: _Monitor, now: float) -> str | None:
        limits = monitor.limits
        hard = (
            limits.hard_timeout_seconds
            if limits is not None
            else self._settings.run_hard_timeout_seconds
        )
        startup = self._settings.run_startup_timeout_seconds
        idle = (
            limits.idle_timeout_seconds
            if limits is not None
            else self._settings.run_idle_timeout_seconds
        )
        if hard > 0 and monitor.started_mono is not None and now - monitor.started_mono > hard:
            return "hard"
        if (
            startup > 0
            and monitor.status in (DshRunStatus.PENDING, DshRunStatus.STARTING)
            and now - monitor.created_mono > startup
        ):
            return "startup"
        if idle > 0 and monitor.status in (
            DshRunStatus.RUNNING,
            DshRunStatus.CANCELLING,
        ):
            last_progress = max(
                monitor.last_output_mono, monitor.last_mcp_mono, monitor.last_state_mono
            )
            if now - last_progress > idle:
                return "idle"
        return None

    # --- 对账 / 重启恢复 ---

    async def reconcile_once(self) -> None:
        """数据库状态 ↔ 真实进程状态对账（只处理本进程拥有的行）。"""
        for row in await self._repo.list_non_terminal():
            if (
                row.owner_pid != self._owner_pid
                and row.owner_pid is not None
                and _pid_alive(row.owner_pid)
            ):
                continue  # 别的实例的行：只要属主进程还活着就不动它
            if row.id in self._monitors:
                continue  # 本进程正在看护，心跳负责
            await self._reconcile_row(row)
        for hook in self._reconcile_hooks:
            try:
                await hook()
            except Exception:  # noqa: BLE001 - 下游钩子失败不能拖垮 Run 对账
                logger.exception("run supervisor 对账钩子失败")

    async def _reconcile_row(self, row: AgentRun) -> None:
        status = DshRunStatus(row.status)
        alive = (
            row.pid is not None
            and _pid_alive(row.pid)
            and (row.pgid is None or not _pgid_gone(row.pgid))
        )
        orphan = row.owner_pid is None or not _pid_alive(row.owner_pid)
        if alive:
            # 属主已死（或未知）的孤儿进程：无法再被看护，接管终止并落 INTERRUPTED
            if orphan:
                await self._ensure_row_process_gone(row)
                await self._record_terminal(
                    row.id,
                    DshRunStatus.INTERRUPTED,
                    error="Flux 重启后接管：原属主进程已退出，遗留 Agent 进程已终止",
                )
            return
        # 进程已不在：数据库还停在非终态 → 明确落 FAILED / INTERRUPTED，不留僵尸
        final = DshRunStatus.INTERRUPTED if orphan else DshRunStatus.FAILED
        error = (
            "Flux 重启后对账：Run 属主进程已退出，进程也不存在"
            if final is DshRunStatus.INTERRUPTED
            else "对账发现 Agent 进程已消失，但状态仍是非终态"
        )
        logger.warning("run 对账修正 run=%s %s → %s", row.id, status, final)
        await self._record_terminal(row.id, final, error=error, publish=True)

    async def startup_recovery(self) -> list[str]:
        """Flux 启动时扫描所有非终态 Run，按真实进程状态修正（§2.7）。"""
        recovered: list[str] = []
        for row in await self._repo.list_non_terminal():
            if (
                row.owner_pid is not None
                and row.owner_pid != self._owner_pid
                and _pid_alive(row.owner_pid)
            ):
                continue  # 另一个实例的活跃 Run，不动
            await self._reconcile_row(row)
            recovered.append(row.id)
        if recovered:
            logger.warning("run 启动恢复处理了 %d 条遗留 Run：%s", len(recovered), recovered)
        return recovered

    # --- 后台任务 ---

    def ensure_background(self) -> None:
        """惰性启动心跳与对账循环（幂等）。"""
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(
                self._loop(self._settings.run_heartbeat_interval_seconds, self.heartbeat_once),
                name="run-supervisor-heartbeat",
            )
        if self._reconcile_task is None or self._reconcile_task.done():
            self._reconcile_task = asyncio.create_task(
                self._loop(self._settings.run_reconcile_interval_seconds, self.reconcile_once),
                name="run-supervisor-reconcile",
            )

    async def stop_background(self) -> None:
        for task in (self._heartbeat_task, self._reconcile_task):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._heartbeat_task = None
        self._reconcile_task = None

    async def kill_active_processes(self) -> None:
        """进程退出前尽力清理仍在跑的进程树，避免留下孤儿 Agent。"""
        for monitor in list(self._monitors.values()):
            if monitor.terminal:
                continue
            row = await self._repo.get(monitor.run_id)
            await self._ensure_process_gone(monitor, row)

    async def _loop(self, interval: float, action: Callable[[], object]) -> None:
        period = max(interval, 0.05)
        while True:
            await asyncio.sleep(period)
            try:
                await action()  # type: ignore[misc]
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 看护循环自身不能因为单次异常停摆
                logger.exception("run supervisor 周期任务失败")

    # --- 内部 ---

    async def _record_terminal(
        self,
        run_id: str,
        status: DshRunStatus,
        *,
        final_response: str = "",
        finish_reason: str | None = None,
        error: str | None = None,
        timeout_kind: str | None = None,
        publish: bool = True,
    ) -> None:
        await self._repo.set_status(
            run_id,
            status,
            error=error,
            finish_reason=finish_reason,
            timeout_kind=timeout_kind,
            finished=True,
        )
        if not publish:
            return
        row = await self._repo.get(run_id)
        body: dict[str, object] = {
            "run_id": run_id,
            "status": str(status),
            "final_response": final_response,
            "finish_reason": finish_reason,
            "error": error,
            "timeout_kind": timeout_kind,
            "agent_id": row.agent_id if row is not None else None,
            "task_id": str(row.task_id) if row is not None and row.task_id else None,
        }
        await self._publish(_event_for(status), body)

    async def _finish_cancel_failure(self, run_id: str, monitor: _Monitor | None) -> DshRunStatus:
        """清理不掉就明确失败：绝不把"还有进程活着"记成 cancelled。"""
        message = "取消失败：Agent 进程树未能清理干净（SIGTERM/SIGKILL 后仍存在）"
        logger.error("run %s run=%s", message, run_id)
        if monitor is not None:
            monitor.status = DshRunStatus.FAILED
            monitor.terminal = True
            monitor.settled.set()
            await self._record_terminal(run_id, DshRunStatus.FAILED, error=message)
            return DshRunStatus.FAILED
        row = await self._repo.get(run_id)
        if row is not None and is_terminal(row.status):
            return DshRunStatus(row.status)
        await self._record_terminal(run_id, DshRunStatus.FAILED, error=message)
        return DshRunStatus.FAILED

    async def _recover_stuck_cancelling(self, monitor: _Monitor) -> None:
        """纠正卡住的 CANCELLING：再清一次进程树，成功落 CANCELLED，失败落 FAILED。"""
        logger.warning("run 检测到卡住的 CANCELLING，自行收尾 run=%s", monitor.run_id)
        row = await self._repo.get(monitor.run_id)
        monitor.terminal = True
        if await self._ensure_process_gone(monitor, row):
            monitor.status = DshRunStatus.CANCELLED
            monitor.settled.set()
            await self._record_terminal(monitor.run_id, DshRunStatus.CANCELLED)
            return
        await self._finish_cancel_failure(monitor.run_id, monitor)

    async def _wait_for_registration(self, monitor: _Monitor) -> None:
        """取消来得比进程登记还早：等一小会儿，登记时 register_interrupt 会补发通知。"""
        deadline = self._clock() + max(self._settings.run_cancel_grace_seconds, 0.0)
        while self._clock() < deadline:
            if monitor.registered.is_set() or monitor.terminal:
                return
            await asyncio.sleep(_POLL_STEP)

    async def _ensure_process_gone(self, monitor: _Monitor | None, row: AgentRun | None) -> bool:
        """确保整个进程树消失：SIGTERM → 等 grace → SIGKILL → 确认。没有进程则视为已清理。"""
        if monitor is not None and monitor.reusable_process and self._reusable_alive(monitor, row):
            # runtime 声明了进程复用：这个进程跨 Run 常驻，取消/超时只结束本轮，
            # 由 runtime 自己（abort + 静默）收口，这里绝不能杀。
            logger.info(
                "保留可复用进程 run=%s pid=%s pgid=%s",
                monitor.run_id,
                monitor.pid,
                monitor.pgid,
            )
            return True
        pgid = monitor.pgid if monitor is not None else None
        pid = monitor.pid if monitor is not None else None
        if pgid is None and row is not None:
            pgid = row.pgid
        if pid is None and row is not None:
            pid = row.pid

        if pgid is None:
            if pid is None:
                return True  # 没有可清理的进程（未启动 / 纯内存替身）
            return await self._kill_pid(pid)
        if is_self_group(pgid):
            # 绝不对自己所在的进程组动手：那会把 Flux 自己一起杀掉
            logger.error("拒绝清理与 Flux 同组的 pgid=%s run 进程", pgid)
            return False
        return await self._kill_pgid(pgid)

    async def _ensure_row_process_gone(self, row: AgentRun) -> bool:
        return await self._ensure_process_gone(None, row)

    def _reusable_alive(self, monitor: _Monitor, row: AgentRun | None) -> bool:
        """可复用进程是否还在：pgid 优先（组内可能还有子进程），pid 兜底。"""
        pgid = monitor.pgid if monitor.pgid is not None else (row.pgid if row is not None else None)
        if pgid is not None and not is_self_group(pgid) and is_group_alive(pgid):
            return True
        pid = monitor.pid if monitor.pid is not None else (row.pid if row is not None else None)
        return bool(pid is not None and is_alive(pid))

    async def _kill_pgid(self, pgid: int) -> bool:
        if not is_group_alive(pgid):
            return True
        # 这里刻意不声明 owns_group：pgid 来自监视器/数据库（重启接管、SDK 自建的进程都可能
        # 落在里面），无法保证该组是本进程用 CREATE_NEW_PROCESS_GROUP 创建的。Windows 上
        # 对不属于自己的组发 CTRL_BREAK 会波及共享控制台（把运行 Flux 的终端一起打断），
        # 因此优雅信号只在 POSIX 生效，Windows 交由 kill_group 的 taskkill 收尾。
        signal_group_graceful(pgid)
        grace = max(self._settings.run_cancel_grace_seconds, 0.0)
        if await self._wait_gone(pgid, grace):
            return True
        kill_group(pgid)
        return await self._wait_gone(pgid, _KILL_CONFIRM_SECONDS)

    async def _kill_pid(self, pid: int) -> bool:
        if not is_alive(pid):
            return True
        signal_process_graceful(pid)
        grace = max(self._settings.run_cancel_grace_seconds, 0.0)
        deadline = self._clock() + grace
        while self._clock() < deadline:
            if not is_alive(pid):
                return True
            await asyncio.sleep(_POLL_STEP)
        kill_process(pid)
        deadline = self._clock() + _KILL_CONFIRM_SECONDS
        while self._clock() < deadline:
            if not is_alive(pid):
                return True
            await asyncio.sleep(_POLL_STEP)
        return not is_alive(pid)

    async def _wait_gone(self, pgid: int, timeout: float) -> bool:
        deadline = self._clock() + max(timeout, 0.0)
        while True:
            if not is_group_alive(pgid):
                return True
            if self._clock() >= deadline:
                return False
            await asyncio.sleep(_POLL_STEP)

    @staticmethod
    def _safe_call(callback: Callable[[], None], run_id: str) -> None:
        try:
            callback()
        except Exception:  # noqa: BLE001 - 通知失败不能改变终态判定
            logger.exception("优雅中断通知失败 run=%s", run_id)

    def _collect_partial(self, monitor: _Monitor) -> str:
        """读取 runtime 已产出的正文（未登记读取器时为空串）。"""
        provider = monitor.partial_output
        if provider is None:
            return ""
        try:
            return (provider() or "").strip()
        except Exception:  # noqa: BLE001 - 读取失败只影响"能否保留产出"，不影响终态
            logger.exception("读取已生成正文失败 run=%s", monitor.run_id)
            return ""

    async def _publish(self, event: str, body: dict[str, object]) -> None:
        if self._bus is not None:
            await self._bus.publish(event, body)

    # --- 只读快照 ---

    def monitor_snapshot(self, run_id: str) -> dict[str, object] | None:
        monitor = self._monitors.get(run_id)
        if monitor is None:
            return None
        return {
            "run_id": monitor.run_id,
            "status": str(monitor.status),
            "pid": monitor.pid,
            "pgid": monitor.pgid,
            "cancel_requested": monitor.cancel_requested,
            "terminal": monitor.terminal,
        }

    def active_count(self) -> int:
        return sum(1 for m in self._monitors.values() if not m.terminal)


def _event_for(status: DshRunStatus) -> str:
    return EVENT_BY_STATUS.get(status, Events.DSH_RUN_FAILED)


def _as_uuid_or_none(value: object | None) -> uuid.UUID | None:
    """把 task_id 归一成 UUID（调用方可能传 str/UUID/None）。"""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        logger.warning("Run 的 task_id 不是合法 UUID，按无关联处理：%r", value)
        return None


def _timeout_reason(
    kind: str,
    monitor: _Monitor,
    settings: Settings,
    clock: Callable[[], float],
    limits: RuntimeLimits | None = None,
) -> str:
    hard_limit = (
        limits.hard_timeout_seconds if limits is not None else settings.run_hard_timeout_seconds
    )
    idle_limit = (
        limits.idle_timeout_seconds if limits is not None else settings.run_idle_timeout_seconds
    )
    if kind == "hard":
        return f"运行超时（hard）：超过绝对上限 {hard_limit:g} 秒"
    if kind == "startup":
        elapsed = clock() - monitor.created_mono
        return (
            f"启动超时（startup）：{elapsed:.0f} 秒内未完成 Agent 启动"
            f"（上限 {settings.run_startup_timeout_seconds:g} 秒）"
        )
    idle_for = clock() - max(
        monitor.last_output_mono, monitor.last_mcp_mono, monitor.last_state_mono
    )
    return f"空闲超时（idle）：{idle_for:.0f} 秒内既无输出也无 MCP 活动（上限 {idle_limit:g} 秒）"


def _monotonic() -> float:
    import time

    return time.monotonic()


def _pid_alive(pid: int) -> bool:
    """只读探活（平台原语）：Windows 上绝不退化成终止进程。"""
    return is_alive(pid)


def _pgid_gone(pgid: int) -> bool:
    return not is_group_alive(pgid)


__all__ = ["EVENT_BY_STATUS", "RunSupervisor", "TERMINAL_RUN_STATUSES"]
