"""tasks 补 Solo 生命周期字段

文档 §4 P0-06 / §5 需要四列才能把「需求确认 → 开始执行 → 遇决策点暂停 → 用户拍板续跑」落库：
- decision_mode：任务级决策策略（auto = AI 默认方案 / manual = 由我决定）
- confirmation：需求确认六维度（目标/功能范围/技术方案/修改范围/风险/需人工审核的环节），用户可改
- decisions：决策点历史列表；status=pending 的那条是"正等用户选择"
- run_id：该任务触发的 DSH Run，Run 终态靠它回写到任务状态

Revision ID: f8b1c4d20a37
Revises: c3a9e5b17d42
Create Date: 2026-10-02 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8b1c4d20a37"
down_revision: str | Sequence[str] | None = "c3a9e5b17d42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # batch 模式：SQLite 不支持 ALTER COLUMN，显式 batch 保证本地与 PostgreSQL 都能跑。
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("decision_mode", sa.String(length=16), nullable=False, server_default="auto")
        )
        batch_op.add_column(sa.Column("confirmation", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("decisions", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("run_id", sa.String(length=64), nullable=True))
        batch_op.create_index("ix_tasks_run_id", ["run_id"])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_index("ix_tasks_run_id")
        batch_op.drop_column("run_id")
        batch_op.drop_column("decisions")
        batch_op.drop_column("confirmation")
        batch_op.drop_column("decision_mode")
