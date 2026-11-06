"""Supervisor（助理）领域模型。

参考 DeepSeek Super-agent 的 Source / Trigger / Thread / Eval / Mandate / Ticket /
Assignment / Report / Outbox 分层，但按 Flux 的多项目、多平台场景重写：

- 外部内容一律先落 `supervisor_triggers`（原始事实），再经无状态的 Gate / Assessment
  产生 `supervisor_evals`（判断事实）；判断层不持有任何状态。
- 真正有状态的只有 `supervisor_threads` / `tickets` / `ticket_assignments` /
  `ticket_reports`；执行仍归既有 Task Engine，本包只负责编排。
- `supervisor_outbox` 保证"先存后做"：事件落库与后续动作在同一事务里排队，
  进程崩溃后按 outbox 重放，不依赖内存队列。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SupervisorSettings(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """单行配置表（singleton=1）。

    配置落库而不是只放环境变量：准入模式要在运行中切换，且观察期需要重启后保持。
    """

    __tablename__ = "supervisor_settings"

    singleton: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1", unique=True
    )
    #: 取值见 flux.enums.AcceptMode；默认 observe，绝不默认自动派活
    accept_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="observe", server_default="observe"
    )
    #: 判断/规划用的平台模型（留空 = 用 ModelRouter 默认供应商）
    model_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: 单个线程最多拆几个子任务（硬上限，防止模型给出爆炸式计划）
    max_tickets_per_thread: Mapped[int] = mapped_column(
        Integer, nullable=False, default=6, server_default="6"
    )
    #: 计划树最大层数（1 = 只有叶子，2 = 允许一层依赖）
    max_ticket_depth: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    #: 自动模式的并发上限（同一线程同时派发的 Run 数）
    max_concurrent_runs: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "accept_mode": self.accept_mode,
            "model_provider": self.model_provider,
            "model_name": self.model_name,
            "max_tickets_per_thread": self.max_tickets_per_thread,
            "max_ticket_depth": self.max_ticket_depth,
            "max_concurrent_runs": self.max_concurrent_runs,
            "updated_at": _iso(self.updated_at),
        }


class SupervisorSource(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """事件来源（Flux 内部 / 飞书 / 微信 …）。key 是稳定标识，不随后端重启变化。"""

    __tablename__ = "supervisor_sources"

    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: flux_internal / feishu / wechat
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    #: 来源级配置（如飞书的 verification token 对应环境变量名，不存明文密钥）
    config: Mapped[dict] = mapped_column(JSON, default=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "key": self.key,
            "kind": self.kind,
            "name": self.name,
            "enabled": bool(self.enabled),
            "config": self.config or {},
        }


class SupervisorPolicy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """准入策略：按项目 / 来源覆盖 AcceptMode，并给出 Gate 的硬规则。"""

    __tablename__ = "supervisor_policies"

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True
    )
    source_key: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    #: 只处理这些事件类型；空列表 = 不限制
    trigger_types: Mapped[list] = mapped_column(JSON, default=list)
    #: 覆盖 AcceptMode；None = 用 supervisor_settings
    accept_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: Gate 规则（max_events_per_window / window_seconds / ignore_actors …）
    rules: Mapped[dict] = mapped_column(JSON, default=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id) if self.project_id is not None else None,
            "source_key": self.source_key,
            "name": self.name,
            "enabled": bool(self.enabled),
            "trigger_types": list(self.trigger_types or []),
            "accept_mode": self.accept_mode,
            "rules": self.rules or {},
        }


class SupervisorTrigger(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一条外部事实（消息 / webhook / 内部指令）。

    `dedupe_key` 唯一：同一来源的同一事件重复投递只落一行，这是幂等的唯一依据。
    原始内容存 `raw`，规范字段单独列出——判断层只读规范字段，永不执行原始内容。
    """

    __tablename__ = "supervisor_triggers"

    source_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, default="message")
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: 平台内的说话人标识（不同平台永不互相映射）
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    actor_platform: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    #: 线程/会话标识（飞书 chat_id、Flux 对话 id …）
    thread_key: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 原始请求体（只作审计，不进入模型指令）
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    #: received / gated / assessed / queued / ignored
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="received", server_default="received", index=True
    )
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "source_key": self.source_key,
            "external_id": self.external_id,
            "event_type": self.event_type,
            "actor_id": self.actor_id,
            "actor_platform": self.actor_platform,
            "thread_key": self.thread_key,
            "project_id": str(self.project_id) if self.project_id is not None else None,
            "content": self.content,
            "status": self.status,
            "received_at": _iso(self.received_at),
            "created_at": _iso(self.created_at),
        }


