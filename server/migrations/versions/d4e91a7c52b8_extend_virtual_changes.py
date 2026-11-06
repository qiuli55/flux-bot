"""extend virtual_changes for change proposals

把 virtual_changes 从"纯展示对象"升级为闭环的权威存储（实施计划 §5 Proposal 数据模型）：
- project_id 改为可空：本地临时目录也能跑完整闭环，提案不必绑定已注册项目
- 新增 task_id / original_hash / reason / summary / added_lines / removed_lines / hunks
  original_hash 是 Apply 前复验的凭据（否则会出现 AI 覆盖用户刚写的代码）

Revision ID: d4e91a7c52b8
Revises: b1f7c2e4a903
Create Date: 2026-09-30 08:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e91a7c52b8"
down_revision: str | Sequence[str] | None = "b1f7c2e4a903"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # batch 模式：SQLite 不支持 ALTER COLUMN，显式 batch 保证本地与 PostgreSQL 都能跑。
    with op.batch_alter_table("virtual_changes", schema=None) as batch_op:
        batch_op.alter_column("project_id", existing_type=sa.Uuid(), nullable=True)
        batch_op.add_column(sa.Column("task_id", sa.Uuid(), nullable=True))
        batch_op.add_column(
            sa.Column("original_hash", sa.String(length=64), nullable=False, server_default="")
        )
        batch_op.add_column(sa.Column("reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("summary", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("added_lines", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("removed_lines", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("hunks", sa.Integer(), nullable=False, server_default="0"))
        batch_op.create_index("ix_virtual_changes_task_id", ["task_id"])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("virtual_changes", schema=None) as batch_op:
        batch_op.drop_index("ix_virtual_changes_task_id")
        batch_op.drop_column("hunks")
        batch_op.drop_column("removed_lines")
        batch_op.drop_column("added_lines")
        batch_op.drop_column("summary")
        batch_op.drop_column("reason")
        batch_op.drop_column("original_hash")
        batch_op.drop_column("task_id")
        batch_op.alter_column("project_id", existing_type=sa.Uuid(), nullable=False)
