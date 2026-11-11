"""外部连接器：github_connections

按本地账号隔离的 GitHub OAuth 连接。令牌用密钥库的 SecretBox 加密后落库，
`account_key` 唯一；断开连接即删行。

Revision ID: c8e4a1f72b93
Revises: a7f3c9e1d284
Create Date: 2026-11-11 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8e4a1f72b93"
down_revision: str | Sequence[str] | None = "a7f3c9e1d284"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "github_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_key", sa.String(length=64), nullable=False),
        sa.Column("login", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("name", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("avatar_url", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("token_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("scopes", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("token_kind", sa.String(length=32), nullable=False, server_default="oauth"),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_key", name="uq_github_connections_account"),
    )
    op.create_index(
        op.f("ix_github_connections_account_key"),
        "github_connections",
        ["account_key"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_github_connections_account_key"), table_name="github_connections")
    op.drop_table("github_connections")
