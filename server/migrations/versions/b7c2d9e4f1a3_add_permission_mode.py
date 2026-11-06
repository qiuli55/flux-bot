"""对话权限三模式：tasks / agent_runs 增加 permission_mode

Plan（只读 + 计划文档）/ Normal（高风险命令审批）/ Full（跳过逐步审批）三模式需要两列：
- tasks.permission_mode：对话当前模式，切换只影响后续消息；
- agent_runs.permission_mode：Run 启动时从任务快照，MCP 层据此做工具白名单与审批闸门
  （DSH 内置 Agent 用长期令牌，仅靠令牌 scopes 无法区分模式）。

Revision ID: b7c2d9e4f1a3
Revises: d4b8f1c62a97
Create Date: 2026-10-25 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c2d9e4f1a3"
down_revision: str | Sequence[str] | None = "d4b8f1c62a97"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "permission_mode", sa.String(length=16), nullable=False, server_default="normal"
            )
        )
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "permission_mode", sa.String(length=16), nullable=False, server_default="normal"
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.drop_column("permission_mode")
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_column("permission_mode")
