"""微信接入：wechat_sessions 会话映射表

一个微信私聊 ↔ 一条 Flux 助理对话（tasks.origin = assistant）。消息仍落
`task_messages`，因此微信对话会自动进入知识库索引；本表只保存「哪个微信会话
对应哪个任务」以及幂等用的 last_message_id。

`(source_key, external_id)` 唯一；任务删除时会话映射级联删除，下次微信消息
自动新开一条对话。

Revision ID: d4a8e1c73b29
Revises: c1f7a2b9e4d6
Create Date: 2026-11-03 17:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4a8e1c73b29"
down_revision: str | Sequence[str] | None = "c1f7a2b9e4d6"
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
        "wechat_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_key", sa.String(length=32), nullable=False, server_default="wechat"),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("sender_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("last_message_id", sa.String(length=191), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata", sa.JSON(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_key", "external_id", name="uq_wechat_sessions_source_external"),
    )
    op.create_index(op.f("ix_wechat_sessions_task_id"), "wechat_sessions", ["task_id"])
    op.create_index("ix_wechat_sessions_sender", "wechat_sessions", ["sender_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_wechat_sessions_sender", table_name="wechat_sessions")
    op.drop_index(op.f("ix_wechat_sessions_task_id"), table_name="wechat_sessions")
    op.drop_table("wechat_sessions")
