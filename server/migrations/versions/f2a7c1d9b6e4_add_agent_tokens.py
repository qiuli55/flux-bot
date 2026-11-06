"""add agent_tokens for MCP access

MCP 能力面（目标架构 §3.2）的唯一鉴权凭据：
- token_hash：只存 sha256 十六进制，明文 `fxt_`+32 字节 hex 只在签发时返回一次
- scopes：该令牌可调用的能力集合；`secret.access` 永不入库（§3.5 硬禁令）
- revoked_at：撤销即失效，鉴权现查现判、不做进程内缓存

agent_id 不设外键：内置档案在 AgentManager 的内存注册表里，
外部 agent（codex / claude-code / opencode）不在 agents 表内。

Revision ID: f2a7c1d9b6e4
Revises: e7c05f3d81a2
Create Date: 2026-10-01 08:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a7c1d9b6e4"
down_revision: str | Sequence[str] | None = "e7c05f3d81a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "agent_tokens",
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
    with op.batch_alter_table("agent_tokens", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_agent_tokens_agent_id"), ["agent_id"], unique=False)
        # 唯一索引同时兜住"同一枚令牌被登记两次"的编程错误
        batch_op.create_index(batch_op.f("ix_agent_tokens_token_hash"), ["token_hash"], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("agent_tokens", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_agent_tokens_token_hash"))
        batch_op.drop_index(batch_op.f("ix_agent_tokens_agent_id"))
    op.drop_table("agent_tokens")
