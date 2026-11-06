"""区分 Agent Terminal 与 Human Terminal 会话（终端运行时隔离）。

Revision ID: f2a7c4d9e1b3
Revises: f3a1b2c4d5e6
Create Date: 2026-10-06 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a7c4d9e1b3"
down_revision: str | Sequence[str] | None = "f3a1b2c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add a non-null session kind; existing terminal sessions are Agent Terminal sessions."""
    op.add_column(
        "terminal_sessions",
        sa.Column("kind", sa.String(length=16), server_default="agent", nullable=False),
    )


def downgrade() -> None:
    """Remove the session kind discriminator."""
    op.drop_column("terminal_sessions", "kind")
