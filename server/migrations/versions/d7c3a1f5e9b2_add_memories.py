"""add memories table for User / Environment memory

三层记忆的 User / Environment 层存储（批次② §4.2）；Project 层继续用 project_memory。

- layer：user / environment（取值见 flux.enums.MemoryLayer）
- key：平台维护条目的稳定标识，(layer, key) 唯一；User 条目为 NULL（NULL 互不冲突）
- source：来源记录（user / platform）
- expires_at：TTL 到点即失效；NULL 表示不因时间失效

Revision ID: d7c3a1f5e9b2
Revises: c1a4e7d92f08
Create Date: 2026-10-04 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7c3a1f5e9b2"
down_revision: str | Sequence[str] | None = "c1a4e7d92f08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "memories",
        sa.Column("layer", sa.String(length=16), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
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
        sa.UniqueConstraint("layer", "key", name="uq_memories_layer_key"),
    )
    with op.batch_alter_table("memories", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_memories_layer"), ["layer"], unique=False)
        batch_op.create_index(batch_op.f("ix_memories_expires_at"), ["expires_at"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("memories", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_memories_expires_at"))
        batch_op.drop_index(batch_op.f("ix_memories_layer"))
    op.drop_table("memories")
