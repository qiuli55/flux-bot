"""内部多 bot 群聊的持久化模型。

一个群只负责「谁在一起聊、聊了什么」；成员是一个常驻 Bot（`bots.id`），
名称、头像、模型都落在 bots 上。群聊消息单独存表，不复用 task_messages：
同一句话要出现在群里、又不能让别的 bot 顺着它读到你与某个 bot 的私聊。

`seq` 是同一群内的单调序号（从 1 起），前端轮询用它做游标——
不依赖 created_at（SQLite 时间戳只到秒，同一秒多条消息无法排序）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ChatGroup(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一个内部群聊。"""

    __tablename__ = "chat_groups"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 每个 epoch（用户一次发消息引发的多轮讨论）的最大轮数；全员沉默会提前结束。
    #: 这是结构性闸门，不是预算：不限制 token / 金额 / 时长。
    max_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default="2")
    #: active / archived（归档后只读，不再响应新消息）
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name,
            "project_id": str(self.project_id) if self.project_id is not None else None,
            "description": self.description,
            "max_rounds": self.max_rounds,
            "status": self.status,
            "last_message_at": (
                self.last_message_at.isoformat() if self.last_message_at is not None else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at is not None else None,
        }


class ChatGroupMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """群成员：成员 = 一个常驻 Bot（bots.id）。"""

    __tablename__ = "chat_group_members"
    __table_args__ = (
        UniqueConstraint("group_id", "bot_id", name="uq_chat_group_members_group_bot"),
        Index("ix_chat_group_members_bot", "bot_id"),
    )

    group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("chat_groups.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bots.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(
        String(32), nullable=False, default="member", server_default="member"
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "group_id": str(self.group_id),
            "bot_id": str(self.bot_id),
            "role": self.role,
            "created_at": self.created_at.isoformat() if self.created_at is not None else None,
        }


class ChatGroupMemberState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """成员在某个群里的独立会话状态（一成员 × 一群一行）。

    `memory` 只记录该 bot 自己在本群说过/被安排的事，注入它的发言上下文；
    `last_seen_seq` 是它上次参与到的群消息序号——同一个 bot 在不同群里
    互不可见，群与群之间也不会共享这份笔记。
    """

    __tablename__ = "chat_group_member_states"
    __table_args__ = (
        UniqueConstraint("group_id", "bot_id", name="uq_chat_group_member_states_group_bot"),
        Index("ix_chat_group_member_states_bot", "bot_id"),
    )

    group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("chat_groups.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bots.id", ondelete="CASCADE"),
        nullable=False,
    )
    last_seen_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: 个人笔记（该 bot 自己的发言摘要），只对该 bot 在本群可见
    memory: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 已在本群发言的次数（观测用，不参与闸门）
    turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": str(self.group_id),
            "bot_id": str(self.bot_id),
            "last_seen_seq": self.last_seen_seq,
            "memory": self.memory,
            "turns": self.turns,
            "updated_at": self.updated_at.isoformat() if self.updated_at is not None else None,
        }


class ChatGroupMemory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """分层记忆：群共享记忆（scope=group）与团队记忆（scope=team）。

    team 记忆不挂在任何群上（`group_id` 为空），因此所有群都能看到；
    group 记忆只在本群内注入。`up_to_seq` 记录已经压缩进记忆的群消息上界，
    避免同一段对话被反复摘要。
    """

    __tablename__ = "chat_group_memories"
    __table_args__ = (Index("ix_chat_group_memories_scope_group", "scope", "group_id"),)

    group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("chat_groups.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    #: group / team
    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="group", server_default="group"
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    up_to_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": str(self.group_id) if self.group_id is not None else None,
            "scope": self.scope,
            "content": self.content,
            "up_to_seq": self.up_to_seq,
            "updated_at": self.updated_at.isoformat() if self.updated_at is not None else None,
        }


class ChatGroupEpoch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一次「用户发消息 → 多轮讨论」的执行单元。

    epoch 是打断的粒度：用户发新消息时把上一个 epoch 置 interrupted，
    编排器在每个发言者之前检查状态，看到非 running 就立刻收手。
    """

    __tablename__ = "chat_group_epochs"
    __table_args__ = (
        UniqueConstraint("group_id", "epoch_index", name="uq_chat_group_epochs_group_index"),
        Index("ix_chat_group_epochs_group_status", "group_id", "status"),
    )

    group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("chat_groups.id", ondelete="CASCADE"),
        nullable=False,
    )
    epoch_index: Mapped[int] = mapped_column(Integer, nullable=False)
    #: user / system：本轮由用户消息触发，或由平台（如定时汇报）触发
    trigger_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="user", server_default="user"
    )
    #: running / done / interrupted
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running", server_default="running"
    )
    rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default="2")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "group_id": str(self.group_id),
            "epoch_index": self.epoch_index,
            "trigger": self.trigger_kind,
            "status": self.status,
            "rounds": self.rounds,
            "max_rounds": self.max_rounds,
            "finished_at": (self.finished_at.isoformat() if self.finished_at is not None else None),
            "created_at": self.created_at.isoformat() if self.created_at is not None else None,
        }


class ChatGroupMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """群聊里的一条消息（用户 / bot / 系统）。"""

    __tablename__ = "chat_group_messages"
    __table_args__ = (Index("ix_chat_group_messages_group_seq", "group_id", "seq"),)

    group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("chat_groups.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: 群内单调序号（从 1 起），前端增量轮询的游标
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 说话者对应的 Bot；用户消息与已删除 Bot 的消息为 NULL
    bot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bots.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: 说话者名字快照：bot 被删除或改名后，历史消息仍能显示当时的名字
    author_name: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    #: user / bot / system
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="bot")
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="text", server_default="text"
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: 属于哪一个 epoch（不设外键：epoch 清理不应级联删除聊天记录）
    epoch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "group_id": str(self.group_id),
            "seq": self.seq,
            "bot_id": str(self.bot_id) if self.bot_id is not None else None,
            "author_name": self.author_name,
            "role": self.role,
            "kind": self.kind,
            "content": self.content,
            "payload": self.payload,
            "epoch_id": str(self.epoch_id) if self.epoch_id is not None else None,
            "created_at": self.created_at.isoformat() if self.created_at is not None else None,
        }
