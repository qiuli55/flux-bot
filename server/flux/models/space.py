"""共享空间（Phase 3）：两个 Flux 云用户共享一层记忆的容器。

为什么不复用 `organizations` / `organization_members`：那两张表的外键指向本机
`users.id`，而本仓库里 `users` 表不是身份权威——人的身份只认 Flux 云用户 id
（云 JWT 的 `sub` / `FLUX_OWNER_CLOUD_USER_ID`）。把云用户 id 硬塞进本地
`users.id` 会制造第二套身份，因此新建独立的最小表。

- `spaces`：一个共享空间；创建者即 owner。
- `space_members`：成员关系，唯一键 `(space_id, cloud_user_id)`。
- shared 层记忆仍放在 `memories` 表（`layer="shared"` + `space_id`）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class Space(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一个共享空间；只有成员能看到它和它的 shared 记忆。"""

    __tablename__ = "spaces"

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 创建者的 Flux 云用户 id（创建时自动成为 owner）
    created_by_cloud_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name,
            "created_by_cloud_user_id": self.created_by_cloud_user_id,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class SpaceMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """共享空间成员；role 取值见 `flux.enums.SpaceRole`。"""

    __tablename__ = "space_members"
    __table_args__ = (
        UniqueConstraint("space_id", "cloud_user_id", name="uq_space_members_space_user"),
    )

    space_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("spaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cloud_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: owner / member；只有 owner 能增删成员。
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="member", server_default="member"
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "space_id": str(self.space_id),
            "cloud_user_id": self.cloud_user_id,
            "role": self.role,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


__all__ = ["Space", "SpaceMember"]
