"""Bot 例程的事件触发

在 `bot_routines` 上增加 `event_triggers`（JSON 数组）：例程除了按 cron 开工，
还可以被外部 Webhook 事件（`webhook.received` / `webhook.<slug>`）或
GitHub 事件（`github.<event>`）触发。NULL / 空数组 = 只按 cron 跑。

Revision ID: e6a4c2d81f37
Revises: b3e5c7a9d1f2
Create Date: 2026-11-12 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6a4c2d81f37"
down_revision: str | Sequence[str] | None = "b3e5c7a9d1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("bot_routines") as batch:
        batch.add_column(sa.Column("event_triggers", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("bot_routines") as batch:
        batch.drop_column("event_triggers")
