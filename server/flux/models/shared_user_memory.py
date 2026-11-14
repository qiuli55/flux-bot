"""跨 Bot 共享的用户记忆。

与 `bot_memories`（属于某个 Bot 的第一人称记忆）和 `memories`（User / Environment
三层记忆）都不是一回事：这里记的是「同一个用户，无论和哪个 Bot 说话都成立」的
账号级信息。

分两张表：

- `user_profile_entries`：用户画像。按 key 覆盖（last-write-wins），只允许用户侧
  画像接口写入；Bot 不能静默改写。
- `user_journal_entries`：共享日志。只追加，按规范化文本的 sha256 去重；一条观察
  可以被下一个 Bot 看到，但不会因为重复上报而膨胀。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class UserProfileEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """用户画像的一条键值事实（name / locale / timezone ...）。

    Phase 4 起按 Flux 云用户隔离：同一个小 key（例如 name）在两个人身上各存一份，
    但同一个人同一个 key 仍然唯一。历史行 `cloud_user_id IS NULL` 只属于
    `FLUX_OWNER_CLOUD_USER_ID`（读取时由 `owned_clause` 兼容）。
    """

    __tablename__ = "user_profile_entries"
    __table_args__ = (
        # 同一用户内 key 唯一；SQL NULL 在唯一约束里互不相等，所以历史 NULL 行
        # 另用部分唯一索引兜住（旧的全表唯一约束保证最多只剩一行）。
        UniqueConstraint("cloud_user_id", "key", name="uq_user_profile_entries_user_key"),
        Index(
            "uq_user_profile_entries_legacy_key",
            "key",
            unique=True,
            sqlite_where=text("cloud_user_id IS NULL"),
            postgresql_where=text("cloud_user_id IS NULL"),
        ),
    )

    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    #: 归属人；NULL = Phase 4 之前写入的老数据（只对主用户可见）。
    cloud_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class UserJournalEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """共享日志的一条观察；`content_hash` 唯一，重复内容返回已有行。"""

    __tablename__ = "user_journal_entries"
    __table_args__ = (
        # 同一用户内按规范化文本去重；不同用户可以有完全相同的观察。
        UniqueConstraint("cloud_user_id", "content_hash", name="uq_user_journal_entries_user_hash"),
        Index(
            "uq_user_journal_entries_legacy_hash",
            "content_hash",
            unique=True,
            sqlite_where=text("cloud_user_id IS NULL"),
            postgresql_where=text("cloud_user_id IS NULL"),
        ),
        Index("ix_user_journal_entries_created", "created_at"),
    )

    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: 规范化文本（strip + 折叠空白 + casefold）的 sha256 十六进制串
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: user / bot / system
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="user", server_default="user"
    )
    #: 观察来自哪个 Bot；Bot 被删除后保留日志本身，只把来源置空
    source_bot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bots.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    #: 归属人；NULL = Phase 4 之前写入的老数据（只对主用户可见）。
    cloud_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "content": self.content,
            "source": self.source,
            "source_bot_id": (str(self.source_bot_id) if self.source_bot_id is not None else None),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
