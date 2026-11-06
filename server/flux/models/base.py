"""SQLAlchemy 声明式基类与公共 Mixin。

主规格 §11.5：主键一律 UUID，每张表必须有 created_at 与 updated_at。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    # 客户端默认值带微秒，server_default 仍保留给直接写 SQL 的场景。
    # 只靠 func.now()（SQLite 下是秒级 CURRENT_TIMESTAMP）会让同一秒内创建的行
    # 无法按时间排序，例如"停止后立刻重试"时挑不出最新的 assignment。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
        nullable=False,
    )
