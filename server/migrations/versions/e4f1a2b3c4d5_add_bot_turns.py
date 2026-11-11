"""Bot 异步对话轮次：bots.chat_task_id + bot_turns

- `bots.chat_task_id`：每个 Bot 唯一一条对话任务（普通 tasks 行，bot_id 指向本表）。
  懒创建，任务被清理或进入终态后下一轮再建；与 last_task_id（最近一次开工）无关。
- `bot_turns`：一轮用户消息 → 助手回复的持久化状态。HTTP 只落 queued 就返回，
  后台按 Bot 串行执行；client_turn_id 是同一 Bot 内的幂等键。

Revision ID: e4f1a2b3c4d5
Revises: c8e4a1f72b93
Create Date: 2026-11-12 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4f1a2b3c4d5"
down_revision: str | Sequence[str] | None = "c8e4a1f72b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "bot_turns",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("user_message_id", sa.Uuid(), nullable=True),
        sa.Column("reply_message_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("client_turn_id", sa.String(length=128), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_message_id"], ["task_messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reply_message_id"], ["task_messages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bot_id", "client_turn_id", name="uq_bot_turns_bot_client"),
    )
    op.create_index(op.f("ix_bot_turns_bot_id"), "bot_turns", ["bot_id"])
    op.create_index(op.f("ix_bot_turns_task_id"), "bot_turns", ["task_id"])
    op.create_index(op.f("ix_bot_turns_status"), "bot_turns", ["status"])

    with op.batch_alter_table("bots") as batch:
        batch.add_column(sa.Column("chat_task_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_bots_chat_task_id_tasks",
            "tasks",
            ["chat_task_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("bots") as batch:
        batch.drop_constraint("fk_bots_chat_task_id_tasks", type_="foreignkey")
        batch.drop_column("chat_task_id")

    op.drop_index(op.f("ix_bot_turns_status"), table_name="bot_turns")
    op.drop_index(op.f("ix_bot_turns_task_id"), table_name="bot_turns")
    op.drop_index(op.f("ix_bot_turns_bot_id"), table_name="bot_turns")
    op.drop_table("bot_turns")
