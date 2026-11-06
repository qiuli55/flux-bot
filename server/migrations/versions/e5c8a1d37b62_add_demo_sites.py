"""add demo_sites table for public demo publishing

把设计稿发布到 demo.qiuli55.top（B 机静态站点）后，Flux 侧只留一条记录：
- design_id / slug：一个设计稿一个子路径，重复发布覆盖同一 URL；两者都受
  ^[a-z0-9][a-z0-9-]{0,63}$ 约束，直接用于远端目录名，不需要再做转义；
- design_version / html_sha256 / size_bytes：判断线上是不是最新、以及内容未变时跳过上传；
- published_by：发布者来源（owner:local / owner:token / cloud:<user id>），便于审计。

Revision ID: e5c8a1d37b62
Revises: b7c2d9e4f1a3
Create Date: 2026-10-26 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5c8a1d37b62"
down_revision: str | Sequence[str] | None = "b7c2d9e4f1a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
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


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("demo_sites", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_demo_sites_slug"))
        batch_op.drop_index(batch_op.f("ix_demo_sites_design_id"))
    op.drop_table("demo_sites")
