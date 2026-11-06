"""virtual_changes 补 Apply 审计字段

Apply Engine（实施计划 ⑦）需要两条落库信息才能做到"失败可追溯、可回滚"：
- backup_path：原文件备份位置，回滚与人工恢复都靠它
- apply_error：失败原因全文，不能只留在日志里

Revision ID: e7c05f3d81a2
Revises: d4e91a7c52b8
Create Date: 2026-09-30 09:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7c05f3d81a2"
down_revision: str | Sequence[str] | None = "d4e91a7c52b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # batch 模式：SQLite 不支持 ALTER COLUMN，显式 batch 保证本地与 PostgreSQL 都能跑。
    with op.batch_alter_table("virtual_changes", schema=None) as batch_op:
        batch_op.add_column(sa.Column("backup_path", sa.String(length=1024), nullable=True))
        batch_op.add_column(sa.Column("apply_error", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("virtual_changes", schema=None) as batch_op:
        batch_op.drop_column("apply_error")
        batch_op.drop_column("backup_path")
