"""drop demo_sites table

公网发布能力已抽成独立工具（demo-publisher），Flux 不再记录发布结果，也不再持有
SSH 部署配置。本迁移删掉 `demo_sites` 表；`e5c8a1d37b62` 保留在版本链里，因为已有
实例（A 机）已经应用过它。

Revision ID: f6b2d4a9c8e1
Revises: e5c8a1d37b62
Create Date: 2026-10-26 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6b2d4a9c8e1"
down_revision: str | Sequence[str] | None = "e5c8a1d37b62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_table("demo_sites")


def downgrade() -> None:
    """Downgrade schema."""
    op.create_table(
        "demo_sites",
        sa.Column("design_id", sa.String(length=64), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("url", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("design_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("html_sha256", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("published_by", sa.String(length=128), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("demo_sites", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_demo_sites_design_id"), ["design_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_demo_sites_slug"), ["slug"], unique=True)
