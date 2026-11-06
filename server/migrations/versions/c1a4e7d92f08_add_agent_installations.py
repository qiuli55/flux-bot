"""新增 agent_installations 表（最终方案 §3.2 / §4：Agent Discovery 与接入状态机）

记录本机 CLI Agent 的安装事实与状态（DISCOVERED → VERIFIED → CONNECTED → READY），
与 agents 表（身份 / 权限边界）分离：档案描述"Flux 认识谁"，本表描述"这台机器有什么"。

Revision ID: c1a4e7d92f08
Revises: b2e6a4c81d35
Create Date: 2026-10-04 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1a4e7d92f08"
down_revision: str | Sequence[str] | None = "b2e6a4c81d35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "agent_installations",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("adapter", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("executable", sa.String(length=128), nullable=True),
        sa.Column("path", sa.String(length=512), nullable=True),
        sa.Column("version", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="path"),
        sa.Column("auth_status", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("capabilities", sa.JSON(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_agent_installations_name", "agent_installations", ["name"], unique=True)
    op.create_index("ix_agent_installations_status", "agent_installations", ["status"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_agent_installations_status", table_name="agent_installations")
    op.drop_index("ix_agent_installations_name", table_name="agent_installations")
    op.drop_table("agent_installations")
