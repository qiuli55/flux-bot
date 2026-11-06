"""Bot 运行健康与失败熔断

无人值守的 Bot 连续失败时不应该无限重试：
- `consecutive_failures` 记录连续失败轮次，达到 `failure_threshold` 自动暂停；
- `last_settled_task_id` 保证同一条终态任务只结算一次（调度器每轮都会看到 last_task_id）；
- `runs_total` / `runs_failed` / `last_error` / `paused_reason` 用于展示健康度与失败原因。

Revision ID: f2a9c7d1b804
Revises: e4b8c2d6f901
Create Date: 2026-11-05 17:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a9c7d1b804"
down_revision: str | Sequence[str] | None = "e4b8c2d6f901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("bots") as batch:
        batch.add_column(sa.Column("last_settled_task_id", sa.Uuid(), nullable=True))
        batch.add_column(
            sa.Column("failure_threshold", sa.Integer(), nullable=False, server_default="3")
        )
        batch.add_column(
            sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("runs_total", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("runs_failed", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("last_error", sa.Text(), nullable=True))
        batch.add_column(sa.Column("paused_reason", sa.String(length=200), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("bots") as batch:
        batch.drop_column("paused_reason")
        batch.drop_column("last_error")
        batch.drop_column("runs_failed")
        batch.drop_column("runs_total")
        batch.drop_column("consecutive_failures")
        batch.drop_column("failure_threshold")
        batch.drop_column("last_settled_task_id")
