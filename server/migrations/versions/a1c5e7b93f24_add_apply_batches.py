"""新增 apply_batches 表（Apply 事务日志）

P0-1（Apply 崩溃恢复）：apply_many 在任何磁盘操作之前先落一行 in_progress 记录，
每过一个阶段更新 phase；服务被杀后重启时扫这张表，按磁盘事实还原/删除半应用的改动，
把结果写进 status / recovery_note。这张表同时是 P1-2 整批回滚的分组依据。

Revision ID: a1c5e7b93f24
Revises: b7e2f4a9c1d3
Create Date: 2026-10-05 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c5e7b93f24"
down_revision: str | Sequence[str] | None = "b7e2f4a9c1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "apply_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("change_ids", sa.JSON(), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("backup_root", sa.String(length=512), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("recovery_note", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_index("ix_apply_batches_status", "apply_batches", ["status"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_apply_batches_status", table_name="apply_batches")
    op.drop_table("apply_batches")
