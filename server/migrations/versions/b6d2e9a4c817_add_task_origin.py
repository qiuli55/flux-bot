"""tasks 增加对话来源 origin

助理页的助理对话需要与首页普通对话分开：两者都是真实任务（能跑 Agent、选模型），
但助理对话不进首页对话列表。`origin` 取值 chat / assistant（flux.enums.TaskOrigin），
旧数据由 server_default 补成 chat。

Revision ID: b6d2e9a4c817
Revises: a3f7d1c8b504
Create Date: 2026-11-03 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6d2e9a4c817"
down_revision: str | Sequence[str] | None = "a3f7d1c8b504"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # batch 模式：SQLite 不支持 ALTER COLUMN，显式 batch 保证本地与 PostgreSQL 都能跑。
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("origin", sa.String(length=16), nullable=False, server_default="chat")
        )
        batch_op.create_index("ix_tasks_origin", ["origin"])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_index("ix_tasks_origin")
        batch_op.drop_column("origin")
