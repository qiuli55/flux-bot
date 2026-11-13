"""用户保险库：站点登录 / 支付卡 / 联系人

- `vault_logins`：站点、用户名、密码密文（SecretBox 信封）、备注；
- `vault_cards`：只存展示字段（last4 / brand / 有效期）+ PAN/CVV 密文；
- `vault_contacts`：姓名、电话数组、邮箱数组、备注（PII，日志不倾倒）。

密文复用密钥库同一把主密钥（FLUX_KEY_VAULT_SECRET 或 ~/.flux/key-vault.key）。
三张表都是新增，不改动既有表。

Revision ID: d7a1f4c9b2e6
Revises: f4b7e1c9a260
Create Date: 2026-11-13 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7a1f4c9b2e6"
down_revision: str | Sequence[str] | None = "f4b7e1c9a260"
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
        "vault_logins",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("site", sa.String(length=512), nullable=False),
        sa.Column("username", sa.String(length=256), nullable=False),
        sa.Column("password_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_vault_logins_site"), "vault_logins", ["site"])

    op.create_table(
        "vault_cards",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("brand", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("last4", sa.String(length=4), nullable=False),
        sa.Column("exp_month", sa.Integer(), nullable=True),
        sa.Column("exp_year", sa.Integer(), nullable=True),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("has_pan", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("has_cvv", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "vault_contacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("phones", sa.JSON(), nullable=False),
        sa.Column("emails", sa.JSON(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_vault_contacts_name"), "vault_contacts", ["name"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_vault_contacts_name"), table_name="vault_contacts")
    op.drop_table("vault_contacts")
    op.drop_table("vault_cards")
    op.drop_index(op.f("ix_vault_logins_site"), table_name="vault_logins")
    op.drop_table("vault_logins")
