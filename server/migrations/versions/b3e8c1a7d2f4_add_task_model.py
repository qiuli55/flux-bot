"""tasks 补任务级模型覆盖

移动端对话页支持切换「当前 Agent 支持的模型」：选择结果按任务保存，
后续 Run 由所属 runtime 读取；切换 Agent 时清空（回到新 Agent 默认模型）。

Revision ID: b3e8c1a7d2f4
Revises: a9d4c2e70b16
Create Date: 2026-10-23 16:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3e8c1a7d2f4"
down_revision: str | Sequence[str] | None = "a9d4c2e70b16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("model", sa.String(length=128), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_column("model")
