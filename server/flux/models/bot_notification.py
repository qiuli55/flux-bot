"""Bot 的主动通知（失败告知）。

Bot 是无人值守的：一轮失败如果只在卡片上显示错误，用户很可能错过。
失败且 `bot.notify_on_failure` 为真时，调度器落一条通知，前端顶栏据此提醒；
用户读掉后 `read_at` 落时间，不再重复提醒。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class BotNotification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "bot_notifications"

    #: Bot 被删除后通知保留（用户仍能回看历史），只断开关联
    bot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bots.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    #: Bot 名快照：Bot 改名/删除后通知仍能说明是谁
    bot_name: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    #: info / warning / error
    level: Mapped[str] = mapped_column(
        String(16), nullable=False, default="error", server_default="error"
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="", server_default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "bot_id": str(self.bot_id) if self.bot_id is not None else None,
            "bot_name": self.bot_name,
            "task_id": str(self.task_id) if self.task_id is not None else None,
            "level": self.level,
            "title": self.title,
            "body": self.body,
            "read_at": _iso(self.read_at),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
