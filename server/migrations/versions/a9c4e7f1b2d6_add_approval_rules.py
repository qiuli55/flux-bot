"""add approval_rules 全局审批规则

产品约定改为「默认零审批」：用户按云账号添加规则（支付阈值 / 命令模式 /
工具或任务匹配 / 出站消息），规则对该用户所有 Bot 生效。空表即零审批。

本迁移**不写入任何种子规则**。

Revision ID: a9c4e7f1b2d6
Revises: b7d3e5f9a2c1
Create Date: 2026-11-14 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9c4e7f1b2d6"
down_revision: str | Sequence[str] | None = "b7d3e5f9a2c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "approval_rules",
        sa.Column("cloud_user_id", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_by_kind", sa.String(length=8), nullable=False, server_default="user"),
        sa.Column("created_by_bot_id", sa.Uuid(), nullable=True),
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
    op.create_index(
        "ix_approval_rules_cloud_user_id", "approval_rules", ["cloud_user_id"], unique=False
    )
    op.create_index("ix_approval_rules_type", "approval_rules", ["type"], unique=False)
    op.create_index("ix_approval_rules_enabled", "approval_rules", ["enabled"], unique=False)
    op.create_index(
        "ix_approval_rules_created_by_bot_id",
        "approval_rules",
        ["created_by_bot_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_approval_rules_created_by_bot_id", table_name="approval_rules")
    op.drop_index("ix_approval_rules_enabled", table_name="approval_rules")
    op.drop_index("ix_approval_rules_type", table_name="approval_rules")
    op.drop_index("ix_approval_rules_cloud_user_id", table_name="approval_rules")
    op.drop_table("approval_rules")
