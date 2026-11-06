"""add imported capabilities registry

能力导入注册表（批次③ §5）：同一 (kind, name) 唯一，fingerprint 判定是否变化。

- kind：agent / skill / connector（取值见 flux.enums.CapabilityKind）
- fingerprint：spec canonical JSON 的 SHA-256（canonical = 键排序 + 紧凑分隔符）
- spec：清洗后的 Flux 标准对象（不含任何明文凭证）

Revision ID: b7e2f4a9c1d3
Revises: d7c3a1f5e9b2
Create Date: 2026-10-04 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e2f4a9c1d3"
down_revision: str | Sequence[str] | None = "d7c3a1f5e9b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "imported_capabilities",
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("version", sa.String(length=64), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
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
        sa.UniqueConstraint("kind", "name", name="uq_imported_capabilities_kind_name"),
    )
    with op.batch_alter_table("imported_capabilities", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_imported_capabilities_kind"), ["kind"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("imported_capabilities", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_imported_capabilities_kind"))
    op.drop_table("imported_capabilities")
