"""合并制品页面与主干（Claude Code 执行内核 + 技能包库 + 统一出站草稿 + Bot 子任务）两条迁移线

Revision ID: fd3aaa44b896
Revises: 430c28a2bdb7, b3d7e1a9c5f2
Create Date: 2026-11-13 12:30:00.000000

PR #11（feat/pages）合入 main 后产生两个 Alembic 头：分支侧的制品页面
（b3d7e1a9c5f2），以及主干侧合并用户保险库与既有迁移线的头
（430c28a2bdb7）。本迁移只负责把两个头收拢为一个，不修改任何 schema。

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "fd3aaa44b896"
down_revision: str | Sequence[str] | None = ("430c28a2bdb7", "b3d7e1a9c5f2")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
