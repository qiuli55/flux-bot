"""Bot 模式：常驻 Bot 表 + 任务归属与工作目录快照

- bots：常驻执行者本身（Agent / 模型 / 权限模式 / 独立工作目录 / cron 定时）。
  `next_run_at` 落库，调度器重启后按它继续，不丢定时；
- tasks.bot_id：这条任务由哪个 Bot 开工（为空 = 普通对话），首页对话列表据此过滤；
- tasks.workspace：本轮工作目录快照，CLI runtime 据此决定子进程 cwd。

Revision ID: c6b3f8a1d027
Revises: b7d1a9c4e320
Create Date: 2026-11-05 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c6b3f8a1d027"
down_revision: str | Sequence[str] | None = "b7d1a9c4e320"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "bots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("avatar_shape", sa.String(length=32), nullable=False, server_default="circle"),
        sa.Column("avatar_color", sa.String(length=32), nullable=False, server_default="violet"),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("api_key_id", sa.Uuid(), nullable=True),
        sa.Column(
            "permission_mode", sa.String(length=16), nullable=False, server_default="unattended"
        ),
        sa.Column("decision_mode", sa.String(length=16), nullable=False, server_default="auto"),
        sa.Column("workspace", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("schedule", sa.String(length=64), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_task_id", sa.Uuid(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_bots_status"), "bots", ["status"])
    op.create_index(op.f("ix_bots_next_run_at"), "bots", ["next_run_at"])

    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("bot_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("workspace", sa.String(length=512), nullable=True))
        batch.create_foreign_key(
            "fk_tasks_bot_id_bots", "bots", ["bot_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_index(op.f("ix_tasks_bot_id"), ["bot_id"])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("tasks") as batch:
        batch.drop_index(op.f("ix_tasks_bot_id"))
        batch.drop_constraint("fk_tasks_bot_id_bots", type_="foreignkey")
        batch.drop_column("workspace")
        batch.drop_column("bot_id")

    op.drop_index(op.f("ix_bots_next_run_at"), table_name="bots")
    op.drop_index(op.f("ix_bots_status"), table_name="bots")
    op.drop_table("bots")
