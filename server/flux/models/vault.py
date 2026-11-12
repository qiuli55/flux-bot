"""用户保险库（站点登录 / 支付卡 / 联系人）。

与 `api_keys`（模型通道的供应商密钥）不是一回事：这里存的是用户自己的账号信息。
三条铁律：

- 密码、完整卡号、CVV 只以 `SecretBox` 信封密文（`LargeBinary`）落库，明文只在
  写入或显式 reveal 时短暂存在于内存；
- 对外投影（`to_dict`）一律不含密文，卡片永远只给 last4 / brand / 有效期；
- 写入入口只有用户侧 REST API，Agent / MCP 不提供写工具。

主密钥复用 `flux.core.key_vault.service.KeyVaultService` 的同一把
（`FLUX_KEY_VAULT_SECRET` 或 `~/.flux/key-vault.key`），不另立第二套密钥。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class VaultLogin(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一条站点登录信息；密码密文不可逆地只以信封形式存在。"""

    __tablename__ = "vault_logins"

    site: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(256), nullable=False)
    password_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    def to_dict(self) -> dict[str, Any]:
        """只回展示字段；密码连掩码都不回，仅告知"已保存"。"""
        return {
            "id": str(self.id),
            "site": self.site,
            "username": self.username,
            "notes": self.notes,
            "has_password": True,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class VaultCard(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一张支付卡；PAN / CVV 在 `secret_ciphertext` 里，展示字段单独存。"""

    __tablename__ = "vault_cards"

    label: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    brand: Mapped[str] = mapped_column(String(32), nullable=False, default="", server_default="")
    last4: Mapped[str] = mapped_column(String(4), nullable=False)
    exp_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exp_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    #: 仅表示"是否存在 PAN / CVV"，不是秘密本身；避免列表页为判断而解密。
    has_pan: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    has_cvv: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    def to_dict(self) -> dict[str, Any]:
        """掩码投影：绝不返回 PAN / CVV / 密文，只回 last4、品牌与有效期。"""
        return {
            "id": str(self.id),
            "label": self.label,
            "brand": self.brand,
            "last4": self.last4,
            "exp_month": self.exp_month,
            "exp_year": self.exp_year,
            "has_pan": self.has_pan,
            "has_cvv": self.has_cvv,
            "notes": self.notes,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class VaultContact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """联系人（PII）：姓名 / 电话 / 邮箱 / 备注；日志里不得整本地址簿倾倒。"""

    __tablename__ = "vault_contacts"

    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    phones: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    emails: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name,
            "phones": list(self.phones or []),
            "emails": list(self.emails or []),
            "notes": self.notes,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


__all__ = ["VaultCard", "VaultContact", "VaultLogin"]
