"""合并 Email 入站触发与主干（Claude Code 内核 + 制品页面 + 技能包库等）两条迁移线

Revision ID: 9f2c4e6a8b1d
Revises: b7c2e4f9a1d3, fd3aaa44b896
Create Date: 2026-11-13 15:00:00.000000

PR #18 合入 main 后产生两个 Alembic 头：分支侧的 Email 入站轮询游标
（b7c2e4f9a1d3），以及主干侧收拢制品页面与既有迁移线的头（fd3aaa44b896）。
本迁移只负责把两个头收拢为一个，不修改任何 schema。

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "9f2c4e6a8b1d"
down_revision: str | Sequence[str] | None = ("b7c2e4f9a1d3", "fd3aaa44b896")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
