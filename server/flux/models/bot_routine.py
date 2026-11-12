"""Bot 的例程（Routine）。

一个 Bot 可以拆成多个例程：每个例程有自己的指令与定时，互不覆盖。
Bot 自身的 `schedule` 仍然是「主例程」——不建 routine 时行为与以前完全一致。

调度器每轮扫到期的例程开工，开工记录仍然落到 `tasks`（`tasks.bot_id` 指回 Bot），
`bot_id` 相同的例程共用同一个工作目录与权限配置，同一时刻只会有一个例程在跑。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class BotRoutine(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "bot_routines"

    bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bots.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 这个例程本轮的执行指令
    instruction: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    #: 5 段 cron；留空 = 只随 Bot 主调度或手动触发
    schedule: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    #: 可触发本例程的事件名（webhook.* / github.* 或 Events 常量）；NULL/空 = 只按 cron 跑
    event_triggers: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "bot_id": str(self.bot_id),
            "name": self.name,
            "instruction": self.instruction or "",
            "schedule": self.schedule,
            "enabled": bool(self.enabled),
            "event_triggers": list(self.event_triggers or []),
            "next_run_at": _iso(self.next_run_at),
            "last_run_at": _iso(self.last_run_at),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
