"""合并技能包库与主干（统一出站草稿 + Bot 子任务）两条迁移线

Revision ID: f6a2c9e4d817
Revises: a7c1e9d4b2f6, c5e9a3f72b48
Create Date: 2026-11-13 11:24:00.000000

PR #9 合入 main 后产生两个 Alembic 头：分支侧的技能包库
（a7c1e9d4b2f6），以及主干侧合并统一出站草稿与 Bot 子任务的迁移头
（c5e9a3f72b48）。本迁移只负责把两个头收拢为一个，不修改任何 schema。

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "f6a2c9e4d817"
down_revision: str | Sequence[str] | None = ("a7c1e9d4b2f6", "c5e9a3f72b48")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
