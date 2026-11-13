"""外部连接器（GitHub）的账号连接表。

一个本地账号最多绑定一个 GitHub 身份；`account_key` 唯一，令牌用密钥库的
`SecretBox` 信封加密后落库，接口与日志都不出现明文。

为什么按 `account_key` 隔离：Phase 2 起账号标识就是登录人（云 JWT 的 `sub`，或
`FLUX_OWNER_CLOUD_USER_ID` 主用户），不再信任客户端自报的 `X-Flux-Account`。
历史行的 `account_key='default'` 只对配置的主用户可见（查找时等价映射，见
`flux.core.github.service`）；表结构不需要改。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class GithubConnection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "github_connections"

    account_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    login: Mapped[str] = mapped_column(String(128), default="", server_default="")
    name: Mapped[str] = mapped_column(String(256), default="", server_default="")
    avatar_url: Mapped[str] = mapped_column(String(512), default="", server_default="")
    token_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    scopes: Mapped[str] = mapped_column(String(512), default="", server_default="")
    token_kind: Mapped[str] = mapped_column(String(32), default="oauth", server_default="oauth")

    def to_public(self) -> dict[str, Any]:
        """对外表示：不含令牌与密文，只回身份与授权范围。"""
        return {
            "id": "github",
            "name": "GitHub",
            "connected": True,
            "account": self.login,
            "display_name": self.name or self.login,
            "avatar_url": self.avatar_url,
            "scopes": [part for part in (self.scopes or "").split() if part],
            "connected_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = ["GithubConnection"]
