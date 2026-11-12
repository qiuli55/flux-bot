"""手机设备配对与长期令牌

- `devices`：已配对设备；token_hash 只存 sha256，token_prefix 用于列表展示。
- `pairing_codes`：一次性配对码；code_hash 只存 sha256，used_at 非空即已兑换。

Revision ID: f9d2e4a7b1c8
Revises: e4f1a2b3c4d5
Create Date: 2026-11-12 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f9d2e4a7b1c8"
down_revision: str | Sequence[str] | None = "e4f1a2b3c4d5"
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
        "devices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_devices_token_hash"), "devices", ["token_hash"], unique=True)
    op.create_index(op.f("ix_devices_expires_at"), "devices", ["expires_at"])

    op.create_table(
        "pairing_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_pairing_codes_code_hash"), "pairing_codes", ["code_hash"], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_pairing_codes_code_hash"), table_name="pairing_codes")
    op.drop_table("pairing_codes")
    op.drop_index(op.f("ix_devices_expires_at"), table_name="devices")
    op.drop_index(op.f("ix_devices_token_hash"), table_name="devices")
    op.drop_table("devices")
