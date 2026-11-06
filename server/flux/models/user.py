"""用户与组织（主规格 §11.2；organizations / organization_members 为裁决 A7 补全）。"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    provider_accounts: Mapped[list[ProviderAccount]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class ProviderAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """用户在某供应商下的账号绑定。

    credential_reference 指向 Vault 中的条目引用，**不是密钥本身**（主规格 §14.3）。
    """

    __tablename__ = "provider_accounts"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_provider_account"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32))
    credential_reference: Mapped[str] = mapped_column(String(255))

    user: Mapped[User] = relationship(back_populates="provider_accounts")


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)


class OrganizationMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_organization_member"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # 取值见 flux.enums.Role
    role: Mapped[str] = mapped_column(String(32), default="developer")
