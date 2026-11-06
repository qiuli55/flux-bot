"""virtual_changes 新增 kind 列并放开 proposed_content 可空（P1-1 Delete Proposal）

补齐 CREATE / MODIFY / DELETE 三种文件变更语义：
- 新增 kind（create/modify/delete）：回填规则为 `original_hash IS NULL → create`，否则 modify；
- delete 类没有"改动后内容"，proposed_content 允许为 NULL。

Revision ID: f3a1b2c4d5e6
Revises: d5e8b1c3a7f2
Create Date: 2026-10-05 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a1b2c4d5e6"
down_revision: str | Sequence[str] | None = "d5e8b1c3a7f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # batch 模式：SQLite 不支持 ALTER COLUMN，显式 batch 保证本地与 PostgreSQL 都能跑。
    with op.batch_alter_table("virtual_changes", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("kind", sa.String(length=16), nullable=False, server_default="modify")
        )
        batch_op.alter_column("proposed_content", existing_type=sa.Text(), nullable=True)
    # 回填：历史 create 类提案（无 original_hash）标为 create，其余保持默认的 modify
    op.execute("UPDATE virtual_changes SET kind = 'create' WHERE original_hash IS NULL")


def downgrade() -> None:
    """Downgrade schema."""
    # 回退成 NOT NULL 之前，先把 delete 类留下的 NULL 填成空串（否则约束无法加回）
    op.execute("UPDATE virtual_changes SET proposed_content = '' WHERE proposed_content IS NULL")
    with op.batch_alter_table("virtual_changes", schema=None) as batch_op:
        batch_op.alter_column("proposed_content", existing_type=sa.Text(), nullable=False)
        batch_op.drop_column("kind")
