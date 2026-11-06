"""Agent Run 持久化模型（修复方案 §2.2 / §2.7）。

DSH Run 过去只存进程内字典（Phase 1），Flux 重启后状态全丢，卡死的 Run 没人知道、
残留的子进程也没人清理。这张表是 Run 生命周期的权威存储：状态、进程树（pid/pgid）、
心跳时间戳与超时判据全部落库，Reconciler 与重启恢复都以它为准。

`owner_pid` 记录创建该 Run 的 Flux 进程：多个后端实例共享同一个库时，
对账/恢复只处理"自己的行 + 属主进程已死的行"，绝不误伤其它实例正在跑的 Run。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin


class AgentRun(TimestampMixin, Base):
    """一次 Agent Run（当前只有 DSH；表名不叫 dsh_runs，后续其它运行时共用同一生命周期表）。"""

    __tablename__ = "agent_runs"

    #: Run 标识（DSH 的 run_id，uuid4().hex）
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128), default="")
    #: 关联的任务（可空：`/dsh/runs` 也能独立起 Run）
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), index=True, nullable=True)
    #: canonical Agent 标识（agents.id 的字符串形态）；归属看令牌，不看请求体
    agent_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    instruction: Mapped[str] = mapped_column(Text, default="")
    #: 取值见 flux.enums.DshRunStatus
    status: Mapped[str] = mapped_column(String(32), index=True)
    #: 启动时从任务快照的权限模式（flux.enums.PermissionMode）。MCP 层按它判定
    #: 工具白名单与审批闸门——DSH 内置 Agent 用长期令牌，仅靠令牌 scopes 无法区分模式。
    permission_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="normal", server_default="normal"
    )
    #: Agent 运行时子进程（进程组组长，见 dsh_client 的 setsid shim）
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pgid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 创建该 Run 的 Flux 进程 pid（多实例共享库时用于区分归属 + 判断属主是否已死）
    owner_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    #: 命中的超时类型：startup / idle / hard（非超时终态为 NULL）
    timeout_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: 心跳判据：状态变化 / 有输出 / 有 MCP 活动 / 心跳写入
    last_state_change_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_output_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_mcp_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: 最近一次收到的 Agent 事件（供 UI 显示"Agent 在做什么"，不存全量事件流）
    last_event: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.id,
            "session_id": self.session_id,
            "task_id": str(self.task_id) if self.task_id else None,
            "agent_id": self.agent_id,
            "instruction": self.instruction,
            "status": self.status,
            "permission_mode": self.permission_mode or "normal",
            "pid": self.pid,
            "pgid": self.pgid,
            "cancel_requested": bool(self.cancel_requested),
            "timeout_kind": self.timeout_kind,
            "error": self.error,
            "finish_reason": self.finish_reason,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "last_state_change_at": (
                self.last_state_change_at.isoformat() if self.last_state_change_at else None
            ),
            "last_heartbeat_at": (
                self.last_heartbeat_at.isoformat() if self.last_heartbeat_at else None
            ),
            "last_output_at": self.last_output_at.isoformat() if self.last_output_at else None,
            "last_mcp_activity_at": (
                self.last_mcp_activity_at.isoformat() if self.last_mcp_activity_at else None
            ),
            "last_event": self.last_event,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
