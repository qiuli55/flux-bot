"""常驻 Bot（Bot 模式）。

与「对话」的区别不是身份，而是生命周期：对话是一轮一轮问、终态即结束；
Bot 是常驻在机器上的一个执行者，有自己的工作目录、自己的 Agent 与权限模式，
可以按 `schedule`（5 段 cron）周期性开工，也可以在需要时手动触发一次。

每次开工都是一条 `tasks` 记录（`tasks.bot_id` 指回本表），所以执行、上下文、
计费、消息都复用既有链路，Bot 只负责「什么时候、在哪个目录、让谁去跑」。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class Bot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "bots"

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 这个 Bot 的职责/固定作业内容。定时触发时它就是本轮的执行指令。
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    #: 头像与对话一致：只由「形状 × 颜色」确定性渲染（flux.core.group.avatar）
    avatar_shape: Mapped[str] = mapped_column(
        String(32), nullable=False, default="circle", server_default="circle"
    )
    avatar_color: Mapped[str] = mapped_column(
        String(32), nullable=False, default="violet", server_default="violet"
    )
    #: 固定由哪个 Agent 执行；留空 = 平台默认 runtime
    agent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    #: 默认需要人工审核高风险工具调用（PermissionMode.NORMAL）；
    #: 无人值守场景由用户显式改成 unattended / full。
    permission_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="normal", server_default="normal"
    )
    decision_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="auto", server_default="auto"
    )
    #: 独立工作目录（绝对路径）。留空 = `<dsh_home>/bots/<id>/workspace`。
    #: 每次开工都会把解析后的路径快照进 tasks.workspace，历史记录不随配置漂移。
    workspace: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: 取值见 flux.enums.BotStatus；paused 的 Bot 不会被定时触发
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active", index=True
    )
    #: 5 段 cron（分 时 日 月 周）。留空 = 只手动触发。
    schedule: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 下一次自动开工时间；调度器按它取到期任务，落库保证重启不丢。
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: 最近一次开工的任务；不设外键，任务被清理后 Bot 仍可查
    last_task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    #: 与这个 Bot 的对话任务（懒创建，一轮对话共用一条 tasks 行）。
    #: 它与 last_task_id 无关：后者是最近一次定时/手动开工，聊天不会推进调度记录。
    chat_task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: 最近一次已结算结果的任务。调度器每轮都会看到 last_task_id，
    #: 用它保证同一条终态任务只计入健康统计一次。
    last_settled_task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    #: 连续失败多少次自动暂停；0 = 永不自动暂停（用户完全自己管）
    failure_threshold: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: 累计已结算的轮次与其中失败的轮次（只增不减，用于健康度展示）
    runs_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    runs_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: 最近一次失败原因（截断后落库）；成功一轮后清空
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 自动暂停的原因；用户手动恢复时清空
    paused_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: 每日 token 预算（按 FLUX_TIMEZONE 的自然日）。NULL 或 0 = 不限制；
    #: 已用量不落库，直接从 usage_records 按当日聚合（账本是唯一权威，天然幂等）。
    token_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 失败时是否主动通知用户（写 bot_notifications）
    notify_on_failure: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    #: 可触发本 Bot 的事件名列表（如 task.completed）；NULL/空 = 不响应事件
    event_triggers: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    #: 允许调用的 MCP 工具名；NULL = 不额外限制（仍受权限模式约束）
    allowed_tools: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    #: 允许执行的命令首 token 白名单；NULL = 不限制
    allowed_commands: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    #: 允许访问的路径前缀（相对工作目录）；NULL = 不限制
    allowed_paths: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name,
            "description": self.description or "",
            "avatar_shape": self.avatar_shape or "circle",
            "avatar_color": self.avatar_color or "violet",
            "agent_id": str(self.agent_id) if self.agent_id is not None else None,
            "model": self.model,
            "permission_mode": self.permission_mode or "normal",
            "decision_mode": self.decision_mode or "auto",
            "workspace": self.workspace,
            "status": self.status or "active",
            "schedule": self.schedule,
            "next_run_at": _iso(self.next_run_at),
            "last_run_at": _iso(self.last_run_at),
            "last_task_id": str(self.last_task_id) if self.last_task_id is not None else None,
            "chat_task_id": (str(self.chat_task_id) if self.chat_task_id is not None else None),
            "last_settled_task_id": (
                str(self.last_settled_task_id) if self.last_settled_task_id is not None else None
            ),
            "failure_threshold": int(self.failure_threshold or 0),
            "consecutive_failures": int(self.consecutive_failures or 0),
            "runs_total": int(self.runs_total or 0),
            "runs_failed": int(self.runs_failed or 0),
            "last_error": self.last_error,
            "paused_reason": self.paused_reason,
            "token_budget": int(self.token_budget) if self.token_budget else None,
            "notify_on_failure": bool(self.notify_on_failure),
            "event_triggers": list(self.event_triggers or []),
            "allowed_tools": list(self.allowed_tools) if self.allowed_tools is not None else None,
            "allowed_commands": (
                list(self.allowed_commands) if self.allowed_commands is not None else None
            ),
            "allowed_paths": (list(self.allowed_paths) if self.allowed_paths is not None else None),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
