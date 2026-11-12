"""用户账号连接器的安装记录（通用凭据表）。

与 Agent 执行侧的 `connectors` / `connector_logs` 无关：这里记的是"某个本地账号
安装了哪个外部连接器、用什么鉴权方式、拿到了哪些 scope"。

GitHub 的 OAuth 令牌仍落在既有的 `github_connections` 表（历史数据结构不动）；
本表只承载通用安装元数据与 api_key 类凭据。凭据用密钥库的 SecretBox 加密，
接口与日志都不出现明文。行按 `(account_key, connector_id)` 唯一，账号之间互相不可见。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ConnectorInstallation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "connector_installations"
    __table_args__ = (
        UniqueConstraint(
            "account_key",
            "connector_id",
            name="uq_connector_installations_account_connector",
        ),
    )

    account_key: Mapped[str] = mapped_column(String(64), index=True)
    connector_id: Mapped[str] = mapped_column(String(64), index=True)
    #: oauth / api_key；与 catalog 定义里声明的 auth_types 对应。
    auth_type: Mapped[str] = mapped_column(String(32))
    #: 空格分隔的授权范围，与 GitHub 连接的存储格式保持一致。
    scopes: Mapped[str] = mapped_column(String(512), default="", server_default="")
    #: API Key 等凭据的密文；oauth 连接（如 GitHub）此列为空，令牌在专用表。
    credential_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    def scope_list(self) -> list[str]:
        return [part for part in (self.scopes or "").split() if part]

    def to_public(self, *, name: str, description: str) -> dict[str, Any]:
        """对外表示：不含凭据密文，只回安装状态与授权范围。"""
        return {
            "id": self.connector_id,
            "name": name,
            "description": description,
            "connected": True,
            "account": "",
            "display_name": name,
            "avatar_url": "",
            "scopes": self.scope_list(),
            "connected_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = ["ConnectorInstallation"]
