"""无感切换 Agent：任务排队字段 + 会话按 Agent 隔离

- `tasks.pending_agent_id`：执行中切换 Agent 时只记目标，本轮 Run 收尾后再应用。
- `task_runtime_sessions.agent_id`：外部会话从「按 task+runtime」改为
  「按 task+agent+runtime」唯一，避免同一 runtime 下两个 Agent 串用会话。
  旧行 agent_id 为 NULL，只给「任务未指定 Agent」的轮次续用。

Revision ID: c9e2a5f7b3d1
Revises: b7d3f9a1c2e4
Create Date: 2026-10-27 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9e2a5f7b3d1"
down_revision: str | Sequence[str] | None = "b7d3f9a1c2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("tasks", sa.Column("pending_agent_id", sa.Uuid(), nullable=True))

    # SQLite 不支持直接改约束，batch 模式会重建表；索引与已有数据由反射保留
    with op.batch_alter_table("task_runtime_sessions", recreate="always") as batch:
        batch.add_column(sa.Column("agent_id", sa.Uuid(), nullable=True))
        batch.drop_constraint("uq_task_runtime_sessions_task_runtime", type_="unique")
        batch.create_unique_constraint(
            "uq_task_runtime_sessions_task_agent_runtime",
            ["task_id", "agent_id", "runtime_id"],
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("task_runtime_sessions", recreate="always") as batch:
        batch.drop_constraint("uq_task_runtime_sessions_task_agent_runtime", type_="unique")
        batch.create_unique_constraint(
            "uq_task_runtime_sessions_task_runtime", ["task_id", "runtime_id"]
        )
        batch.drop_column("agent_id")
    op.drop_column("tasks", "pending_agent_id")
