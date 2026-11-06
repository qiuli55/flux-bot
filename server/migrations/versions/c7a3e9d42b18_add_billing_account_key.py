"""计费账号维度：`usage_records` / `usage_snapshots` 增加 `account_key`

供应商额度是**账号级**的：同一个人在两台机器上用两个订阅账号，
或者一台机器换了 apiKey，看到的都是不同的配额。此前两张表只按 `plan_key`
聚合，多个账号的窗口会互相覆盖，账本也无法回答「这笔钱是哪个账号花的」。

- 新列 `account_key`（8 位十六进制指纹，NOT NULL DEFAULT ''）：
  `''` = 未识别账号（旧数据、或该套餐没有可解析的凭据）。
- 两张表都加索引：读取总是「套餐 × 账号 × 时间窗」。

Revision ID: c7a3e9d42b18
Revises: d4f8b1c6e2a9
Create Date: 2026-11-02 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7a3e9d42b18"
down_revision: str | Sequence[str] | None = "d4f8b1c6e2a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("usage_records", recreate="always") as batch:
        batch.add_column(
            sa.Column("account_key", sa.String(length=16), nullable=False, server_default="")
        )
        batch.create_index("ix_usage_records_account_key", ["account_key"], unique=False)

    with op.batch_alter_table("usage_snapshots", recreate="always") as batch:
        batch.add_column(
            sa.Column("account_key", sa.String(length=16), nullable=False, server_default="")
        )
        batch.create_index("ix_usage_snapshots_account_key", ["account_key"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("usage_snapshots", recreate="always") as batch:
        batch.drop_index("ix_usage_snapshots_account_key")
        batch.drop_column("account_key")

    with op.batch_alter_table("usage_records", recreate="always") as batch:
        batch.drop_index("ix_usage_records_account_key")
        batch.drop_column("account_key")
