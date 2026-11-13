"""合并用户保险库与主干（技能包库 + 统一出站草稿 + Bot 子任务）两条迁移线

Revision ID: 430c28a2bdb7
Revises: d7a1f4c9b2e6, f6a2c9e4d817
Create Date: 2026-11-13 12:00:00.000000

PR #10 合入 main 后产生两个 Alembic 头：分支侧的用户保险库
（d7a1f4c9b2e6），以及主干侧合并技能包库、统一出站草稿与 Bot 子任务的
迁移头（f6a2c9e4d817）。本迁移只负责把两个头收拢为一个，不修改任何 schema。

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "430c28a2bdb7"
down_revision: str | Sequence[str] | None = ("d7a1f4c9b2e6", "f6a2c9e4d817")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
