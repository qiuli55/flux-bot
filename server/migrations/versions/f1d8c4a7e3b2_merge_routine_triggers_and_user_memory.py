"""合并例程事件触发与共享用户记忆两条迁移线

Revision ID: f1d8c4a7e3b2
Revises: e6a4c2d81f37, c7e2a9b4d6f1
Create Date: 2026-11-12 10:48:00.000000

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "f1d8c4a7e3b2"
down_revision: str | Sequence[str] | None = ("e6a4c2d81f37", "c7e2a9b4d6f1")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
