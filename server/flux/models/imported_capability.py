"""已导入能力的注册表（批次③ §5）。

导入是"能力清单"的权威：同一 (kind, name) 只有一条记录；重复导入按 fingerprint
判定 unchanged / keep / replace——**不静默覆盖**。fingerprint 是同一条记录
"是否变化过"的唯一依据（canonical JSON 的 SHA-256）。

spec 里存的是清洗后的 Flux 标准对象：env / header 只可能有键名与 ${VAR} 引用，
明文值在 Scanner 阶段就被拦下，不会到这张表。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ImportedCapability(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "imported_capabilities"
    __table_args__ = (UniqueConstraint("kind", "name", name="uq_imported_capabilities_kind_name"),)

    #: 取值见 flux.enums.CapabilityKind（agent / skill / connector）
    kind: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(128))
    #: 发现来源（文件路径 / 安装命令），仅作审计线索
    source: Mapped[str] = mapped_column(String(512), default="", server_default="")
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: spec canonical JSON 的 SHA-256：内容没变则指纹不变
    fingerprint: Mapped[str] = mapped_column(String(64))
    spec: Mapped[dict] = mapped_column(JSON)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "kind": self.kind,
            "name": self.name,
            "source": self.source,
            "version": self.version,
            "fingerprint": self.fingerprint,
            "spec": dict(self.spec or {}),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
