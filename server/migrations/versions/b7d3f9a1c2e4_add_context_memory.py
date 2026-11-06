"""跨轮记忆：工作记忆卡 + 任务外部会话绑定

执行 Agent 此前每轮都是新会话、只拿到当前消息与最初任务描述，长对话里会
重做最初的需求。本迁移补两张表：

- `task_memory_cards`：每轮结束由平台生成一张短卡（本轮目标 / 结果 / 已改文件 /
  失败原因），下一轮开工注入执行指令；按 (task_id, run_id) 幂等。
- `task_runtime_sessions`：任务在 opencode / codex 上的外部会话 id，下一轮按
  task 续聊，不再每轮新建后销毁；(task_id, runtime_id) 唯一。

Revision ID: b7d3f9a1c2e4
Revises: c1e7a4b93d05
Create Date: 2026-10-26 23:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d3f9a1c2e4"
down_revision: str | Sequence[str] | None = "c1e7a4b93d05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "task_memory_cards",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_task_memory_cards_task_id"), "task_memory_cards", ["task_id"], unique=False
    )
    op.create_index(
        op.f("ix_task_memory_cards_run_id"), "task_memory_cards", ["run_id"], unique=False
    )

    op.create_table(
        "task_runtime_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("runtime_id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=200), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "runtime_id", name="uq_task_runtime_sessions_task_runtime"),
    )
    op.create_index(
        op.f("ix_task_runtime_sessions_task_id"),
        "task_runtime_sessions",
        ["task_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_task_runtime_sessions_runtime_id"),
        "task_runtime_sessions",
        ["runtime_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_task_runtime_sessions_runtime_id"), table_name="task_runtime_sessions")
    op.drop_index(op.f("ix_task_runtime_sessions_task_id"), table_name="task_runtime_sessions")
    op.drop_table("task_runtime_sessions")
    op.drop_index(op.f("ix_task_memory_cards_run_id"), table_name="task_memory_cards")
    op.drop_index(op.f("ix_task_memory_cards_task_id"), table_name="task_memory_cards")
    op.drop_table("task_memory_cards")
