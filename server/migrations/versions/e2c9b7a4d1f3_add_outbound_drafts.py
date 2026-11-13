"""add outbound_drafts 统一出站草稿

飞书出站草稿此前只存在进程内存里，重启即丢，其它频道也无法复用。本表把正文、
目标与状态落库：审批通过后原子认领并发送一次，失败也保留正文；pending 草稿在
进程重启后仍可查看或取消。

Revision ID: e2c9b7a4d1f3
Revises: f4b7e1c9a260
Create Date: 2026-11-13 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2c9b7a4d1f3"
down_revision: str | Sequence[str] | None = "f4b7e1c9a260"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "outbound_drafts",
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_hash", sa.String(length=64), nullable=False),
        sa.Column("dest", sa.JSON(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("turn_id", sa.Uuid(), nullable=True),
        sa.Column("decision_id", sa.String(length=64), nullable=True),
        sa.Column("error", sa.String(length=256), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_index("ix_outbound_drafts_channel", "outbound_drafts", ["channel"], unique=False)
    op.create_index("ix_outbound_drafts_status", "outbound_drafts", ["status"], unique=False)
    op.create_index("ix_outbound_drafts_bot_id", "outbound_drafts", ["bot_id"], unique=False)
    op.create_index("ix_outbound_drafts_task_id", "outbound_drafts", ["task_id"], unique=False)
    op.create_index("ix_outbound_drafts_turn_id", "outbound_drafts", ["turn_id"], unique=False)
    op.create_index(
        "ix_outbound_drafts_decision_id", "outbound_drafts", ["decision_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_outbound_drafts_decision_id", table_name="outbound_drafts")
    op.drop_index("ix_outbound_drafts_turn_id", table_name="outbound_drafts")
    op.drop_index("ix_outbound_drafts_task_id", table_name="outbound_drafts")
    op.drop_index("ix_outbound_drafts_bot_id", table_name="outbound_drafts")
    op.drop_index("ix_outbound_drafts_status", table_name="outbound_drafts")
    op.drop_index("ix_outbound_drafts_channel", table_name="outbound_drafts")
    op.drop_table("outbound_drafts")
