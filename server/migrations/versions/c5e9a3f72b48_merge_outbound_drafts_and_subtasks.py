"""合并统一出站草稿与 Bot 子任务两条迁移线

Revision ID: c5e9a3f72b48
Revises: e2c9b7a4d1f3, b7d2e4f9c1a3
Create Date: 2026-11-13 10:12:00.000000

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "c5e9a3f72b48"
down_revision: str | Sequence[str] | None = ("e2c9b7a4d1f3", "b7d2e4f9c1a3")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
