"""Email 入站触发：email_trigger_cursors

每个 (account_key, mailbox) 一行，记录已处理的最大 IMAP UID 与最近一次轮询状态。
进程内去重集合重启即丢，游标必须落库才能真正避免重复发布 email.received。

Revision ID: b7c2e4f9a1d3
Revises: f4b7e1c9a260
Create Date: 2026-11-13 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c2e4f9a1d3"
down_revision: str | Sequence[str] | None = "f4b7e1c9a260"
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
        "email_trigger_cursors",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_key", sa.String(length=64), nullable=False),
        sa.Column("mailbox", sa.String(length=255), nullable=False),
        sa.Column("last_uid", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_message_id", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("last_error", sa.String(length=255), nullable=False, server_default=""),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_key",
            "mailbox",
            name="uq_email_trigger_cursors_account_mailbox",
        ),
    )
    op.create_index(
        op.f("ix_email_trigger_cursors_account_key"),
        "email_trigger_cursors",
        ["account_key"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_email_trigger_cursors_account_key"),
        table_name="email_trigger_cursors",
    )
    op.drop_table("email_trigger_cursors")
