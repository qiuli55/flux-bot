"""任务与外部 Agent 会话的绑定（跨轮会话复用的持久化锚点）。

Claude Code / opencode / codex 的会话都按 task 复用：下一轮用同一个 external session id
续聊，不再每轮新建后销毁。这张表就是「任务 → 外部会话 id」的权威存储。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TaskRuntimeSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一个任务在一个 Agent + runtime 上的外部会话 id。

    会话必须按 Agent 隔离：同一 runtime（如 opencode）下的两个 Agent 各有自己的
    系统提示词、工具与模型，共用一个外部会话会把上一个 Agent 的上下文带给下一个。
    旧数据没有 agent_id（NULL）：只有「任务未指定 Agent」的轮次才会读取并续用。
    """

    __tablename__ = "task_runtime_sessions"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "agent_id",
            "runtime_id",
            name="uq_task_runtime_sessions_task_agent_runtime",
        ),
    )

    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    #: 会话归属的 Agent；NULL = 加这一列之前的旧绑定（按 task+runtime 续用）
    agent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    runtime_id: Mapped[str] = mapped_column(String(32), index=True)
    session_id: Mapped[str] = mapped_column(String(200), default="")
    #: 上一轮该会话的真实上下文规模（token）。仅用于跨 Run 判断要不要压缩 / 重开；
    #: 0 = 未知（旧数据或该 runtime 不报送），消费方按「未知」处理而不是「空会话」。
    context_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": str(self.task_id),
            "agent_id": str(self.agent_id) if self.agent_id is not None else None,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "context_tokens": self.context_tokens,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
