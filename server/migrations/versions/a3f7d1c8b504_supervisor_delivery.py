"""助理（Supervisor）P3：outbox 幂等键与订阅唯一性

订阅直通要求"同一份报告只投递一次"：报告落库时同事务写一条 `supervisor.report_ready`
outbox 行，幂等键取 `report:<report_id>`；重复对账不会重复投递。

同一工单对同一平台线程只应有一条订阅，否则一条报告会被发多次，因此给
`supervisor_subscriptions(ticket_id, source_key, external_id)` 加唯一索引。

Revision ID: a3f7d1c8b504
Revises: e1b7c4a9d2f6
Create Date: 2026-11-03 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f7d1c8b504"
down_revision: str | Sequence[str] | None = "e1b7c4a9d2f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "supervisor_outbox",
        sa.Column("dedupe_key", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_supervisor_outbox_dedupe_key",
        "supervisor_outbox",
        ["dedupe_key"],
        unique=True,
    )
    op.create_index(
        "ix_supervisor_subscriptions_target",
        "supervisor_subscriptions",
        ["ticket_id", "source_key", "external_id"],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_supervisor_subscriptions_target",
        table_name="supervisor_subscriptions",
    )
    op.drop_index("ix_supervisor_outbox_dedupe_key", table_name="supervisor_outbox")
    with op.batch_alter_table("supervisor_outbox") as batch_op:
        batch_op.drop_column("dedupe_key")
