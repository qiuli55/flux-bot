"""外部连接器（GitHub）的账号连接表。

一个本地账号最多绑定一个 GitHub 身份；`account_key` 唯一，令牌用密钥库的
`SecretBox` 信封加密后落库，接口与日志都不出现明文。

为什么按 `account_key` 隔离：App 当前的「账号」是本地账号（还没有服务端登录体系），
所以账号标识由客户端以 `X-Flux-Account` 声明、服务端只做格式校验。将来接入真实登录后，
把这里换成令牌里的 subject 即可，表结构不用动。
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
