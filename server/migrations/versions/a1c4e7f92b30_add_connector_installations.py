"""外部连接器：connector_installations

用户账号侧的通用连接器安装记录。GitHub 的 OAuth 令牌仍保存在
`github_connections`（历史结构不动）；本表承载 api_key 类连接的凭据密文
与通用安装元数据，按 `(account_key, connector_id)` 唯一隔离。

Revision ID: a1c4e7f92b30
Revises: b3e5c7a9d1f2
Create Date: 2026-11-12 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c4e7f92b30"
down_revision: str | Sequence[str] | None = "b3e5c7a9d1f2"
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
        "connector_installations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_key", sa.String(length=64), nullable=False),
        sa.Column("connector_id", sa.String(length=64), nullable=False),
        sa.Column("auth_type", sa.String(length=32), nullable=False),
        sa.Column("scopes", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("credential_ciphertext", sa.LargeBinary(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_key",
            "connector_id",
            name="uq_connector_installations_account_connector",
        ),
    )
    op.create_index(
        op.f("ix_connector_installations_account_key"),
        "connector_installations",
        ["account_key"],
    )
    op.create_index(
        op.f("ix_connector_installations_connector_id"),
        "connector_installations",
        ["connector_id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_connector_installations_connector_id"),
        table_name="connector_installations",
    )
    op.drop_index(
        op.f("ix_connector_installations_account_key"),
        table_name="connector_installations",
    )
    op.drop_table("connector_installations")