class SupervisorThread(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一段持续对话（同一来源 + 同一 thread_key）。线程是授权的边界。"""

    __tablename__ = "supervisor_threads"

    thread_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    source_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "thread_key": self.thread_key,
            "source_key": self.source_key,
            "project_id": str(self.project_id) if self.project_id is not None else None,
            "title": self.title,
            "summary": self.summary,
            "last_activity_at": _iso(self.last_activity_at),
        }


class SupervisorEval(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一次无状态判断的结论（只追加，不更新）。

    回放能力来自这里：任何"当时为什么这么判"都能按 trigger_id 找回来。
    """

    __tablename__ = "supervisor_evals"

    trigger_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("supervisor_triggers.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 判断层识别出的风险点（不改写原事件，只标注）
    risk: Mapped[dict] = mapped_column(JSON, default=dict)
    #: 真实模型调用元信息（provider / model / usage / latency_ms）
    model_meta: Mapped[dict] = mapped_column(JSON, default=dict)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "trigger_id": str(self.trigger_id),
            "decision": self.decision,
            "confidence": self.confidence,
            "reason": self.reason,
            "risk": self.risk or {},
            "model": self.model_meta or {},
            "latency_ms": self.latency_ms,
            "created_at": _iso(self.created_at),
        }


class SupervisorMandate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """用户对某个线程的授权。不可变：撤销写 `revoked_at`，不修改原记录。"""

    __tablename__ = "supervisor_mandates"

    thread_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("supervisor_threads.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_platform: Mapped[str] = mapped_column(String(32), nullable=False)
    strength: Mapped[str] = mapped_column(String(16), nullable=False, default="explicit")
    #: 授权范围（allow_dispatch / allow_projects / max_tickets …）
    scope: Mapped[dict] = mapped_column(JSON, default=dict)
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "thread_id": str(self.thread_id),
            "actor_id": self.actor_id,
            "actor_platform": self.actor_platform,
            "strength": self.strength,
            "scope": self.scope or {},
            "granted_at": _iso(self.granted_at),
            "expires_at": _iso(self.expires_at),
            "revoked_at": _iso(self.revoked_at),
        }


class SupervisorTicket(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一个可派活的工单。1:1 关联既有 tasks 行，绝不加宽 tasks 表。"""

    __tablename__ = "supervisor_tickets"

    thread_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("supervisor_threads.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    parent_ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("supervisor_tickets.id", ondelete="CASCADE"), nullable=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    #: 由 Supervisor 创建的 Task；唯一，保证一个工单只对应一个执行任务
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    plan_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 期望的执行角色（必须能在 agents 表里找到）
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="developer")
    #: 依赖的同线程 plan_key 列表（只有全部 done 才派发）
    depends_on: Mapped[list] = mapped_column(JSON, default=list)
    #: 模型未能给出完整计划时由平台兜底生成的单工单
    degraded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    #: 派发顺序（小的先派）
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def to_dict(self, *, stage: str | None = None) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "thread_id": str(self.thread_id),
            "parent_ticket_id": (
                str(self.parent_ticket_id) if self.parent_ticket_id is not None else None
            ),
            "project_id": str(self.project_id) if self.project_id is not None else None,
            "task_id": str(self.task_id) if self.task_id is not None else None,
            "plan_key": self.plan_key,
            "title": self.title,
            "description": self.description,
            "role": self.role,
            "depends_on": list(self.depends_on or []),
            "degraded": bool(self.degraded),
            "sequence": self.sequence,
            "stage": stage,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class SupervisorSubscription(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """订阅直通：发生在这个工单上的报告，同时投递回指定平台线程。"""

    __tablename__ = "supervisor_subscriptions"
    __table_args__ = (
        # 同一工单对同一平台线程只允许一条订阅，否则一条报告会被发多次
        UniqueConstraint(
            "ticket_id",
            "source_key",
            "external_id",
            name="ix_supervisor_subscriptions_target",
        ),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("supervisor_tickets.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    #: 投递策略：all / terminal / needs_input
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="terminal", server_default="terminal"
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "ticket_id": str(self.ticket_id),
            "source_key": self.source_key,
            "external_id": self.external_id,
            "mode": self.mode,
        }


class SupervisorAssignment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一次派活记录：工单 → Agent / Task / DSH Run。"""

    __tablename__ = "supervisor_assignments"

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("supervisor_tickets.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: dispatched / running / finished / failed
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="dispatched", server_default="dispatched"
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "ticket_id": str(self.ticket_id),
            "agent_id": str(self.agent_id) if self.agent_id is not None else None,
            "task_id": str(self.task_id) if self.task_id is not None else None,
            "run_id": self.run_id,
            "status": self.status,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
        }


class SupervisorReport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """子 Agent 交回的结构化结论。报告只由平台按事实写入，Agent 不能自报"成功"。"""

    __tablename__ = "supervisor_reports"

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("supervisor_tickets.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("supervisor_assignments.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "ticket_id": str(self.ticket_id),
            "assignment_id": (str(self.assignment_id) if self.assignment_id is not None else None),
            "status": self.status,
            "summary": self.summary,
            "payload": self.payload or {},
            "created_at": _iso(self.created_at),
        }


class SupervisorDigest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """线程收尾后的对话摘要（P4）：给项目页与后续检索用，不塞进 Agent 上下文。"""

    __tablename__ = "supervisor_digests"

    thread_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("supervisor_threads.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decisions: Mapped[list] = mapped_column(JSON, default=list)
    ticket_ids: Mapped[list] = mapped_column(JSON, default=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "thread_id": str(self.thread_id) if self.thread_id is not None else None,
            "project_id": str(self.project_id) if self.project_id is not None else None,
            "summary": self.summary,
            "decisions": list(self.decisions or []),
            "ticket_ids": list(self.ticket_ids or []),
            "created_at": _iso(self.created_at),
        }


class SupervisorOutbox(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Transactional outbox：事件落库与"接下来要做什么"同事务排队，重启后重放。"""

    __tablename__ = "supervisor_outbox"

    topic: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    #: 幂等键：同一件事只排一次队（唯一索引，重复入队静默跳过）
    dedupe_key: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    #: pending / processing / done / dead
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending", index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    available_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "topic": self.topic,
            "payload": self.payload or {},
            "status": self.status,
            "attempts": self.attempts,
            "available_at": _iso(self.available_at),
            "processed_at": _iso(self.processed_at),
            "last_error": self.last_error,
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
