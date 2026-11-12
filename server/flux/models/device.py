"""手机 / 桌面设备与配对码。

设备令牌是给「某台手机」用的长期凭据：明文形如 `fbd_<32 字节 urlsafe>`，
库里只存 sha256，接口只回 8 位前缀。配对码是一次性的短期凭据，同样只存哈希；
用户在后端生成、在手机上输入，兑换成功后立刻作废。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class Device(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "devices"

    name: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    #: sha256(明文令牌) 十六进制；明文只在配对响应里出现一次。
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    #: 明文令牌前 8 个字符，仅供列表展示（不足以还原令牌）。
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: 非空即已撤销；鉴权缓存会在撤销时立即失效。
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: 设备令牌过期时间；NULL = 永不过期（FLUX_DEVICE_TOKEN_TTL_DAYS=0）。
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    def to_dict(self) -> dict[str, Any]:
        """对外表示**不含** token_hash。"""
        return {
            "id": str(self.id),
            "name": self.name,
            "token_prefix": self.token_prefix,
            "created_at": _iso(self.created_at),
            "last_seen_at": _iso(self.last_seen_at),
            "revoked_at": _iso(self.revoked_at),
            "revoked": self.revoked,
            "expires_at": _iso(self.expires_at),
        }


class PairingCode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "pairing_codes"

    #: sha256(8 位配对码) 十六进制；不存明文。
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: 命中该码后失败的兑换次数（审计用；真正的限流按来源 IP 在内存里做）。
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
