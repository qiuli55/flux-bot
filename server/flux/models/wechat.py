"""微信接入的会话映射（OpenClaw / 微信 ClawBot 只做传输层）。

一个微信会话对应一条 Flux 助理对话（`tasks.origin = assistant`）：消息落成
`task_messages` 后会自动进入知识库索引，因此微信里聊过的内容也能被 RAG 检索到。

`(source_key, external_id)` 唯一：external_id 用 OpenClaw 的 sessionKey（同一微信
私聊稳定不变），sender_id 只在白名单校验与审计时使用。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class WechatSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """微信会话 → Flux 任务 的持久映射。"""

    __tablename__ = "wechat_sessions"
    __table_args__ = (
        UniqueConstraint("source_key", "external_id", name="uq_wechat_sessions_source_external"),
        Index("ix_wechat_sessions_sender", "sender_id"),
    )

    # 来源标识（默认 wechat），与 Supervisor 订阅 / 出站通道的键一致
    source_key: Mapped[str] = mapped_column(
        String(32), nullable=False, default="wechat", server_default="wechat"
    )
    # OpenClaw sessionKey：同一微信私聊稳定不变
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # 微信 senderId（wxid_xxx）：白名单校验 + 审计
    sender_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # 幂等：OpenClaw 重试同一条消息时用它识别，避免重复调用模型
    last_message_id: Mapped[str | None] = mapped_column(String(191), nullable=True)
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "source_key": self.source_key,
            "external_id": self.external_id,
            "sender_id": self.sender_id,
            "task_id": str(self.task_id),
            "last_message_id": self.last_message_id,
            "message_count": self.message_count,
            "metadata": self.meta or {},
        }
