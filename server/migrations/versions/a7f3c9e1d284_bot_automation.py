"""Bot 自动化增强：失败通知 / 事件触发 / 执行白名单 / 例程

- `bots` 新增 5 列：
  - notify_on_failure：失败时是否主动写通知；
  - event_triggers：可触发本 Bot 的事件名列表；
  - allowed_tools / allowed_commands / allowed_paths：Bot 级执行白名单，
    NULL 表示不额外限制。
- `bot_routines`：一个 Bot 可拆成多个例程，各自有指令与 cron。
- `bot_notifications`：Bot 主动通知，失败时落一条，用户读后写 read_at。

Revision ID: a7f3c9e1d284
Revises: d9e4a7c2b613
Create Date: 2026-11-05 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7f3c9e1d284"
down_revision: str | Sequence[str] | None = "d9e4a7c2b613"
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
    with op.batch_alter_table("bots") as batch:
        batch.add_column(
            sa.Column("notify_on_failure", sa.Boolean(), nullable=False, server_default="1")
        )
        batch.add_column(sa.Column("event_triggers", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("allowed_tools", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("allowed_commands", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("allowed_paths", sa.JSON(), nullable=True))

    op.create_table(
        "bot_routines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False, server_default=""),
        sa.Column("schedule", sa.String(length=64), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_bot_routines_bot_id"), "bot_routines", ["bot_id"])
    op.create_index(op.f("ix_bot_routines_next_run_at"), "bot_routines", ["next_run_at"])

    op.create_table(
        "bot_notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=True),
        sa.Column("bot_name", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("level", sa.String(length=16), nullable=False, server_default="error"),
        sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_bot_notifications_bot_id"), "bot_notifications", ["bot_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_bot_notifications_bot_id"), table_name="bot_notifications")
    op.drop_table("bot_notifications")
    op.drop_index(op.f("ix_bot_routines_next_run_at"), table_name="bot_routines")
    op.drop_index(op.f("ix_bot_routines_bot_id"), table_name="bot_routines")
    op.drop_table("bot_routines")
    with op.batch_alter_table("bots") as batch:
        batch.drop_column("allowed_paths")
        batch.drop_column("allowed_commands")
        batch.drop_column("allowed_tools")
        batch.drop_column("event_triggers")
        batch.drop_column("notify_on_failure")
