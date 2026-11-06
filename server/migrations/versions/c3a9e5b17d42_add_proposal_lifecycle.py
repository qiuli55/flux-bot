"""virtual_changes 补提案生命周期字段

P0-02（Proposal 生命周期）需要三列才能落地"过期/失效"与"整组审核"：
- group_id：同一次 CodeChangeSet 落成的多条提案共享，用于整组批准/拒绝/落盘
- expires_at：审核截止时间，超过即 expired（无时区，统一按 UTC 存储）
- expired_reason：失效原因（超时 / 被更新的提案取代），供 UI 与审计解释

Revision ID: c3a9e5b17d42
Revises: a7d3f1c9e5b2
Create Date: 2026-10-02 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3a9e5b17d42"
down_revision: str | Sequence[str] | None = "a7d3f1c9e5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # batch 模式：SQLite 不支持 ALTER COLUMN，显式 batch 保证本地与 PostgreSQL 都能跑。
    with op.batch_alter_table("virtual_changes", schema=None) as batch_op:
        batch_op.add_column(sa.Column("group_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("expired_reason", sa.Text(), nullable=True))
        batch_op.create_index("ix_virtual_changes_group_id", ["group_id"])
        batch_op.create_index("ix_virtual_changes_expires_at", ["expires_at"])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("virtual_changes", schema=None) as batch_op:
        batch_op.drop_index("ix_virtual_changes_expires_at")
        batch_op.drop_index("ix_virtual_changes_group_id")
        batch_op.drop_column("expired_reason")
        batch_op.drop_column("expires_at")
        batch_op.drop_column("group_id")
