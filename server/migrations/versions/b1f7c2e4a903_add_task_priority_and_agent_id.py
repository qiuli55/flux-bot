"""add task priority and agent_id

为 tasks 表补 priority / agent_id 两列（主规格附录 A 裁决 A20 / A21）：
API 已暴露这两个字段，不落库会导致进程重启后取值不一致。

Revision ID: b1f7c2e4a903
Revises: 64ccaac06660
Create Date: 2026-09-30 05:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1f7c2e4a903"
down_revision: str | Sequence[str] | None = "64ccaac06660"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # batch 模式：SQLite 不支持 ALTER COLUMN，显式 batch 保证本地与 PostgreSQL 都能跑。
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("priority", sa.Integer(), nullable=False, server_default="100")
        )
        batch_op.add_column(sa.Column("agent_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_column("agent_id")
        batch_op.drop_column("priority")
