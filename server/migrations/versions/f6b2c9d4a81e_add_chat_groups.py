"""内部多 bot 群聊：bot 档案字段 + 群聊四表

一个对话就是一只 bot：`tasks` 增加 `bot_name` / `avatar_shape` / `avatar_color`，
头像只由「形状 + 颜色」确定性渲染，不存图片。

群聊单独建表（表名避开 SQL 保留字 `group`）：
- chat_groups：群本身与轮数闸门 `max_rounds`；
- chat_group_members：成员 = 一条 bot 对话；
- chat_group_epochs：用户一次发言引发的多轮讨论，也是打断的粒度；
- chat_group_messages：群消息，`seq` 为群内单调序号（前端轮询游标）。
消息与 epoch 故意不设外键：清理 epoch 不应级联删掉聊天记录。

Revision ID: f6b2c9d4a81e
Revises: d4a8e1c73b29
Create Date: 2026-11-04 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6b2c9d4a81e"
down_revision: str | Sequence[str] | None = "d4a8e1c73b29"
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
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("bot_name", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column("avatar_shape", sa.String(length=32), nullable=False, server_default="circle")
        )
        batch.add_column(
            sa.Column("avatar_color", sa.String(length=32), nullable=False, server_default="violet")
        )

    op.create_table(
        "chat_groups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("max_rounds", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_chat_groups_project_id"), "chat_groups", ["project_id"])

    op.create_table(
        "chat_group_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="member"),
        *_timestamps(),
        sa.ForeignKeyConstraint(["group_id"], ["chat_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "task_id", name="uq_chat_group_members_group_task"),
    )
    op.create_index(op.f("ix_chat_group_members_group_id"), "chat_group_members", ["group_id"])
    op.create_index("ix_chat_group_members_task", "chat_group_members", ["task_id"])

    op.create_table(
        "chat_group_epochs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("epoch_index", sa.Integer(), nullable=False),
        sa.Column("trigger_kind", sa.String(length=16), nullable=False, server_default="user"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("rounds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_rounds", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["group_id"], ["chat_groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "epoch_index", name="uq_chat_group_epochs_group_index"),
    )
    op.create_index(
        "ix_chat_group_epochs_group_status", "chat_group_epochs", ["group_id", "status"]
    )

    op.create_table(
        "chat_group_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("author_name", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="text"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("epoch_id", sa.Uuid(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["group_id"], ["chat_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_group_messages_group_seq", "chat_group_messages", ["group_id", "seq"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_chat_group_messages_group_seq", table_name="chat_group_messages")
    op.drop_table("chat_group_messages")
    op.drop_index("ix_chat_group_epochs_group_status", table_name="chat_group_epochs")
    op.drop_table("chat_group_epochs")
    op.drop_index("ix_chat_group_members_task", table_name="chat_group_members")
    op.drop_index(op.f("ix_chat_group_members_group_id"), table_name="chat_group_members")
    op.drop_table("chat_group_members")
    op.drop_index(op.f("ix_chat_groups_project_id"), table_name="chat_groups")
    op.drop_table("chat_groups")

    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("avatar_color")
        batch.drop_column("avatar_shape")
        batch.drop_column("bot_name")
