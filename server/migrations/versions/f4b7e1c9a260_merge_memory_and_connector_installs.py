"""合并共享用户记忆与连接器安装两条迁移线

Revision ID: f4b7e1c9a260
Revises: f1d8c4a7e3b2, a1c4e7f92b30
Create Date: 2026-11-12 11:03:00.000000

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "f4b7e1c9a260"
down_revision: str | Sequence[str] | None = ("f1d8c4a7e3b2", "a1c4e7f92b30")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
