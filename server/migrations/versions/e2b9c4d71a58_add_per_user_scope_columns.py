"""Phase 2 按用户隔离：记忆 / API Key / 用量归属到 Flux 云用户

- `memories.cloud_user_id`：User 层记忆的归属人；Environment 仍为 NULL（全局）。
- `api_keys.cloud_user_id`：密钥归属人；`name` 从全局唯一改为同一用户内唯一
  （服务层校验 + 非唯一索引），两个人可以都叫「DeepSeek 官方」。
- `usage_records.cloud_user_id` / `usage_snapshots.cloud_user_id`：账本与官方额度的
  归属人。注意 `account_key` 仍是凭据指纹（sha256[:8]），不是人。

全部可空：历史数据保持原样，读取时 NULL/空串按 `FLUX_OWNER_CLOUD_USER_ID` 归属
（见 `flux.core.user_scope`）。

Revision ID: e2b9c4d71a58
Revises: a8c4e2f19b70
Create Date: 2026-11-14 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2b9c4d71a58"
down_revision: str | Sequence[str] | None = "a8c4e2f19b70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("memories", schema=None) as batch_op:
        batch_op.add_column(sa.Column("cloud_user_id", sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f("ix_memories_cloud_user_id"), ["cloud_user_id"])

    # name 原先全局唯一；改成普通索引，唯一性由 KeyVaultService 按人校验。
    op.drop_index(op.f("ix_api_keys_name"), table_name="api_keys")
    op.create_index(op.f("ix_api_keys_name"), "api_keys", ["name"], unique=False)
    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.add_column(sa.Column("cloud_user_id", sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f("ix_api_keys_cloud_user_id"), ["cloud_user_id"])

    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.add_column(sa.Column("cloud_user_id", sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f("ix_usage_records_cloud_user_id"), ["cloud_user_id"])

    with op.batch_alter_table("usage_snapshots", schema=None) as batch_op:
        batch_op.add_column(sa.Column("cloud_user_id", sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f("ix_usage_snapshots_cloud_user_id"), ["cloud_user_id"])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("usage_snapshots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_usage_snapshots_cloud_user_id"))
        batch_op.drop_column("cloud_user_id")

    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_usage_records_cloud_user_id"))
        batch_op.drop_column("cloud_user_id")

    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_api_keys_cloud_user_id"))
        batch_op.drop_column("cloud_user_id")
    op.drop_index(op.f("ix_api_keys_name"), table_name="api_keys")
    # 回退到全局唯一：若两个用户已经建了同名 key，这一步会失败（数据需要先清理）。
    op.create_index(op.f("ix_api_keys_name"), "api_keys", ["name"], unique=True)

    with op.batch_alter_table("memories", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_memories_cloud_user_id"))
        batch_op.drop_column("cloud_user_id")
