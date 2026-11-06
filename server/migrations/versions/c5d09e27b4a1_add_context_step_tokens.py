"""新增 context_step_tokens 反向索引（FLUX_CONTEXT_MECHANISM §7.2）

正向 `fetch(step_id)` 的前提是 agent 已经知道 id，但接管 agent 手里通常只有线索
（"我记得有人改过 auth.py，但不知道是哪一段"）。本表把文件 / 命令 / URL / 错误关键字
这类线索映射回 step_id，让回看通道真的走得进去。

Revision ID: c5d09e27b4a1
Revises: a3f1c8d47b26
Create Date: 2026-10-16 11:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5d09e27b4a1"
down_revision: str | Sequence[str] | None = "a3f1c8d47b26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 与 context_steps.id 同型（SQLite 只有 INTEGER 才自增，但本列不自增，仍保持同型）
_STEP_ID_TYPE = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "context_step_tokens",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("step_id", _STEP_ID_TYPE, nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("turn_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("token", sa.String(length=512), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_context_step_tokens_step_id", "context_step_tokens", ["step_id"])
    op.create_index("ix_context_step_tokens_run_id", "context_step_tokens", ["run_id"])
    op.create_index("ix_context_step_tokens_turn_id", "context_step_tokens", ["turn_id"])
    op.create_index("ix_context_step_tokens_kind", "context_step_tokens", ["kind"])
    op.create_index("ix_context_step_tokens_token", "context_step_tokens", ["token"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_context_step_tokens_token", table_name="context_step_tokens")
    op.drop_index("ix_context_step_tokens_kind", table_name="context_step_tokens")
    op.drop_index("ix_context_step_tokens_turn_id", table_name="context_step_tokens")
    op.drop_index("ix_context_step_tokens_run_id", table_name="context_step_tokens")
    op.drop_index("ix_context_step_tokens_step_id", table_name="context_step_tokens")
    op.drop_table("context_step_tokens")
