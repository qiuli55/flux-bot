"""跨 Bot 共享的用户记忆

- `user_profile_entries`：账号级画像键值，key 唯一，last-write-wins。
- `user_journal_entries`：共享日志，只追加；`content_hash` 唯一，用于去重。

原 `memories`（User / Environment）与 `bot_memories`（每 Bot 记忆）保持不变。

Revision ID: c7e2a9b4d6f1
Revises: b3e5c7a9d1f2
Create Date: 2026-11-12 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7e2a9b4d6f1"
down_revision: str | Sequence[str] | None = "b3e5c7a9d1f2"
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
        "user_profile_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_user_profile_entries_key"),
    )

    op.create_table(
        "user_journal_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="user"),
        sa.Column("source_bot_id", sa.Uuid(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["source_bot_id"], ["bots.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_hash", name="uq_user_journal_entries_hash"),
    )
    op.create_index(
        op.f("ix_user_journal_entries_source_bot_id"),
        "user_journal_entries",
        ["source_bot_id"],
    )
    op.create_index(
        "ix_user_journal_entries_created",
        "user_journal_entries",
        ["created_at"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_user_journal_entries_created", table_name="user_journal_entries")
    op.drop_index(
        op.f("ix_user_journal_entries_source_bot_id"),
        table_name="user_journal_entries",
    )
    op.drop_table("user_journal_entries")
    op.drop_table("user_profile_entries")
