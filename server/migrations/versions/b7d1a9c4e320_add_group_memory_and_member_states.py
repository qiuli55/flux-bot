"""群聊记忆分层：成员独立会话状态 + 群/团队记忆

- chat_group_member_states：一成员 × 一群一行，记录该 bot 在本群的个人笔记
  （`memory`）与已参与到的消息序号（`last_seen_seq`），实现群内记忆隔离；
- chat_group_memories：`scope=group` 为群共享记忆，`scope=team` 为跨群团队记忆
  （`group_id` 为空）。`up_to_seq` 是已经压缩进记忆的消息上界，避免重复摘要。

Revision ID: b7d1a9c4e320
Revises: f6b2c9d4a81e
Create Date: 2026-11-04 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d1a9c4e320"
down_revision: str | Sequence[str] | None = "f6b2c9d4a81e"
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
        "chat_group_member_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("last_seen_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("memory", sa.Text(), nullable=True),
        sa.Column("turns", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
        sa.ForeignKeyConstraint(["group_id"], ["chat_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "task_id", name="uq_chat_group_member_states_group_task"),
    )
    op.create_index(
        op.f("ix_chat_group_member_states_group_id"), "chat_group_member_states", ["group_id"]
    )
    op.create_index("ix_chat_group_member_states_task", "chat_group_member_states", ["task_id"])

    op.create_table(
        "chat_group_memories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="group"),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("up_to_seq", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
        sa.ForeignKeyConstraint(["group_id"], ["chat_groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_chat_group_memories_group_id"), "chat_group_memories", ["group_id"])
    op.create_index(
        "ix_chat_group_memories_scope_group", "chat_group_memories", ["scope", "group_id"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_chat_group_memories_scope_group", table_name="chat_group_memories")
    op.drop_index(op.f("ix_chat_group_memories_group_id"), table_name="chat_group_memories")
    op.drop_table("chat_group_memories")
    op.drop_index("ix_chat_group_member_states_task", table_name="chat_group_member_states")
    op.drop_index(
        op.f("ix_chat_group_member_states_group_id"), table_name="chat_group_member_states"
    )
    op.drop_table("chat_group_member_states")
