"""Agent Installation 持久化模型（最终方案 §3.2 / §4）。

描述"当前机器是否具备该 CLI Agent"：可执行文件、路径、版本、来源、认证状态、
探测到的能力与状态机状态。它不是 Agent 档案（agents 表，身份与权限边界），
而是本机安装事实——同一台机器可以没有任何 Agent，也不影响档案存在。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from flux.enums import AgentInstallStatus
from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AgentInstallation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_installations"

    #: Adapter / Agent 标识（如 opencode、codex），一台机器上唯一
    name: Mapped[str] = mapped_column(String(64), index=True, unique=True)
    adapter: Mapped[str] = mapped_column(String(64), default="")
    #: 取值见 flux.enums.AgentInstallStatus
    status: Mapped[str] = mapped_column(
        String(32), index=True, default=AgentInstallStatus.NOT_INSTALLED.value
    )
    executable: Mapped[str | None] = mapped_column(String(128), nullable=True)
    path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 发现来源（当前只有 path；预留给 known locations / manifest）
    source: Mapped[str] = mapped_column(String(32), default="path")
    #: ok / missing / unknown
    auth_status: Mapped[str] = mapped_column(String(32), default="unknown")
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name,
            "adapter": self.adapter,
            "status": self.status,
            "executable": self.executable,
            "path": self.path,
            "version": self.version,
            "source": self.source,
            "auth_status": self.auth_status,
            "capabilities": list(self.capabilities or []),
            "detail": dict(self.detail or {}),
        }
