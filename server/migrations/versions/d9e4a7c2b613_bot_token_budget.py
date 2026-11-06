"""Bot 每日 token 预算

给常驻 Bot 加一个可自行配置的每日 token 上限：
- `token_budget` 为 NULL 或 0 表示不限制；
- 已用量不落库，按 UTC 自然日从 `usage_records`（经 `tasks.bot_id` 归因）实时聚合，
  账本是唯一权威，因此天然幂等，不需要额外的结算字段。

Revision ID: d9e4a7c2b613
Revises: f2a9c7d1b804
Create Date: 2026-11-05 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9e4a7c2b613"
down_revision: str | Sequence[str] | None = "f2a9c7d1b804"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("bots") as batch:
        batch.add_column(sa.Column("token_budget", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("bots") as batch:
        batch.drop_column("token_budget")
