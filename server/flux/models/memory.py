"""三层记忆的持久化模型（批次② §4.2）。

只承载 User / Environment 两层：Project Memory 的底座是既有 `project_memory` 表
（Project Brain），不在这里复制一份。`layer` 的取值见 `flux.enums.MemoryLayer`。

`key` 只有平台维护的 Environment 条目使用（同一 key 幂等更新，平台升级时刷新内容）；
User 条目的 key 恒为 NULL——同一唯一约束下多个 NULL 互相不冲突，两类写入共用一张表。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MemoryEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一条被记住的结论（User / Environment 层）。"""

    __tablename__ = "memories"
    __table_args__ = (UniqueConstraint("layer", "key", name="uq_memories_layer_key"),)

    # 取值见 flux.enums.MemoryLayer（user / environment）
    layer: Mapped[str] = mapped_column(String(16), index=True)
    # 平台维护条目的稳定标识（如 runtime.proposal_required）；User 条目为 NULL
    key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    # 来源记录：这条记忆是"谁以什么方式"写的（user / platform），便于审计与清理
    source: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # 过期时间（UTC）：NULL 表示不因时间失效；过期条目在读取时被排除、写入时被清理
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "layer": self.layer,
            "key": self.key,
            "content": self.content,
            "source": self.source,
            "metadata": self.meta or {},
            "expires_at": self.expires_at.isoformat() if self.expires_at is not None else None,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
