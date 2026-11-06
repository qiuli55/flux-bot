"""记录终端命令进程组 id，供 Flux 重启后回收孤儿命令进程。

Revision ID: a9d4c2e70b16
Revises: b4d7e1a9c3f5
Create Date: 2026-10-16 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9d4c2e70b16"
down_revision: str | Sequence[str] | None = "b4d7e1a9c3f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the running command's process-group id (nullable)."""
    op.add_column(
        "terminal_sessions",
        sa.Column("cmd_pgid", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Drop the command process-group id column."""
    op.drop_column("terminal_sessions", "cmd_pgid")
