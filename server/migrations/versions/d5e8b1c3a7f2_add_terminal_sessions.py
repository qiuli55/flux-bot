"""新增终端会话与终端事件表（Agent Terminal Console §6 / §8）

Terminal Session 是"观察 + 控制"的载体：Flux 自己执行命令，因此命令与输出都拿得到；
事件按会话内单调递增的 seq 落库，前端按 seq 续读即可恢复历史、SSE 断线重连也只带 seq。

Revision ID: d5e8b1c3a7f2
Revises: c4b9f2a7d183
Create Date: 2026-10-05 13:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e8b1c3a7f2"
down_revision: str | Sequence[str] | None = "c4b9f2a7d183"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "terminal_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("workspace_root", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("next_seq", sa.Integer(), server_default="1", nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_index("ix_terminal_sessions_run_id", "terminal_sessions", ["run_id"], unique=False)
    op.create_index("ix_terminal_sessions_status", "terminal_sessions", ["status"], unique=False)

    op.create_table(
        "terminal_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("chunk", sa.Text(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
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
        sa.ForeignKeyConstraint(["session_id"], ["terminal_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_terminal_events_session_id", "terminal_events", ["session_id"], unique=False
    )
    op.create_index("ix_terminal_events_seq", "terminal_events", ["seq"], unique=False)
    op.create_index("ix_terminal_events_kind", "terminal_events", ["kind"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_terminal_events_kind", table_name="terminal_events")
    op.drop_index("ix_terminal_events_seq", table_name="terminal_events")
    op.drop_index("ix_terminal_events_session_id", table_name="terminal_events")
    op.drop_table("terminal_events")
    op.drop_index("ix_terminal_sessions_status", table_name="terminal_sessions")
    op.drop_index("ix_terminal_sessions_run_id", table_name="terminal_sessions")
    op.drop_table("terminal_sessions")
