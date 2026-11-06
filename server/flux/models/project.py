"""项目与 Project Brain（主规格 §11.2 / §5.6）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Project(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(128), index=True)
    repository: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    # 属性名 meta 对应列名 metadata（metadata 是 SQLAlchemy 声明式基类的保留属性名）
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name,
            "repository": self.repository,
            "metadata": self.meta or {},
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class ProjectMemory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Project Brain 记忆条目。

    第一版是结构化记忆（type/content/metadata），不上向量库；embedding 在 M0 以 JSON
    存储（便于无 PostgreSQL 环境跑通测试），生产环境按主规格 §11.1 使用 pgvector 向量列，
    切换点在 M5 Project Brain。type 的取值见 `flux.enums.BrainSection`。
    """

    __tablename__ = "project_memory"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    # 取值见 flux.enums.BrainSection（overview / tech_stack / ... / agent_notes）
    type: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "section": self.type,
            "content": self.content,
            "metadata": self.meta or {},
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
