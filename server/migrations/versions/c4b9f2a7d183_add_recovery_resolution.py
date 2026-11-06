"""virtual_changes 新增 recovery_resolution 列（崩溃恢复挂起项的人工决策结果）

P0-1 §2.2（2026-10-05 定案）：恢复时若发现文件内容既非原文也非提案内容（或文件被外部
删除），不再自动处理，改为把提案留 failed 并挂起；用户二选一——cover（用备份覆盖，还原为
改动前原文）或 keep（保持现状，提案作废）。本列记录决策结果，为空即"待决策"。

Revision ID: c4b9f2a7d183
Revises: a1c5e7b93f24
Create Date: 2026-10-05 11:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4b9f2a7d183"
down_revision: str | Sequence[str] | None = "a1c5e7b93f24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "virtual_changes",
        sa.Column("recovery_resolution", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("virtual_changes", "recovery_resolution")
