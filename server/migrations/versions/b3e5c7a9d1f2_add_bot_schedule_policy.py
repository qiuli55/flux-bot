"""Bot 调度时区与漏跑策略

- `timezone`：Bot 自己的 IANA 时区；NULL = 继承 FLUX_TIMEZONE。
- `missed_run_policy`：停机错过的触发点如何处理（skip / skip_silent / catch_up），
  默认 skip，与旧调度行为一致。

Revision ID: b3e5c7a9d1f2
Revises: f9d2e4a7b1c8
Create Date: 2026-11-12 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3e5c7a9d1f2"
down_revision: str | Sequence[str] | None = "f9d2e4a7b1c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("bots") as batch:
        batch.add_column(sa.Column("timezone", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column(
                "missed_run_policy",
                sa.String(length=16),
                nullable=False,
                server_default="skip",
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("bots") as batch:
        batch.drop_column("missed_run_policy")
        batch.drop_column("timezone")
