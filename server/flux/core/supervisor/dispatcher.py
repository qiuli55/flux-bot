"""Supervisor 的执行层：把工单落到既有 Task Engine，再起一次 Run。

本层只做三件事，且每一步都以"平台事实"为准：

1. **解析角色**：工单声明的 role 必须能在 Agent 注册表里找到对应档案；
   找不到就写一条 `blocked` 报告，绝不随便挑一个 Agent 顶替授权边界。
2. **派发**：创建 Task（pending）→ 建 assignment → `TaskRunService.start()`。
   任务状态、Run 状态仍由既有引擎负责，本层不复制第二套状态机。
3. **回收结论**：任务进入终态或等人决策时，按真实 task.status 物化一条
   `supervisor_reports`；Agent 不能自报成功。

工单阶段不落库，一律由 `derive_stage()` 从 task / assignment / report 三个事实派生。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from flux.core.agent_runtime.manager import AgentHandle, AgentManager
from flux.core.agent_runtime.runtimes.service import TaskRunService
from flux.core.supervisor.repository import SupervisorRepository
from flux.core.task_engine.repository import TaskRepository
from flux.enums import ReportStatus, TaskStatus, TicketStage
from flux.errors import AIOSError, NotFoundError
from flux.logging import get_logger
from flux.models.supervisor import SupervisorTicket

logger = get_logger(__name__)

__all__ = ["DispatchOutcome", "SupervisorDispatcher", "derive_stage"]

#: 任务的真实终态：到达这里就不会再变化
_TERMINAL_TASKS = frozenset(
    {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
)

#: task.status → 报告状态（只有到达这些状态才物化报告）
_TASK_REPORT = {
    TaskStatus.COMPLETED.value: (ReportStatus.COMPLETED, "任务已完成"),
    TaskStatus.FAILED.value: (ReportStatus.FAILED, "任务执行失败"),
    TaskStatus.CANCELLED.value: (ReportStatus.CANCELLED, "任务已取消"),
    TaskStatus.WAITING_FOR_USER_DECISION.value: (ReportStatus.NEEDS_INPUT, "等待用户决策"),
}


def derive_stage(
    *,
    task_status: str | None,
    assignment_status: str | None,
    report_status: str | None,
) -> TicketStage:
    """从结构化事实派生工单阶段（不读、不写 ticket.status，因为根本没有这一列）。

    优先级：在途 assignment > 报告结论 > 任务状态 > 历史 assignment > planned。
    在途 assignment 优先，是为了让"失败后重试"立刻显示为 running——
    旧报告仍然保留，但不能把新一轮执行盖成 stopped。
    """
    if assignment_status in {"dispatched", "running"}:
        return TicketStage.RUNNING
    if report_status in {
        ReportStatus.COMPLETED.value,
        ReportStatus.FAILED.value,
        ReportStatus.BLOCKED.value,
        ReportStatus.NEEDS_INPUT.value,
        ReportStatus.CANCELLED.value,
        ReportStatus.TIMED_OUT.value,
    }:
        if report_status == ReportStatus.COMPLETED.value:
            return TicketStage.DONE
        if report_status in {ReportStatus.BLOCKED.value, ReportStatus.NEEDS_INPUT.value}:
            return TicketStage.WAITING
        return TicketStage.STOPPED
    if task_status is not None:
        if task_status in _TERMINAL_TASKS:
            return TicketStage.REVIEWING
        if task_status == TaskStatus.RUNNING.value:
            return TicketStage.RUNNING
        if task_status == TaskStatus.WAITING_FOR_USER_DECISION.value:
            return TicketStage.WAITING
    if assignment_status == "finished":
        return TicketStage.REVIEWING
    if assignment_status == "failed":
        return TicketStage.STOPPED
    return TicketStage.PLANNED


@dataclass(frozen=True)
class DispatchOutcome:
    """一次派发的结论；`dispatched=False` 时 reason 说明为什么没派出去。"""

    ticket_id: uuid.UUID
    dispatched: bool
    reason: str = ""
    task_id: uuid.UUID | None = None
    run_id: str = ""
    agent_id: uuid.UUID | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": str(self.ticket_id),
            "dispatched": self.dispatched,
            "reason": self.reason,
            "task_id": str(self.task_id) if self.task_id is not None else None,
            "run_id": self.run_id,
            "agent_id": str(self.agent_id) if self.agent_id is not None else None,
        }


class SupervisorDispatcher:
    """工单 → Task → Run 的唯一派发入口。"""

    def __init__(
        self,
        repo: SupervisorRepository,
        tasks: TaskRepository,
        runs: TaskRunService,
        agents: AgentManager,
        *,
        dsh: Any = None,
    ) -> None:
        self._repo = repo
        self._tasks = tasks
        self._runs = runs
        self._agents = agents
        self._dsh = dsh

    # --- 角色 ---

    def roles(self) -> tuple[str, ...]:
        """当前注册表里真实存在的角色（规划层只允许从这里选）。"""
        return tuple(sorted({str(handle.spec.role) for handle in self._agents.list()}))

    def _agent_for(self, role: str) -> AgentHandle | None:
        """按角色找档案；同名档案优先，找不到就是没有，绝不跨角色顶替。"""
        target = (role or "").strip()
        if not target:
            return None
        matches = [handle for handle in self._agents.list() if str(handle.spec.role) == target]
        if matches:
            return min(matches, key=lambda handle: handle.id.int)
        return self._agents.find_by_name(target)

    # --- 派发 ---

    async def dispatch_ticket(self, ticket: SupervisorTicket) -> DispatchOutcome:
        """首次派发；已经有 task 的工单不会被重复派活。"""
        if ticket.task_id is not None:
            return DispatchOutcome(ticket.id, False, "工单已派发", task_id=ticket.task_id)
        return await self._dispatch(ticket)

    async def retry_ticket(self, ticket: SupervisorTicket) -> DispatchOutcome:
        """重试：为工单新建一个 Task 与 assignment，旧记录保留供回放。"""
        return await self._dispatch(ticket)

    async def _dispatch(self, ticket: SupervisorTicket) -> DispatchOutcome:
        handle = self._agent_for(ticket.role)
        if handle is None:
            reason = f"没有可执行角色 {ticket.role} 的 Agent；请先注册该角色的 Agent"
            # 自动续派每轮都会重试这张工单：同一角色只写一条 blocked 报告，
            # 否则 watchdog / auto_dispatch 会把看板刷成一堵重复报告墙。
            latest = (await self._repo.latest_reports([ticket.id])).get(ticket.id)
            if (
                latest is not None
                and latest.status == ReportStatus.BLOCKED.value
                and (latest.payload or {}).get("role") == ticket.role
            ):
                return DispatchOutcome(ticket.id, False, reason)
            await self._repo.create_report(
                ticket.id,
                status=ReportStatus.BLOCKED,
                summary=reason,
                payload={"role": ticket.role, "reason": "agent_unavailable"},
            )
            logger.warning("Supervisor 工单 %s 无法派发：%s", ticket.id, reason)
            return DispatchOutcome(ticket.id, False, reason)

        task_id = uuid.uuid4()
        description = ticket.title
        if ticket.description and ticket.description != ticket.title:
            description = f"{ticket.title}\n\n{ticket.description}"
        task = await self._tasks.create(
            task_id=task_id,
            description=description,
            priority=100,
            project_id=ticket.project_id,
            agent_id=handle.id,
        )
        assignment = await self._repo.create_assignment(
            ticket.id,
            agent_id=handle.id,
            task_id=task_id,
            status="dispatched",
        )
        await self._repo.set_ticket_task(ticket.id, task_id)

        try:
            result = await self._runs.start(task)
        except AIOSError as exc:
            # 起 Run 失败不是"执行失败"：任务已被 TaskRunService 回滚为 pending，
            # 这里如实记一条 failed 报告，供人工判断是否重试。
            await self._repo.update_assignment(
                assignment.id, status="failed", finished_at=_utcnow()
            )
            await self._repo.create_report(
                ticket.id,
                assignment_id=assignment.id,
                status=ReportStatus.FAILED,
                summary=f"起 Run 失败：{exc}",
                payload={"stage": "start_run"},
            )
            logger.warning("Supervisor 工单 %s 起 Run 失败：%s", ticket.id, exc)
            return DispatchOutcome(
                ticket.id, False, f"起 Run 失败：{exc}", task_id=task_id, agent_id=handle.id
            )

        run_id = str(result.run.get("run_id") or "")
        await self._repo.update_assignment(
            assignment.id,
            status="running",
            run_id=run_id or None,
            started_at=_utcnow(),
        )
        logger.info(
            "Supervisor 派发工单 %s → agent=%s task=%s run=%s",
            ticket.id,
            handle.id,
            task_id,
            run_id,
        )
        return DispatchOutcome(
            ticket.id,
            True,
            task_id=task_id,
            run_id=run_id,
            agent_id=handle.id,
        )

    # --- 收尾 ---

    async def sync_ticket(self, ticket: SupervisorTicket) -> str | None:
        """按任务真实状态物化报告；返回最新报告状态（没有则 None）。

        幂等：同一 assignment 同一状态只会落一条报告，重复对账不会刷出重复行。
        """
        latest = (await self._repo.latest_assignments([ticket.id])).get(ticket.id)
        if latest is None or latest.status not in {"dispatched", "running"}:
            return None
        if latest.task_id is None:
            return None
        try:
            task = await self._tasks.get(latest.task_id)
        except NotFoundError:
            return None
        mapped = _TASK_REPORT.get(str(task.status))
        if mapped is None:
            return None
        status, summary = mapped
        report = (await self._repo.latest_reports([ticket.id])).get(ticket.id)
        if (
            report is not None
            and report.assignment_id == latest.id
            and report.status == status.value
        ):
            return status.value

        await self._repo.create_report(
            ticket.id,
            assignment_id=latest.id,
            status=status,
            summary=summary,
            payload={"task_status": str(task.status), "run_id": latest.run_id},
        )
        if status in {ReportStatus.NEEDS_INPUT}:
            # 等人决策：Run 可能还活着，assignment 保持 running，不释放并发位
            return status.value
        await self._repo.update_assignment(latest.id, status="finished", finished_at=_utcnow())
        return status.value

    async def stop_ticket(self, ticket: SupervisorTicket, *, reason: str = "") -> dict[str, Any]:
        """停止工单：能停 Run 就停 Run，再按平台事实写一条 cancelled 报告。"""
        latest = (await self._repo.latest_assignments([ticket.id])).get(ticket.id)
        stopped_run = ""
        if latest is not None and latest.run_id and self._dsh is not None:
            try:
                result = await self._dsh.cancel(latest.run_id)
                stopped_run = str(getattr(result, "status", ""))
            except NotFoundError:
                stopped_run = ""
        if ticket.task_id is not None:
            try:
                task = await self._tasks.get(ticket.task_id)
                if task.status not in _TERMINAL_TASKS:
                    await self._tasks.set_status(ticket.task_id, TaskStatus.CANCELLED)
            except NotFoundError:
                pass
        if latest is not None and latest.status in {"dispatched", "running"}:
            await self._repo.update_assignment(latest.id, status="finished", finished_at=_utcnow())
        await self._repo.create_report(
            ticket.id,
            assignment_id=latest.id if latest is not None else None,
            status=ReportStatus.CANCELLED,
            summary=reason or "由助理手动停止",
            payload={"stopped_run": stopped_run},
        )
        return {
            "ticket_id": str(ticket.id),
            "stopped_run": stopped_run,
            "report": ReportStatus.CANCELLED.value,
        }

    async def expire_assignment(
        self,
        ticket: SupervisorTicket,
        assignment: Any,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """超时回收一次卡住的派活：停 Run → 任务置取消 → 派活收尾 → 写超时报告。

        只处理仍在途（dispatched / running）的记录；已收尾的直接跳过，
        因此 watchdog 每轮扫同一批行也不会刷出重复报告。
        """
        if assignment.status not in {"dispatched", "running"}:
            return {"ticket_id": str(ticket.id), "expired": False, "reason": "派活已结束"}
        stopped_run = ""
        if assignment.run_id and self._dsh is not None:
            try:
                result = await self._dsh.cancel(assignment.run_id)
                stopped_run = str(getattr(result, "status", ""))
            except Exception:  # noqa: BLE001 - Run 已不在 / 通道异常都不能阻断回收
                logger.warning(
                    "Supervisor 回收时停 Run 失败 assignment=%s run=%s",
                    assignment.id,
                    assignment.run_id,
                    exc_info=True,
                )
        if assignment.task_id is not None:
            try:
                task = await self._tasks.get(assignment.task_id)
                if task.status not in _TERMINAL_TASKS:
                    await self._tasks.set_status(assignment.task_id, TaskStatus.CANCELLED)
            except NotFoundError:
                pass
        await self._repo.update_assignment(assignment.id, status="finished", finished_at=_utcnow())
        await self._repo.create_report(
            ticket.id,
            assignment_id=assignment.id,
            status=ReportStatus.TIMED_OUT,
            summary=reason,
            payload={"stage": "watchdog", "stopped_run": stopped_run},
        )
        logger.warning("Supervisor 回收工单 %s：%s", ticket.id, reason)
        return {"ticket_id": str(ticket.id), "expired": True, "reason": reason}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
