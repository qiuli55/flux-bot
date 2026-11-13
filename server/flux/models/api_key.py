"""密钥库（api_keys 表）。

存的是一条可用于 Agent 模型通道的供应商凭据：

- `provider`：deepseek / openai / openai_compatible / anthropic；
- `base_url`：空 = 用该供应商在 Flux 配置里的默认地址；
- `key_ciphertext`：`SecretBox` 信封密文（主密钥来自 FLUX_KEY_VAULT_SECRET 或
  `~/.flux/key-vault.key`）；**明文不落库、不出接口**；
- `key_hint`：末 4 位，仅供界面辨识；
- `models`：这条 key 可用的模型 id 列表，至少一项。

Phase 2 起密钥库按人隔离：`cloud_user_id` 记录归属。`name` 不再是全局唯一
（两个人可以都叫「DeepSeek 官方」），唯一性由服务层在「同一归属」内校验；
历史 NULL 行归 `FLUX_OWNER_CLOUD_USER_ID` 配置的主用户。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Boolean, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ApiKey(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "api_keys"

    #: 同一用户内唯一（服务层校验）；不同用户可重名
    name: Mapped[str] = mapped_column(String(128), index=True)
    #: 归属的 Flux 云用户 id；历史 NULL 归 FLUX_OWNER_CLOUD_USER_ID
    cloud_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    base_url: Mapped[str] = mapped_column(String(256), default="", server_default="")
    #: codex 的 wire_api；空 = 按 provider 推导
    wire_api: Mapped[str] = mapped_column(String(16), default="", server_default="")
    models: Mapped[list[Any]] = mapped_column(JSON, default=list)
    key_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    key_hint: Mapped[str] = mapped_column(String(16), default="", server_default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", nullable=False)

    def to_dict(self) -> dict[str, Any]:
        """对外表示**不含**密文：密钥库列表只回元数据与掩码。"""
        return {
            "id": str(self.id),
            "name": self.name,
            "cloud_user_id": self.cloud_user_id,
            "provider": self.provider,
            "base_url": self.base_url,
            "wire_api": self.wire_api,
            "models": list(self.models or []),
            "key_hint": self.key_hint,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = ["ApiKey"]
