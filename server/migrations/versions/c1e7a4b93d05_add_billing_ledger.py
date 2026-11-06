"""计费账本：usage_records 扩展 + usage_snapshots 新表

Flux 此前只在 `agent_runs.last_event` 里留一份 run 级 run_metrics 快照，
`usage_records` 从建表起从未写入，无法回答"这个套餐这一周用了多少"。

本迁移补齐两件事：
- `usage_records` 变成可聚合的明细账本：新增 `plan_key`（套餐归因）、
  `run_id`（按 run 回放）、`cache_read/write_tokens`（缓存单独计价）、
  `occurred_at`（窗口切片依据，不依赖 created_at）。
- 新增 `usage_snapshots`：官方额度接口（OpenCode Go /usage、火山 arkcli）只说
  总量、没有任务归因，这张表把总量钉在时间轴上做对账。

Revision ID: c1e7a4b93d05
Revises: f6b2d4a9c8e1
Create Date: 2026-10-26 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1e7a4b93d05"
down_revision: str | Sequence[str] | None = "f6b2d4a9c8e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.add_column(sa.Column("plan_key", sa.String(length=32), nullable=True))
        batch_op.add_column(
            sa.Column("cache_read_tokens", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("run_id", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column(
                "occurred_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
            )
        )
        batch_op.create_index(batch_op.f("ix_usage_records_plan_key"), ["plan_key"], unique=False)
        batch_op.create_index(batch_op.f("ix_usage_records_run_id"), ["run_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_usage_records_occurred_at"), ["occurred_at"], unique=False
        )

    op.create_table(
        "usage_snapshots",
        sa.Column("plan_key", sa.String(length=32), nullable=False),
        sa.Column("window_key", sa.String(length=24), nullable=False),
        sa.Column("used", sa.Float(), nullable=True),
        sa.Column("quota", sa.Float(), nullable=True),
        sa.Column("percent", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(length=16), nullable=False, server_default="percent"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="ok"),
        sa.Column("resets_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
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
    with op.batch_alter_table("usage_snapshots", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_usage_snapshots_plan_key"), ["plan_key"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_usage_snapshots_window_key"), ["window_key"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("usage_snapshots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_usage_snapshots_window_key"))
        batch_op.drop_index(batch_op.f("ix_usage_snapshots_plan_key"))

    op.drop_table("usage_snapshots")

    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_usage_records_occurred_at"))
        batch_op.drop_index(batch_op.f("ix_usage_records_run_id"))
        batch_op.drop_index(batch_op.f("ix_usage_records_plan_key"))
        batch_op.drop_column("occurred_at")
        batch_op.drop_column("run_id")
        batch_op.drop_column("cache_write_tokens")
        batch_op.drop_column("cache_read_tokens")
        batch_op.drop_column("plan_key")
