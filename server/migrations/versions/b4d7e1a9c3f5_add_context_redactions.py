"""新增 context_redactions 脱敏映射表（FLUX_CONTEXT_MECHANISM §10.1）

入站即脱敏：正文先打码再存档，被换下来的原文另存本表，且**单独加密**
（它存的正是"从正文里换下来的秘密"，跟着打码后的正文一起裸奔就等于没打码）。

本表随会话级联删除（§8.2 / §13-5）：删除会话后必须归零，否则残留的还是秘密。

Revision ID: b4d7e1a9c3f5
Revises: e2a7c4f19b83
Create Date: 2026-10-16 12:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4d7e1a9c3f5"
down_revision: str | Sequence[str] | None = "e2a7c4f19b83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "context_redactions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("run_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("turn_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("placeholder", sa.String(length=64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_context_redactions_run_id", "context_redactions", ["run_id"])
    op.create_index("ix_context_redactions_turn_id", "context_redactions", ["turn_id"])
    op.create_index("ix_context_redactions_kind", "context_redactions", ["kind"])
    op.create_index("ix_context_redactions_placeholder", "context_redactions", ["placeholder"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_context_redactions_placeholder", table_name="context_redactions")
    op.drop_index("ix_context_redactions_kind", table_name="context_redactions")
    op.drop_index("ix_context_redactions_turn_id", table_name="context_redactions")
    op.drop_index("ix_context_redactions_run_id", table_name="context_redactions")
    op.drop_table("context_redactions")
