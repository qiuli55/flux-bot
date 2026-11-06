"""add run_trace_items 执行轨迹持久化

对话页的思考过程与命令执行此前只在 SSE 增量里存在，Run 结束或刷新后即消失。
本表按 Run 落库轨迹项，任务详情页据此还原「任务耗时 / 概览 / 思考过程 / 命令输出」。

Revision ID: d4b8f1c62a97
Revises: c8f2a6d91e3b
Create Date: 2026-10-25 16:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4b8f1c62a97"
down_revision: str | Sequence[str] | None = "c8f2a6d91e3b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "run_trace_items",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("output", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("call_id", sa.String(length=160), nullable=False),
        sa.Column("paths", sa.JSON(), nullable=True),
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
    with op.batch_alter_table("run_trace_items", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_run_trace_items_kind"), ["kind"], unique=False)
        batch_op.create_index(batch_op.f("ix_run_trace_items_run_id"), ["run_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_run_trace_items_task_id"), ["task_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("run_trace_items", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_run_trace_items_task_id"))
        batch_op.drop_index(batch_op.f("ix_run_trace_items_run_id"))
        batch_op.drop_index(batch_op.f("ix_run_trace_items_kind"))
    op.drop_table("run_trace_items")
