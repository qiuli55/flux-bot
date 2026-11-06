"""Supervisor 的编排层：把"判断通过"翻译成"工单树 + 派发 + 回收"。

三件事分开做，谁都不越权：

- 规划（planner）：模型只提建议，平台做硬校验，失败就降级成单工单；
- 落库（repository）：同一条 trigger 重复规划不会拆出第二棵树（plan_key 前缀幂等）；
- 派发（dispatcher）：角色必须真实存在，任务/运行状态一律以 Task Engine 为准。

本层不持有状态，所有事实都在 supervisor_* 与 tasks 表里，进程重启可继续。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from flux.core.supervisor.digest import build_thread_digest, is_closed, render_progress
from flux.core.supervisor.dispatcher import DispatchOutcome, SupervisorDispatcher, derive_stage
from flux.core.supervisor.gate import select_policy
from flux.core.supervisor.outbox import SupervisorOutbox
from flux.core.supervisor.planner import SupervisorPlanner, TicketPlan
from flux.core.supervisor.repository import SupervisorRepository
from flux.enums import AcceptMode, BrainSection, TaskStatus, TicketStage
from flux.logging import get_logger
from flux.models.supervisor import (
    SupervisorAssignment,
    SupervisorSettings,
    SupervisorThread,
    SupervisorTicket,
    SupervisorTrigger,
)

logger = get_logger(__name__)

__all__ = ["SupervisorOrchestrator"]

#: 占用并发位的阶段：running / waiting（等用户决策时 Run 可能还活着）
_ACTIVE_STAGE_VALUES = frozenset({TicketStage.RUNNING.value, TicketStage.WAITING.value})

#: 没有外部通道的来源：不需要（也无法）自动订阅回投
_INTERNAL_SOURCES = frozenset({"flux_internal"})

#: 任务已到终态：对账会物化报告，watchdog 不再重复回收
_TERMINAL_TASK_VALUES = frozenset(
    {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
)


class SupervisorOrchestrator:
    """规划 → 落库 → 派发 → 对账的唯一入口。"""

    def __init__(
        self,
        repo: SupervisorRepository,
        planner: SupervisorPlanner,
        dispatcher: SupervisorDispatcher,
        *,
        outbox: SupervisorOutbox | None = None,
        brain: Any | None = None,
        dispatch_timeout_seconds: float = 120.0,
        run_timeout_seconds: float = 0.0,
    ) -> None:
        self._repo = repo
        self._planner = planner
        self._dispatcher = dispatcher
        self._outbox = outbox
        #: Project Brain（可选）：线程收尾后把摘要写进项目的 progress 分区
        self._brain = brain
        #: watchdog 租约：派活后多久没进入执行就回收（秒）
        self._dispatch_timeout_seconds = max(0.0, float(dispatch_timeout_seconds))
        #: watchdog 租约：单次 Run 执行上限（秒），0 = 不限制
        self._run_timeout_seconds = max(0.0, float(run_timeout_seconds))

    # --- 规划 ---

    async def plan_trigger(
        self,
        trigger: SupervisorTrigger,
        *,
        title: str = "",
        dispatch: bool = False,
    ) -> dict[str, Any]:
        """为一条已确认的事件产出工单树；`dispatch=True` 时顺手派发可执行工单。"""
        settings = await self._repo.get_settings()
        thread = await self._repo.ensure_thread(
            thread_key=_thread_key(trigger),
            source_key=trigger.source_key,
            external_id=trigger.external_id,
            project_id=trigger.project_id,
        )
        plan = await self._planner.plan(
            trigger,
            title=title,
            roles=self._dispatcher.roles(),
            max_tickets=settings.max_tickets_per_thread,
            max_depth=settings.max_ticket_depth,
            provider=settings.model_provider,
        )
        tickets = await self._repo.create_tickets(
            thread_id=thread.id,
            project_id=trigger.project_id or thread.project_id,
            plan=plan,
            key_prefix=trigger.id.hex[:8],
        )
        await self._subscribe_thread(trigger, tickets)
        await self._repo.set_trigger_status(trigger.id, "planned")
        outcomes: list[DispatchOutcome] = []
        if dispatch:
            outcomes = await self.dispatch_thread(
                thread.id, max_concurrent_runs=settings.max_concurrent_runs
            )
        return {
            "thread": thread.to_dict(),
            "plan": _plan_dict(plan),
            "tickets": await self._ticket_items(tickets),
            "dispatch": [outcome.to_dict() for outcome in outcomes],
        }

    # --- 派发 ---

    async def dispatch_thread(
        self,
        thread_id: uuid.UUID,
        *,
        max_concurrent_runs: int | None = None,
    ) -> list[DispatchOutcome]:
        """按 sequence 派发依赖已完成的工单，并受并发位约束。"""
        await self._repo.get_thread(thread_id)
        tickets = await self._repo.list_tickets(thread_id=thread_id, limit=200)
        tickets.sort(key=lambda ticket: (ticket.sequence, str(ticket.id)))
        await self._refresh(tickets)
        items = await self._ticket_items(tickets)

        stages = {item["id"]: item["stage"] for item in items}
        by_id = {str(ticket.id): ticket for ticket in tickets}
        plan_keys = {ticket.id: ticket.plan_key for ticket in tickets}
        active = sum(1 for item in items if item["stage"] in _ACTIVE_STAGE_VALUES)
        if max_concurrent_runs is None:
            settings = await self._repo.get_settings()
            max_concurrent_runs = settings.max_concurrent_runs
        available = max(0, int(max_concurrent_runs) - active)

        outcomes: list[DispatchOutcome] = []
        for item in items:
            if available <= 0:
                break
            if item["stage"] != TicketStage.PLANNED.value:
                continue
            ticket = by_id[item["id"]]
            if not _dependencies_done(ticket, plan_keys, stages):
                continue
            outcome = await self._dispatcher.dispatch_ticket(ticket)
            outcomes.append(outcome)
            if outcome.dispatched:
                available -= 1
        return outcomes

    async def retry_ticket(self, ticket_id: uuid.UUID) -> dict[str, Any]:
        ticket = await self._repo.get_ticket(ticket_id)
        outcome = await self._dispatcher.retry_ticket(ticket)
        return outcome.to_dict()

    async def dispatch_ticket(self, ticket_id: uuid.UUID) -> dict[str, Any]:
        """单张工单手动派发；已有 task 的工单不会被重复派活。"""
        ticket = await self._repo.get_ticket(ticket_id)
        return (await self._dispatcher.dispatch_ticket(ticket)).to_dict()

    async def stop_ticket(self, ticket_id: uuid.UUID, *, reason: str = "") -> dict[str, Any]:
        ticket = await self._repo.get_ticket(ticket_id)
        return await self._dispatcher.stop_ticket(ticket, reason=reason)

    # --- 自动续派 / 超时回收 ---

    async def auto_dispatch(self, *, limit: int = 200) -> dict[str, Any]:
        """AUTO 模式下的续派：只推进已有工单树，绝不新建工单。

        上游工单完成后，下游 planned 工单在下一轮被派出；同一线程里无依赖的
        兄弟工单受并发位约束，可在同一轮并行派出（不额外等下一轮）。
        observe / off 模式直接返回空结果 —— 授权收回后立即停止续派。
        """
        settings = await self._repo.get_settings()
        tickets = await self._repo.list_tickets(limit=limit)
        seen: set[uuid.UUID] = set()
        thread_ids: list[uuid.UUID] = []
        for ticket in tickets:
            # 没有 task_id 的工单一定还没派出去过；其余线程无需再查策略。
            if ticket.task_id is not None or ticket.thread_id in seen:
                continue
            seen.add(ticket.thread_id)
            thread_ids.append(ticket.thread_id)

        outcomes: list[dict[str, Any]] = []
        threads = 0
        for thread_id in thread_ids:
            thread = await self._repo.get_thread(thread_id)
            if not await self._thread_auto(thread, settings):
                continue
            threads += 1
            dispatched = await self.dispatch_thread(
                thread_id, max_concurrent_runs=settings.max_concurrent_runs
            )
            outcomes.extend(outcome.to_dict() for outcome in dispatched)
        return {
            "mode": str(settings.accept_mode),
            "threads": threads,
            "dispatched": sum(1 for item in outcomes if item["dispatched"]),
            "outcomes": outcomes,
        }

    async def watchdog(self, *, limit: int = 50) -> dict[str, Any]:
        """回收卡住的在途派活：租约超时、任务丢失、执行超时。

        只处理 dispatched / running 的记录；任务已到终态的交给常规对账物化，
        不在这里抢写报告。回收动作写 `timed_out` 报告，并释放并发位。
        """
        assignments = await self._repo.active_assignments(limit=limit)
        if not assignments:
            return {"checked": 0, "expired": 0, "items": []}
        tickets = await self._repo.get_tickets([item.ticket_id for item in assignments])
        now = datetime.now(timezone.utc)
        items: list[dict[str, Any]] = []
        for assignment in assignments:
            ticket = tickets.get(assignment.ticket_id)
            if ticket is None:
                continue
            reason = await self._stall_reason(assignment, now=now)
            if reason is None:
                continue
            result = await self._dispatcher.expire_assignment(ticket, assignment, reason=reason)
            if result.get("expired"):
                items.append(result)
        return {"checked": len(assignments), "expired": len(items), "items": items}

    async def _stall_reason(
        self,
        assignment: SupervisorAssignment,
        *,
        now: datetime,
    ) -> str | None:
        """判定一次在途派活是否已经卡住；返回回收原因，None 表示健康。"""
        if assignment.status == "dispatched":
            age = _age_seconds(assignment.created_at, now)
            if age is None or age < self._dispatch_timeout_seconds:
                return None
            return f"派活后 {int(age)}s 仍未进入执行，已回收"
        if assignment.status != "running":
            return None
        if assignment.task_id is None:
            return "派活记录缺少任务关联，已回收"
        status = (await self._repo.task_statuses([assignment.task_id])).get(assignment.task_id)
        if status is None:
            return "任务记录已丢失，已回收"
        if status in _TERMINAL_TASK_VALUES:
            # 任务已结束但派活还没收尾：常规对账会物化真实结论
            return None
        if self._run_timeout_seconds <= 0:
            return None
        age = _age_seconds(assignment.started_at or assignment.created_at, now)
        if age is None or age < self._run_timeout_seconds:
            return None
        return f"执行超过 {int(age)}s 未结束，已回收"

    async def _thread_auto(
        self,
        thread: SupervisorThread,
        settings: SupervisorSettings,
    ) -> bool:
        """线程级准入判定：策略覆盖全局设置，收回 AUTO 后不再自动续派。"""
        policies = await self._repo.list_policies(
            source_key=thread.source_key, project_id=thread.project_id
        )
        policy = select_policy(policies, source_key=thread.source_key, project_id=thread.project_id)
        value = (policy.accept_mode if policy is not None else None) or settings.accept_mode
        return str(value) == AcceptMode.AUTO.value

    # --- 对账 / 查询 ---

    async def reconcile(self, *, limit: int = 50) -> int:
        """把任务的真实终态物化成报告；worker 每轮与读接口都会调用。"""
        tickets = await self._repo.list_tickets(limit=limit)
        await self._refresh(tickets)
        await self._close_threads(tickets)
        return len(tickets)

    async def overview(self, *, limit: int = 20) -> dict[str, Any]:
        settings = await self._repo.get_settings()
        tickets = await self._repo.list_tickets(limit=limit)
        await self._refresh(tickets)
        items = await self._ticket_items(tickets)
        counts: dict[str, int] = {stage.value: 0 for stage in TicketStage}
        for item in items:
            counts[item["stage"]] = counts.get(item["stage"], 0) + 1
        return {
            "settings": settings.to_dict(),
            "counts": counts,
            "tickets": items,
            "pending_outbox": await self._outbox.pending_count() if self._outbox else 0,
        }

    async def project_overview(self, project_id: uuid.UUID, *, limit: int = 50) -> dict[str, Any]:
        """项目页：项目信息 + 阶段计数 + 工单 + 最近报告 + 线程摘要 + 时间线。

        读之前先对账一次并尝试收尾线程，保证用户打开项目页时看到的是最新事实。
        """
        project = await self._repo.get_project(project_id)
        tickets = await self._repo.list_tickets(project_id=project_id, limit=limit)
        await self._refresh(tickets)
        await self._close_threads(tickets)
        items = await self._ticket_items(tickets)
        counts: dict[str, int] = {stage.value: 0 for stage in TicketStage}
        for item in items:
            counts[item["stage"]] = counts.get(item["stage"], 0) + 1
        reports = [item["report"] for item in items if item.get("report")]
        reports.sort(key=lambda report: str(report.get("created_at") or ""), reverse=True)
        digests = await self._repo.list_digests(project_id=project_id, limit=10)
        settings = await self._repo.get_settings()
        return {
            "project": project.to_dict(),
            "settings": settings.to_dict(),
            "counts": counts,
            "tickets": items,
            "reports": reports,
            "digests": [digest.to_dict() for digest in digests],
            "timeline": await self.timeline(project_id=project_id, limit=limit),
        }

    async def tickets(
        self,
        *,
        thread_id: uuid.UUID | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        tickets = await self._repo.list_tickets(thread_id=thread_id, limit=limit)
        await self._refresh(tickets)
        return await self._ticket_items(tickets)

    async def ticket_detail(self, ticket_id: uuid.UUID) -> dict[str, Any]:
        ticket = await self._repo.get_ticket(ticket_id)
        await self._refresh([ticket])
        item = (await self._ticket_items([ticket]))[0]
        item["assignments"] = [
            assignment.to_dict() for assignment in await self._repo.list_assignments(ticket_id)
        ]
        item["reports"] = [report.to_dict() for report in await self._repo.list_reports(ticket_id)]
        item["subscriptions"] = [
            subscription.to_dict()
            for subscription in await self._repo.list_subscriptions(ticket_id)
        ]
        return item

    async def reports(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """最近的报告（时间倒序），供助理看板与 MCP 只读工具使用。"""
        return [report.to_dict() for report in await self._repo.recent_reports(limit=limit)]

    async def search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """在工单、报告、线程摘要里检索；只读，空关键字由调用方拦截。"""
        return await self._repo.search(query, limit=limit)

    async def timeline(
        self,
        *,
        thread_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """把事件、工单、报告合成一条时间线（时间倒序），只读不写。"""
        cap = max(1, min(limit, 200))
        thread_key: str | None = None
        if thread_id is not None:
            thread = await self._repo.get_thread(thread_id)
            thread_key = thread.thread_key
            project_id = project_id or thread.project_id
        triggers = await self._repo.list_triggers(
            limit=cap, project_id=project_id, thread_key=thread_key
        )
        tickets = await self._repo.list_tickets(
            thread_id=thread_id,
            project_id=project_id if thread_id is None else None,
            limit=cap,
        )
        items = await self._ticket_items(tickets)
        events: list[dict[str, Any]] = []
        for trigger in triggers:
            events.append(
                {
                    "at": _iso(trigger.created_at),
                    "kind": "trigger",
                    "id": str(trigger.id),
                    "title": (trigger.content or "")[:80],
                    "status": trigger.status,
                    "source_key": trigger.source_key,
                    "actor_id": trigger.actor_id,
                }
            )
        for item in items:
            events.append(
                {
                    "at": item.get("created_at"),
                    "kind": "ticket",
                    "id": item["id"],
                    "title": item["title"],
                    "status": item["stage"],
                    "thread_id": item["thread_id"],
                }
            )
            report = item.get("report")
            if report:
                events.append(
                    {
                        "at": report.get("created_at"),
                        "kind": "report",
                        "id": report["id"],
                        "title": item["title"],
                        "status": report["status"],
                        "summary": (report.get("summary") or "")[:200],
                        "ticket_id": item["id"],
                    }
                )
        events.sort(key=lambda event: str(event.get("at") or ""), reverse=True)
        return events[:cap]

    # --- 内部 ---

    async def _subscribe_thread(
        self,
        trigger: SupervisorTrigger,
        tickets: list[SupervisorTicket],
    ) -> None:
        """外部平台来的工单自动订阅回原会话：报告好了就直通回话说的地方。

        仅对"有平台会话"的 trigger 生效（flux_internal 没有外部通道），
        每条订阅都走唯一约束幂等，重复规划不会重复投递。
        """
        source_key = (trigger.source_key or "").strip()
        thread_key = (trigger.thread_key or "").strip()
        if not thread_key or source_key in _INTERNAL_SOURCES:
            return
        for ticket in tickets:
            try:
                await self._repo.create_subscription(
                    ticket.id,
                    source_key=source_key,
                    external_id=thread_key,
                    mode="terminal",
                )
            except Exception:  # noqa: BLE001 - 订阅失败不能拖垮规划
                logger.warning(
                    "Supervisor 自动订阅失败 ticket=%s source=%s",
                    ticket.id,
                    source_key,
                    exc_info=True,
                )

    async def _refresh(self, tickets: list[SupervisorTicket]) -> None:
        """只对"在途"的工单物化报告，已收尾的工单不重复写。"""
        if not tickets:
            return
        latest = await self._repo.latest_assignments([ticket.id for ticket in tickets])
        for ticket in tickets:
            assignment = latest.get(ticket.id)
            if assignment is not None and assignment.status in {"dispatched", "running"}:
                await self._dispatcher.sync_ticket(ticket)

    async def _close_threads(self, tickets: list[SupervisorTicket]) -> None:
        """线程内工单全部收尾后生成一份摘要（按线程幂等）。

        先用本次读到的工单初筛，再回库复核整条线程：避免"线程里还有未收尾
        工单但这次没被读到"时提前写摘要。
        """
        for thread_id in {ticket.thread_id for ticket in tickets}:
            try:
                if await self._repo.get_digest_by_thread(thread_id) is not None:
                    continue
                thread_tickets = await self._repo.list_tickets(thread_id=thread_id, limit=200)
                await self._refresh(thread_tickets)
                items = await self._ticket_items(thread_tickets)
                if not is_closed(items):
                    continue
                await self._create_digest(thread_id, items)
            except Exception:  # noqa: BLE001 - 摘要失败不能拖垮对账
                logger.warning("Supervisor 生成摘要失败 thread=%s", thread_id, exc_info=True)

    async def _create_digest(self, thread_id: uuid.UUID, items: list[dict[str, Any]]) -> None:
        thread = await self._repo.get_thread(thread_id)
        project_id = thread.project_id or next(
            (
                uuid.UUID(str(item["project_id"]))
                for item in items
                if item.get("project_id") is not None
            ),
            None,
        )
        payload = build_thread_digest(
            thread_id=thread_id,
            thread_key=thread.thread_key,
            project_id=project_id,
            items=items,
        )
        _, created = await self._repo.create_digest(
            thread_id=thread_id,
            project_id=project_id,
            summary=payload["summary"],
            decisions=payload["decisions"],
            ticket_ids=payload["ticket_ids"],
        )
        if created and project_id is not None:
            await self._write_progress(project_id)

    async def _write_progress(self, project_id: uuid.UUID) -> None:
        """把该项目最近的线程摘要渲染进 Brain 的 progress 分区（覆盖写入）。"""
        if self._brain is None:
            return
        try:
            digests = await self._repo.list_digests(project_id=project_id, limit=10)
            content = render_progress([digest.to_dict() for digest in digests])
            if not content:
                return
            await self._brain.write(
                project_id,
                section=BrainSection.PROGRESS,
                content=content,
                meta={"source": "supervisor"},
            )
        except Exception:  # noqa: BLE001 - 记忆写入失败不影响摘要落库
            logger.warning("Supervisor 写入项目进度失败 project=%s", project_id, exc_info=True)

    async def _ticket_items(self, tickets: list[SupervisorTicket]) -> list[dict[str, Any]]:
        if not tickets:
            return []
        ticket_ids = [ticket.id for ticket in tickets]
        assignments = await self._repo.latest_assignments(ticket_ids)
        reports = await self._repo.latest_reports(ticket_ids)
        task_ids = [
            assignment.task_id
            for assignment in assignments.values()
            if assignment.task_id is not None
        ]
        task_statuses = await self._repo.task_statuses(task_ids)
        items: list[dict[str, Any]] = []
        for ticket in tickets:
            assignment = assignments.get(ticket.id)
            report = reports.get(ticket.id)
            stage = derive_stage(
                task_status=(
                    task_statuses.get(assignment.task_id)
                    if assignment is not None and assignment.task_id is not None
                    else None
                ),
                assignment_status=assignment.status if assignment is not None else None,
                report_status=report.status if report is not None else None,
            )
            item = ticket.to_dict(stage=stage.value)
            item["assignment"] = assignment.to_dict() if assignment is not None else None
            item["report"] = report.to_dict() if report is not None else None
            items.append(item)
        return items


def _iso(value: Any) -> str:
    """时间统一成 ISO 字符串，缺失时返回空串（排序时空串自然沉底）。"""
    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def _age_seconds(value: datetime | None, now: datetime) -> float | None:
    """计算年龄（秒）；SQLite 读回的时间可能没有 tzinfo，按 UTC 补齐。"""
    if value is None:
        return None
    moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return (now - moment).total_seconds()


def _thread_key(trigger: SupervisorTrigger) -> str:
    """线程键：平台给了就用平台的，没给就按单条事件隔离，避免串会话。"""
    key = (trigger.thread_key or "").strip()
    return key or f"{trigger.source_key}:{trigger.id.hex}"


def _plan_dict(plan: TicketPlan) -> dict[str, Any]:
    return {
        "degraded": plan.degraded,
        "reason": plan.reason,
        "latency_ms": plan.latency_ms,
        "model_meta": plan.model_meta,
        "tickets": [
            {
                "key": ticket.key,
                "title": ticket.title,
                "role": ticket.role,
                "depends_on": list(ticket.depends_on),
                "sequence": ticket.sequence,
            }
            for ticket in plan.tickets
        ],
    }


def _dependencies_done(
    ticket: SupervisorTicket,
    plan_keys: dict[uuid.UUID, str],
    stages: dict[str, str],
) -> bool:
    """依赖的工单必须全部 done；plan_key → ticket id 反查用同一份快照。"""
    if not ticket.depends_on:
        return True
    by_key = {plan_key: ticket_id for ticket_id, plan_key in plan_keys.items()}
    for dependency in ticket.depends_on:
        dependency_id = by_key.get(dependency)
        if dependency_id is None:
            continue
        if stages.get(str(dependency_id)) != TicketStage.DONE.value:
            return False
    return True
