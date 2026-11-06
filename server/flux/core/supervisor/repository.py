"""Supervisor 持久化读写。

唯一真相源分三类：

- 外部事实：`supervisor_triggers`（幂等键 = 来源 + 事件类型 + external_id）
- 判断事实：`supervisor_evals`（只追加，用于回放"当时为什么这么判"）
- 编排状态：thread / ticket / assignment / report

本文件在 P0 只覆盖"外部事实落库 + 线程收敛"，判断与派发分别由 P1 / P2 追加。
每次调用自开一个会话并提交，与仓库内其它 repository 的约定一致。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.core.supervisor.outbox import REPORT_READY_TOPIC
from flux.enums import AcceptMode, ReportStatus, SupervisorDecision
from flux.errors import BadRequestError, NotFoundError, ValidationError
from flux.logging import get_logger
from flux.models.project import Project
from flux.models.supervisor import (
    SupervisorAssignment,
    SupervisorDigest,
    SupervisorEval,
    SupervisorOutbox,
    SupervisorPolicy,
    SupervisorReport,
    SupervisorSettings,
    SupervisorSource,
    SupervisorSubscription,
    SupervisorThread,
    SupervisorTicket,
    SupervisorTrigger,
)
from flux.models.task import Task

if TYPE_CHECKING:
    from flux.core.supervisor.planner import TicketPlan

logger = get_logger(__name__)

#: 工单 plan_key 落库长度上限（与 planner 的单键上限保持一致，前缀会挤占空间）
_PLAN_KEY_LIMIT = 64

#: 可在设置接口里修改的列（白名单，避免把 singleton / 时间戳交给调用方）
_SETTINGS_FIELDS = frozenset(
    {
        "accept_mode",
        "model_provider",
        "model_name",
        "max_tickets_per_thread",
        "max_ticket_depth",
        "max_concurrent_runs",
    }
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def dedupe_key(source_key: str, event_type: str, external_id: str) -> str:
    """同来源同类型同外部 id 的稳定幂等键（64 位十六进制）。"""
    raw = "\x1f".join((source_key, event_type, external_id)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class SupervisorRepository:
    """supervisor_* 表的读写入口。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # --- 设置 ---

    async def get_settings(self) -> SupervisorSettings:
        """读取单行配置；首次访问时创建默认行（observe 模式）。"""
        async with self._session_factory() as session:
            settings = await session.scalar(
                select(SupervisorSettings).where(SupervisorSettings.singleton == 1)
            )
            if settings is None:
                settings = SupervisorSettings(id=uuid.uuid4(), singleton=1)
                session.add(settings)
                await self._commit(session, "Supervisor 设置")
                await session.refresh(settings)
            return settings

    async def update_settings(self, **fields: Any) -> SupervisorSettings:
        """按白名单更新设置；未知字段直接拒绝，避免静默丢参数。"""
        unknown = set(fields) - _SETTINGS_FIELDS
        if unknown:
            raise ValidationError(
                "不支持的 Supervisor 设置项",
                details={"unknown": sorted(unknown)},
            )
        if "accept_mode" in fields:
            _require_accept_mode(fields["accept_mode"])
        async with self._session_factory() as session:
            settings = await session.scalar(
                select(SupervisorSettings).where(SupervisorSettings.singleton == 1)
            )
            if settings is None:
                settings = SupervisorSettings(id=uuid.uuid4(), singleton=1)
                session.add(settings)
            for key, value in fields.items():
                setattr(settings, key, value)
            await self._commit(session, "Supervisor 设置")
            await session.refresh(settings)
            return settings

    # --- 来源 ---

    async def ensure_source(self, key: str, *, kind: str, name: str = "") -> SupervisorSource:
        """按 key 取来源，不存在则登记（事件先到、配置后补是常态）。"""
        async with self._session_factory() as session:
            source = await session.scalar(
                select(SupervisorSource).where(SupervisorSource.key == key)
            )
            if source is None:
                source = SupervisorSource(id=uuid.uuid4(), key=key, kind=kind, name=name or key)
                session.add(source)
                await self._commit(session, "Supervisor 来源")
                await session.refresh(source)
            return source

    async def get_source(self, key: str) -> SupervisorSource | None:
        async with self._session_factory() as session:
            return await session.scalar(select(SupervisorSource).where(SupervisorSource.key == key))

    async def list_sources(self) -> list[SupervisorSource]:
        async with self._session_factory() as session:
            return list(
                await session.scalars(select(SupervisorSource).order_by(SupervisorSource.key))
            )

    # --- 策略 ---

    async def list_policies(
        self,
        *,
        source_key: str,
        project_id: uuid.UUID | None = None,
    ) -> list[SupervisorPolicy]:
        """取候选策略：项目/来源为空的策略视为通配，由 gate.select_policy 排优先级。"""
        statement = select(SupervisorPolicy).where(SupervisorPolicy.enabled.is_(True))
        statement = statement.where(
            (SupervisorPolicy.source_key.is_(None)) | (SupervisorPolicy.source_key == source_key)
        )
        if project_id is None:
            statement = statement.where(SupervisorPolicy.project_id.is_(None))
        else:
            statement = statement.where(
                (SupervisorPolicy.project_id.is_(None))
                | (SupervisorPolicy.project_id == project_id)
            )
        statement = statement.order_by(SupervisorPolicy.created_at, SupervisorPolicy.id)
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    # --- 事件（幂等）---

    async def ingest(
        self,
        *,
        source_key: str,
        external_id: str,
        event_type: str = "message",
        actor_id: str = "",
        actor_platform: str = "",
        thread_key: str = "",
        content: str = "",
        raw: dict | None = None,
        project_id: uuid.UUID | None = None,
        source_kind: str = "external",
    ) -> tuple[SupervisorTrigger, bool]:
        """落一条外部事实。

        返回 `(trigger, created)`：`created=False` 表示该事件此前已入库（重复投递），
        此时绝不重复写 outbox —— 这是"重启不重放、重投不重复派活"的唯一依据。
        """
        key = dedupe_key(source_key, event_type, external_id)
        now = _utcnow()
        async with self._session_factory() as session:
            existing = await session.scalar(
                select(SupervisorTrigger).where(SupervisorTrigger.dedupe_key == key)
            )
            if existing is not None:
                return existing, False

            await self._require_project(session, project_id)
            await self._ensure_source_row(session, source_key, source_kind)

            trigger = SupervisorTrigger(
                id=uuid.uuid4(),
                source_key=source_key,
                external_id=external_id,
                event_type=event_type,
                dedupe_key=key,
                actor_id=actor_id,
                actor_platform=actor_platform,
                thread_key=thread_key,
                project_id=project_id,
                content=content,
                raw=raw or {},
                status="received",
                received_at=now,
            )
            session.add(trigger)
            if thread_key:
                await self._touch_thread(
                    session,
                    thread_key=thread_key,
                    source_key=source_key,
                    external_id=external_id,
                    project_id=project_id,
                    now=now,
                )
            # 与事件同事务入队：进程在提交后崩溃也能按 outbox 重放后续动作。
            session.add(
                SupervisorOutbox(
                    id=uuid.uuid4(),
                    topic="supervisor.trigger_received",
                    payload={"trigger_id": str(trigger.id)},
                    status="pending",
                    attempts=0,
                    available_at=now,
                )
            )
            try:
                await self._commit(session, "Supervisor 事件")
            except BadRequestError:
                # 并发投递同一事件：唯一约束把后到者挡下，回读既有行即可。
                await session.rollback()
                duplicate = await session.scalar(
                    select(SupervisorTrigger).where(SupervisorTrigger.dedupe_key == key)
                )
                if duplicate is not None:
                    return duplicate, False
                raise
            await session.refresh(trigger)
            return trigger, True

    async def list_triggers(
        self,
        *,
        limit: int = 50,
        status: str | None = None,
        project_id: uuid.UUID | None = None,
        thread_key: str | None = None,
    ) -> list[SupervisorTrigger]:
        statement = select(SupervisorTrigger).order_by(
            SupervisorTrigger.created_at.desc(), SupervisorTrigger.id.desc()
        )
        if status is not None:
            statement = statement.where(SupervisorTrigger.status == status)
        if project_id is not None:
            statement = statement.where(SupervisorTrigger.project_id == project_id)
        if thread_key is not None:
            statement = statement.where(SupervisorTrigger.thread_key == thread_key)
        statement = statement.limit(max(1, min(limit, 200)))
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def get_trigger(self, trigger_id: uuid.UUID) -> SupervisorTrigger:
        async with self._session_factory() as session:
            trigger = await session.get(SupervisorTrigger, trigger_id)
            if trigger is None:
                raise NotFoundError(
                    f"Supervisor 事件 {trigger_id} 不存在", details={"trigger_id": str(trigger_id)}
                )
            return trigger

    async def set_trigger_status(self, trigger_id: uuid.UUID, status: str) -> None:
        """推进 trigger 的处理状态（received → gated → assessed / ignored）。"""
        async with self._session_factory() as session:
            trigger = await session.get(SupervisorTrigger, trigger_id)
            if trigger is None:
                raise NotFoundError(
                    f"Supervisor 事件 {trigger_id} 不存在", details={"trigger_id": str(trigger_id)}
                )
            trigger.status = status
            await self._commit(session, "Supervisor 事件状态")

    async def count_triggers_since(
        self,
        *,
        source_key: str,
        since: datetime,
        exclude_id: uuid.UUID | None = None,
    ) -> int:
        """统计窗口内同一来源的事件数（Gate 的速率规则用）。"""
        statement = (
            select(func.count())
            .select_from(SupervisorTrigger)
            .where(
                SupervisorTrigger.source_key == source_key,
                SupervisorTrigger.received_at >= since,
            )
        )
        if exclude_id is not None:
            statement = statement.where(SupervisorTrigger.id != exclude_id)
        async with self._session_factory() as session:
            return int(await session.scalar(statement) or 0)

    # --- 判断（只追加）---

    async def record_eval(
        self,
        trigger_id: uuid.UUID,
        *,
        decision: SupervisorDecision,
        reason: str = "",
        confidence: float | None = None,
        risk: dict | None = None,
        model_meta: dict | None = None,
        latency_ms: int | None = None,
    ) -> SupervisorEval:
        evaluation = SupervisorEval(
            id=uuid.uuid4(),
            trigger_id=trigger_id,
            decision=decision.value,
            confidence=confidence,
            reason=reason,
            risk=risk or {},
            model_meta=model_meta or {},
            latency_ms=latency_ms,
        )
        async with self._session_factory() as session:
            session.add(evaluation)
            await self._commit(session, "Supervisor 判断")
            await session.refresh(evaluation)
            return evaluation

    async def latest_evals(self, trigger_ids: list[uuid.UUID]) -> dict[uuid.UUID, SupervisorEval]:
        """按 trigger 取最新一条判断；没有判断的事件不出现在结果里。"""
        if not trigger_ids:
            return {}
        statement = (
            select(SupervisorEval)
            .where(SupervisorEval.trigger_id.in_(trigger_ids))
            .order_by(SupervisorEval.created_at, SupervisorEval.id)
        )
        latest: dict[uuid.UUID, SupervisorEval] = {}
        async with self._session_factory() as session:
            for evaluation in await session.scalars(statement):
                latest[evaluation.trigger_id] = evaluation
        return latest

    async def list_evals(self, trigger_id: uuid.UUID) -> list[SupervisorEval]:
        """一条事件的全部判断历史（升序）；手动重判会追加新行，旧结论不覆盖。"""
        statement = (
            select(SupervisorEval)
            .where(SupervisorEval.trigger_id == trigger_id)
            .order_by(SupervisorEval.created_at, SupervisorEval.id)
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    # --- 线程 ---

    async def ensure_thread(
        self,
        *,
        thread_key: str,
        source_key: str,
        external_id: str = "",
        project_id: uuid.UUID | None = None,
    ) -> SupervisorThread:
        """按 thread_key 取线程，不存在则登记；项目关联以后到的为准。"""
        now = _utcnow()
        async with self._session_factory() as session:
            thread = await session.scalar(
                select(SupervisorThread).where(SupervisorThread.thread_key == thread_key)
            )
            if thread is None:
                thread = SupervisorThread(
                    id=uuid.uuid4(),
                    thread_key=thread_key,
                    source_key=source_key,
                    external_id=external_id,
                    project_id=project_id,
                    last_activity_at=now,
                )
                session.add(thread)
            else:
                thread.last_activity_at = now
                if project_id is not None:
                    thread.project_id = project_id
            await self._commit(session, "Supervisor 线程")
            await session.refresh(thread)
            return thread

    async def get_thread(self, thread_id: uuid.UUID) -> SupervisorThread:
        async with self._session_factory() as session:
            thread = await session.get(SupervisorThread, thread_id)
            if thread is None:
                raise NotFoundError(
                    f"Supervisor 线程 {thread_id} 不存在", details={"thread_id": str(thread_id)}
                )
            return thread

    async def get_thread_by_key(self, thread_key: str) -> SupervisorThread | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(SupervisorThread).where(SupervisorThread.thread_key == thread_key)
            )

    # --- 工单 ---

    async def create_tickets(
        self,
        *,
        thread_id: uuid.UUID,
        project_id: uuid.UUID | None,
        plan: TicketPlan,
        key_prefix: str,
    ) -> list[SupervisorTicket]:
        """落一份计划；同一前缀重复规划直接回读已有工单，不重复拆树。"""
        prefix = (key_prefix or "").strip()
        stored_keys = [f"{prefix}-{ticket.key}"[:_PLAN_KEY_LIMIT] for ticket in plan.tickets]

        async with self._session_factory() as session:
            existing = list(
                await session.scalars(
                    select(SupervisorTicket)
                    .where(
                        SupervisorTicket.thread_id == thread_id,
                        SupervisorTicket.plan_key.in_(stored_keys),
                    )
                    .order_by(SupervisorTicket.sequence, SupervisorTicket.id)
                )
            )
            if existing:
                return existing

            rows: list[SupervisorTicket] = []
            for ticket, stored in zip(plan.tickets, stored_keys, strict=True):
                row = SupervisorTicket(
                    id=uuid.uuid4(),
                    thread_id=thread_id,
                    project_id=project_id,
                    plan_key=stored,
                    title=ticket.title,
                    description=ticket.description,
                    role=ticket.role,
                    depends_on=[
                        f"{prefix}-{dependency}"[:_PLAN_KEY_LIMIT]
                        for dependency in ticket.depends_on
                    ],
                    degraded=bool(plan.degraded),
                    sequence=ticket.sequence,
                )
                session.add(row)
                rows.append(row)
            await self._commit(session, "Supervisor 工单")
            for row in rows:
                await session.refresh(row)
            return rows

    async def list_tickets(
        self,
        *,
        thread_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        limit: int = 100,
    ) -> list[SupervisorTicket]:
        statement = select(SupervisorTicket).order_by(
            SupervisorTicket.created_at.desc(), SupervisorTicket.sequence, SupervisorTicket.id
        )
        if thread_id is not None:
            statement = statement.where(SupervisorTicket.thread_id == thread_id)
        if project_id is not None:
            statement = statement.where(SupervisorTicket.project_id == project_id)
        statement = statement.limit(max(1, min(limit, 200)))
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def get_ticket(self, ticket_id: uuid.UUID) -> SupervisorTicket:
        async with self._session_factory() as session:
            ticket = await session.get(SupervisorTicket, ticket_id)
            if ticket is None:
                raise NotFoundError(
                    f"Supervisor 工单 {ticket_id} 不存在", details={"ticket_id": str(ticket_id)}
                )
            return ticket

    async def get_tickets(self, ticket_ids: list[uuid.UUID]) -> dict[uuid.UUID, SupervisorTicket]:
        """按 id 批量取工单（watchdog 一次读齐在途派活对应的工单）。"""
        if not ticket_ids:
            return {}
        statement = select(SupervisorTicket).where(SupervisorTicket.id.in_(ticket_ids))
        async with self._session_factory() as session:
            return {ticket.id: ticket for ticket in await session.scalars(statement)}

    async def set_ticket_task(
        self, ticket_id: uuid.UUID, task_id: uuid.UUID | None
    ) -> SupervisorTicket:
        async with self._session_factory() as session:
            ticket = await session.get(SupervisorTicket, ticket_id)
            if ticket is None:
                raise NotFoundError(
                    f"Supervisor 工单 {ticket_id} 不存在", details={"ticket_id": str(ticket_id)}
                )
            ticket.task_id = task_id
            await self._commit(session, "Supervisor 工单任务")
            await session.refresh(ticket)
            return ticket

    # --- 派活记录 ---

    async def create_assignment(
        self,
        ticket_id: uuid.UUID,
        *,
        agent_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        run_id: str | None = None,
        status: str = "dispatched",
    ) -> SupervisorAssignment:
        assignment = SupervisorAssignment(
            id=uuid.uuid4(),
            ticket_id=ticket_id,
            agent_id=agent_id,
            task_id=task_id,
            run_id=run_id,
            status=status,
        )
        async with self._session_factory() as session:
            session.add(assignment)
            await self._commit(session, "Supervisor 派活")
            await session.refresh(assignment)
            return assignment

    async def update_assignment(self, assignment_id: uuid.UUID, **fields: Any) -> None:
        """只更新显式给出的列（status / run_id / started_at / finished_at）。"""
        async with self._session_factory() as session:
            assignment = await session.get(SupervisorAssignment, assignment_id)
            if assignment is None:
                raise NotFoundError(
                    f"Supervisor 派活记录 {assignment_id} 不存在",
                    details={"assignment_id": str(assignment_id)},
                )
            for key, value in fields.items():
                setattr(assignment, key, value)
            await self._commit(session, "Supervisor 派活状态")

    async def latest_assignments(
        self, ticket_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, SupervisorAssignment]:
        if not ticket_ids:
            return {}
        statement = (
            select(SupervisorAssignment)
            .where(SupervisorAssignment.ticket_id.in_(ticket_ids))
            .order_by(SupervisorAssignment.created_at, SupervisorAssignment.id)
        )
        latest: dict[uuid.UUID, SupervisorAssignment] = {}
        async with self._session_factory() as session:
            for assignment in await session.scalars(statement):
                latest[assignment.ticket_id] = assignment
        return latest

    async def list_assignments(self, ticket_id: uuid.UUID) -> list[SupervisorAssignment]:
        statement = (
            select(SupervisorAssignment)
            .where(SupervisorAssignment.ticket_id == ticket_id)
            .order_by(SupervisorAssignment.created_at, SupervisorAssignment.id)
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def active_assignments(self, *, limit: int = 200) -> list[SupervisorAssignment]:
        """在途派活记录（dispatched / running），watchdog 按租约超时回收。"""
        statement = (
            select(SupervisorAssignment)
            .where(SupervisorAssignment.status.in_(("dispatched", "running")))
            .order_by(SupervisorAssignment.created_at, SupervisorAssignment.id)
            .limit(max(1, min(limit, 500)))
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    # --- 报告（只追加）---

    async def create_report(
        self,
        ticket_id: uuid.UUID,
        *,
        status: ReportStatus | str,
        summary: str = "",
        payload: dict | None = None,
        assignment_id: uuid.UUID | None = None,
    ) -> SupervisorReport:
        report = SupervisorReport(
            id=uuid.uuid4(),
            ticket_id=ticket_id,
            assignment_id=assignment_id,
            status=str(status),
            summary=summary,
            payload=payload or {},
        )
        async with self._session_factory() as session:
            session.add(report)
            # 报告与"投递这件事"同事务入队：崩溃后由 outbox 重放，且 dedupe_key
            # 唯一保证同一份报告只投递一次（订阅直通不重复发消息）。
            session.add(
                SupervisorOutbox(
                    id=uuid.uuid4(),
                    topic=REPORT_READY_TOPIC,
                    payload={"report_id": str(report.id), "ticket_id": str(ticket_id)},
                    dedupe_key=f"report:{report.id}",
                    status="pending",
                    attempts=0,
                )
            )
            await self._commit(session, "Supervisor 报告")
            await session.refresh(report)
            return report

    async def get_report(self, report_id: uuid.UUID) -> SupervisorReport:
        async with self._session_factory() as session:
            report = await session.get(SupervisorReport, report_id)
            if report is None:
                raise NotFoundError(
                    f"Supervisor 报告 {report_id} 不存在", details={"report_id": str(report_id)}
                )
            return report

    async def recent_reports(self, *, limit: int = 50) -> list[SupervisorReport]:
        """最近报告（时间倒序）：助理看板与 MCP 只读工具共用。"""
        statement = (
            select(SupervisorReport)
            .order_by(SupervisorReport.created_at.desc(), SupervisorReport.id.desc())
            .limit(max(1, min(limit, 200)))
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def latest_reports(
        self, ticket_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, SupervisorReport]:
        if not ticket_ids:
            return {}
        statement = (
            select(SupervisorReport)
            .where(SupervisorReport.ticket_id.in_(ticket_ids))
            .order_by(SupervisorReport.created_at, SupervisorReport.id)
        )
        latest: dict[uuid.UUID, SupervisorReport] = {}
        async with self._session_factory() as session:
            for report in await session.scalars(statement):
                latest[report.ticket_id] = report
        return latest

    async def list_reports(self, ticket_id: uuid.UUID) -> list[SupervisorReport]:
        statement = (
            select(SupervisorReport)
            .where(SupervisorReport.ticket_id == ticket_id)
            .order_by(SupervisorReport.created_at, SupervisorReport.id)
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    # --- 订阅直通 ---

    async def create_subscription(
        self,
        ticket_id: uuid.UUID,
        *,
        source_key: str,
        external_id: str,
        mode: str = "terminal",
    ) -> tuple[SupervisorSubscription, bool]:
        """幂等建订阅：同一工单对同一平台线程只有一条，重复调用返回既有行。"""
        async with self._session_factory() as session:
            existing = await session.scalar(
                select(SupervisorSubscription).where(
                    SupervisorSubscription.ticket_id == ticket_id,
                    SupervisorSubscription.source_key == source_key,
                    SupervisorSubscription.external_id == external_id,
                )
            )
            if existing is not None:
                return existing, False
            subscription = SupervisorSubscription(
                id=uuid.uuid4(),
                ticket_id=ticket_id,
                source_key=source_key,
                external_id=external_id,
                mode=mode,
            )
            session.add(subscription)
            try:
                await self._commit(session, "Supervisor 订阅")
            except BadRequestError:
                # 并发建同一条订阅：唯一索引挡下后到者，回读既有行即可。
                await session.rollback()
                duplicate = await session.scalar(
                    select(SupervisorSubscription).where(
                        SupervisorSubscription.ticket_id == ticket_id,
                        SupervisorSubscription.source_key == source_key,
                        SupervisorSubscription.external_id == external_id,
                    )
                )
                if duplicate is not None:
                    return duplicate, False
                raise
            await session.refresh(subscription)
            return subscription, True

    async def list_subscriptions(self, ticket_id: uuid.UUID) -> list[SupervisorSubscription]:
        statement = (
            select(SupervisorSubscription)
            .where(SupervisorSubscription.ticket_id == ticket_id)
            .order_by(SupervisorSubscription.created_at, SupervisorSubscription.id)
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def get_subscription(self, subscription_id: uuid.UUID) -> SupervisorSubscription:
        async with self._session_factory() as session:
            subscription = await session.get(SupervisorSubscription, subscription_id)
            if subscription is None:
                raise NotFoundError(
                    f"Supervisor 订阅 {subscription_id} 不存在",
                    details={"subscription_id": str(subscription_id)},
                )
            return subscription

    async def delete_subscription(self, subscription_id: uuid.UUID) -> None:
        async with self._session_factory() as session:
            subscription = await session.get(SupervisorSubscription, subscription_id)
            if subscription is None:
                raise NotFoundError(
                    f"Supervisor 订阅 {subscription_id} 不存在",
                    details={"subscription_id": str(subscription_id)},
                )
            await session.delete(subscription)
            await self._commit(session, "Supervisor 订阅")

    async def task_statuses(self, task_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        """批量读任务状态（工单阶段派生只信 tasks 表）。"""
        if not task_ids:
            return {}
        statement = select(Task.id, Task.status).where(Task.id.in_(task_ids))
        async with self._session_factory() as session:
            return {row[0]: str(row[1]) for row in await session.execute(statement)}

    # --- 摘要（线程收尾后生成一次）---

    async def create_digest(
        self,
        *,
        thread_id: uuid.UUID,
        project_id: uuid.UUID | None,
        summary: str,
        decisions: list[dict] | None = None,
        ticket_ids: list[str] | None = None,
    ) -> tuple[SupervisorDigest, bool]:
        """按线程幂等写摘要：已有则回读，不覆盖（摘要只陈述收尾时的结论）。"""
        async with self._session_factory() as session:
            existing = await session.scalar(
                select(SupervisorDigest).where(SupervisorDigest.thread_id == thread_id)
            )
            if existing is not None:
                return existing, False
            digest = SupervisorDigest(
                id=uuid.uuid4(),
                thread_id=thread_id,
                project_id=project_id,
                summary=summary,
                decisions=decisions or [],
                ticket_ids=ticket_ids or [],
            )
            session.add(digest)
            try:
                await self._commit(session, "Supervisor 摘要")
            except BadRequestError:
                await session.rollback()
                duplicate = await session.scalar(
                    select(SupervisorDigest).where(SupervisorDigest.thread_id == thread_id)
                )
                if duplicate is not None:
                    return duplicate, False
                raise
            await session.refresh(digest)
            return digest, True

    async def get_digest_by_thread(self, thread_id: uuid.UUID) -> SupervisorDigest | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(SupervisorDigest).where(SupervisorDigest.thread_id == thread_id)
            )

    async def list_digests(
        self,
        *,
        thread_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        limit: int = 20,
    ) -> list[SupervisorDigest]:
        statement = select(SupervisorDigest).order_by(
            SupervisorDigest.created_at.desc(), SupervisorDigest.id.desc()
        )
        if thread_id is not None:
            statement = statement.where(SupervisorDigest.thread_id == thread_id)
        if project_id is not None:
            statement = statement.where(SupervisorDigest.project_id == project_id)
        statement = statement.limit(max(1, min(limit, 200)))
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    # --- 项目 / 检索 ---

    async def get_project(self, project_id: uuid.UUID) -> Project:
        async with self._session_factory() as session:
            project = await session.get(Project, project_id)
            if project is None:
                raise NotFoundError(
                    f"项目 {project_id} 不存在", details={"project_id": str(project_id)}
                )
            return project

    async def search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """在工单、报告、线程摘要里做 LIKE 检索，按时间倒序合并。

        `%` / `_` / `\\` 会转义成字面量：搜 "50%" 不会退化成通配符。
        只返回平台已记录的事实，不改任何状态。
        """
        pattern = _like_pattern(query)
        cap = max(1, min(limit, 200))
        hits: list[dict[str, Any]] = []
        async with self._session_factory() as session:
            tickets = await session.scalars(
                select(SupervisorTicket)
                .where(
                    or_(
                        SupervisorTicket.title.ilike(pattern, escape="\\"),
                        SupervisorTicket.description.ilike(pattern, escape="\\"),
                    )
                )
                .order_by(SupervisorTicket.created_at.desc(), SupervisorTicket.id.desc())
                .limit(cap)
            )
            for ticket in tickets:
                hits.append(
                    {
                        "kind": "ticket",
                        "id": str(ticket.id),
                        "title": ticket.title,
                        "snippet": _snippet(ticket.description or ticket.title, query),
                        "thread_id": str(ticket.thread_id),
                        "project_id": (
                            str(ticket.project_id) if ticket.project_id is not None else None
                        ),
                        "status": None,
                        "at": _iso(ticket.created_at),
                    }
                )

            reports = await session.scalars(
                select(SupervisorReport)
                .where(SupervisorReport.summary.ilike(pattern, escape="\\"))
                .order_by(SupervisorReport.created_at.desc(), SupervisorReport.id.desc())
                .limit(cap)
            )
            for report in reports:
                hits.append(
                    {
                        "kind": "report",
                        "id": str(report.id),
                        "title": _snippet(report.summary, query),
                        "snippet": _snippet(report.summary, query),
                        "ticket_id": str(report.ticket_id),
                        "thread_id": None,
                        "project_id": None,
                        "status": report.status,
                        "at": _iso(report.created_at),
                    }
                )

            digests = await session.scalars(
                select(SupervisorDigest)
                .where(SupervisorDigest.summary.ilike(pattern, escape="\\"))
                .order_by(SupervisorDigest.created_at.desc(), SupervisorDigest.id.desc())
                .limit(cap)
            )
            for digest in digests:
                hits.append(
                    {
                        "kind": "digest",
                        "id": str(digest.id),
                        "title": _snippet(digest.summary, query),
                        "snippet": _snippet(digest.summary, query),
                        "thread_id": (
                            str(digest.thread_id) if digest.thread_id is not None else None
                        ),
                        "project_id": (
                            str(digest.project_id) if digest.project_id is not None else None
                        ),
                        "status": None,
                        "at": _iso(digest.created_at),
                    }
                )

        hits.sort(key=lambda hit: str(hit.get("at") or ""), reverse=True)
        return hits[:cap]

    # --- 内部 ---

    @staticmethod
    async def _require_project(session: AsyncSession, project_id: uuid.UUID | None) -> None:
        if project_id is None:
            return
        exists = await session.scalar(select(Project.id).where(Project.id == project_id))
        if exists is None:
            raise NotFoundError(
                f"项目 {project_id} 不存在", details={"project_id": str(project_id)}
            )

    @staticmethod
    async def _ensure_source_row(session: AsyncSession, key: str, kind: str) -> None:
        exists = await session.scalar(
            select(SupervisorSource.id).where(SupervisorSource.key == key)
        )
        if exists is None:
            session.add(SupervisorSource(id=uuid.uuid4(), key=key, kind=kind, name=key))

    @staticmethod
    async def _touch_thread(
        session: AsyncSession,
        *,
        thread_key: str,
        source_key: str,
        external_id: str,
        project_id: uuid.UUID | None,
        now: datetime,
    ) -> None:
        thread = await session.scalar(
            select(SupervisorThread).where(SupervisorThread.thread_key == thread_key)
        )
        if thread is None:
            session.add(
                SupervisorThread(
                    id=uuid.uuid4(),
                    thread_key=thread_key,
                    source_key=source_key,
                    external_id=external_id,
                    project_id=project_id,
                    last_activity_at=now,
                )
            )
            return
        thread.last_activity_at = now
        if project_id is not None:
            thread.project_id = project_id

    @staticmethod
    async def _commit(session: AsyncSession, label: str) -> None:
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            logger.warning("%s写入违反数据库约束：%s", label, exc.orig)
            raise BadRequestError(f"{label}写入违反数据库约束：{exc.orig}") from exc


def _require_accept_mode(value: object) -> None:
    try:
        AcceptMode(str(value))
    except ValueError as exc:
        raise ValidationError(
            "accept_mode 只能是 off / observe / auto",
            details={"accept_mode": str(value)},
        ) from exc


def _like_pattern(query: str) -> str:
    """把用户输入转成 LIKE 模式：转义符、通配符一律按字面量处理。"""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _snippet(text: str, query: str, *, size: int = 160) -> str:
    """截取命中位置附近的一段文本，避免把整段报告塞进检索结果。"""
    clean = " ".join((text or "").split())
    if len(clean) <= size:
        return clean
    index = clean.lower().find((query or "").lower())
    if index < 0:
        return f"{clean[:size]}…"
    start = max(0, index - size // 3)
    end = min(len(clean), start + size)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(clean) else ""
    return f"{prefix}{clean[start:end]}{suffix}"


def _iso(value: Any) -> str:
    """时间统一成 ISO 字符串，缺失时返回空串（排序时空串自然沉底）。"""
    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)
