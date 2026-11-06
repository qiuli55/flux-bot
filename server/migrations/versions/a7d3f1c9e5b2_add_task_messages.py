"""add task_messages for Solo conversation

任务执行中心（Solo）的对话消息落库：
- seq：同一任务内的单调序号（1 起），向上懒加载更早消息的游标；不依赖 created_at
  （SQLite 时间戳只到秒，同一秒内多条消息无法排序）
- role：user / assistant；kind 当前只有 text（agent 事件类消息待真实事件源接入后扩展）
- payload：结构化附加数据（澄清结论 / 执行计划步骤 / 模型调用元信息）
- task_id 级联删除：任务删除时它的对话一并清理，不留孤儿消息

Revision ID: a7d3f1c9e5b2
Revises: f2a7c1d9b6e4
Create Date: 2026-10-01 22:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7d3f1c9e5b2"
down_revision: str | Sequence[str] | None = "f2a7c1d9b6e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "task_messages",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="text"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
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
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("task_messages", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_task_messages_task_id"), ["task_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_task_messages_seq"), ["seq"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("task_messages", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_task_messages_seq"))
        batch_op.drop_index(batch_op.f("ix_task_messages_task_id"))
    op.drop_table("task_messages")
