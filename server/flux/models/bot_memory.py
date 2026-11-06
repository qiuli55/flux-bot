"""Bot 的分层长期记忆。

记忆属于 Bot（第一人称），不是一个群的公共属性：同一个 Bot 换一个群、
或者回到自己的独立工作目录开工，都还能记得「与自己有关」的事。

分两层存：
- `bot_memories`：摘要层。同一个 Bot 在同一个群里的同类内容合并成一条，
  每次合并 `version + 1`；注入上下文时只读这一层。
- `bot_memory_chunks`：全文层。每条原始发言/消息只追加、不改写，
  需要追溯原话时从这里取。

相关性三档（`relevance`）：
- direct：自己说的话、别人 @ 我、指派给我的事；
- indirect：群里的规则/结论、别人提到我的事；
- 无关内容不落库。

`source_group_id` 记住这段记忆来自哪个群：同群时按正常权重注入，
出群后只按关键词低权重召回（见 `flux.core.memory.bot_memory_service`）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class BotMemory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """摘要层：一个 Bot 的一条长期记忆。"""

    __tablename__ = "bot_memories"
    __table_args__ = (
        Index("ix_bot_memories_bot_relevance", "bot_id", "relevance"),
        Index("ix_bot_memories_bot_group", "bot_id", "source_group_id"),
    )

    bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bots.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    #: preference / fact / decision / commitment / summary
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="summary", server_default="summary"
    )
    #: direct / indirect
    relevance: Mapped[str] = mapped_column(
        String(16), nullable=False, default="direct", server_default="direct", index=True
    )
    #: 合并键：同一个 Bot 在同一个群里的同类记忆共用一个 topic
    topic: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: 这条记忆里是否包含 Bot 自己说过的话
    self_authored: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    #: group / chat / manual
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="group", server_default="group"
    )
    source_group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("chat_groups.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    #: 群名快照：群被删除后仍能说明这段记忆的出处
    source_group_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: 注入权重：direct 与同群 indirect 为 1.0，出群 indirect 为低权重
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0, server_default="1.0")
    #: 每合并一次 +1，用于观测摘要被更新过几次
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "bot_id": str(self.bot_id),
            "content": self.content,
            "kind": self.kind,
            "relevance": self.relevance,
            "topic": self.topic,
            "self_authored": bool(self.self_authored),
            "source": self.source,
            "source_group_id": (
                str(self.source_group_id) if self.source_group_id is not None else None
            ),
            "source_group_name": self.source_group_name,
            "weight": self.weight,
            "version": self.version,
            "chunk_count": self.chunk_count,
            "last_used_at": _iso(self.last_used_at),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class BotMemoryChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """全文层：一条原始发言/消息，只追加不覆盖。"""

    __tablename__ = "bot_memory_chunks"
    __table_args__ = (
        Index("ix_bot_memory_chunks_memory", "memory_id"),
        Index("ix_bot_memory_chunks_bot", "bot_id"),
    )

    memory_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bot_memories.id", ondelete="CASCADE"),
        nullable=False,
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bots.id", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="group", server_default="group"
    )
    source_group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("chat_groups.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: 来源群消息；同一轮重复写入时按它去重（不设外键：消息清理不应带走记忆）
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "memory_id": str(self.memory_id),
            "bot_id": str(self.bot_id),
            "source": self.source,
            "source_group_id": (
                str(self.source_group_id) if self.source_group_id is not None else None
            ),
            "source_message_id": (
                str(self.source_message_id) if self.source_message_id is not None else None
            ),
            "content": self.content,
            "created_at": _iso(self.created_at),
        }
