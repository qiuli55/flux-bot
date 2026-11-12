"""Bot 对话轮次（BotConversation API 的持久化权威）。

一轮 = 用户发来一条消息到助手给出回复（或失败/中断）。HTTP 请求只负责把这一轮
落成 `queued` 就返回；真正的模型调用由每 Bot 串行的后台队列执行。`bot_turns`
因此既是幂等键的归属，也是长轮询/重启恢复的状态源。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class BotTurn(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "bot_turns"
    __table_args__ = (
        # 同一 Bot 的 client_turn_id 是幂等键；SQLite/PostgreSQL 都允许多个 NULL。
        UniqueConstraint("bot_id", "client_turn_id", name="uq_bot_turns_bot_client"),
    )

    bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bots.id", ondelete="CASCADE"), index=True, nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: 用户消息在 turn 开始处理时才落库（保证同一 Bot 的消息严格按轮次顺序）。
    user_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("task_messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    reply_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("task_messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: queued / running / completed / failed / cancelled
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued", index=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 机器可读的失败原因：budget_exhausted / interrupted / assistant_error ...
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 客户端幂等键（同一 Bot 内唯一）；重试同一条消息返回原来的 turn。
    client_turn_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "bot_id": str(self.bot_id),
            "task_id": str(self.task_id),
            "user_message_id": (
                str(self.user_message_id) if self.user_message_id is not None else None
            ),
            "reply_message_id": (
                str(self.reply_message_id) if self.reply_message_id is not None else None
            ),
            "status": self.status,
            "error": self.error,
            "error_code": self.error_code,
            "client_turn_id": self.client_turn_id,
            "created_at": (self.created_at.isoformat() if self.created_at is not None else None),
            "updated_at": (self.updated_at.isoformat() if self.updated_at is not None else None),
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at is not None else None
            ),
        }
