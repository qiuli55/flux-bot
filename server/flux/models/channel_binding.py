"""IM 频道 ↔ Flux 云账号绑定（Phase 3）。

两个概念分开存：

- `channel_user_bindings`：长期绑定，`(channel, external_user_id)` 唯一。入站消息
  先查它，命中就按 `cloud_user_id` 的人格处理（记忆 / 密钥 / 连接器 / 例程 / 出站）。
- `channel_bind_codes`：一次性短码，只存 sha256；用户在已登录的 Flux Bot 里生成，
  到 IM 里发一次即可完成绑定。TTL 短、单次有效，明文不落库。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class ChannelUserBinding(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一个 IM 外部用户对应一个 Flux 云用户。"""

    __tablename__ = "channel_user_bindings"
    __table_args__ = (
        UniqueConstraint("channel", "external_user_id", name="uq_channel_bindings_channel_user"),
    )

    #: feishu / wecom / dingtalk
    channel: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    #: Feishu open_id/user_id/union_id、WeCom FromUserName、DingTalk senderStaffId
    external_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Flux 云用户 id（云 JWT 的 sub）
    cloud_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "channel": self.channel,
            "external_user_id": self.external_user_id,
            "cloud_user_id": self.cloud_user_id,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class ChannelBindCode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一次性绑定码；只存 sha256，兑换成功即写 `used_at`。"""

    __tablename__ = "channel_bind_codes"

    #: sha256(大写后的明文码) 的十六进制串
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    cloud_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: 失败尝试次数（过期 / 已用后仍被输入时累加），便于审计。
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


__all__ = ["ChannelBindCode", "ChannelUserBinding"]
