"""设备 / 配对码归属到 Flux 云用户

Phase 1 按用户隔离：设备不再只属于"这台机器"，而是属于某个 Flux 云账号。
- `devices.cloud_user_id`：配对成功后继承配对码上的归属；
- `pairing_codes.cloud_user_id`：签发时记录是谁的码。

两列都可为 NULL：历史数据保持原样，读取时按 FLUX_OWNER_CLOUD_USER_ID 兜底。
索引非唯一：一个人可以有多台设备 / 多个未兑换的配对码。

Revision ID: a8c4e2f19b70
Revises: 9f2c4e6a8b1d
Create Date: 2026-11-14 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8c4e2f19b70"
down_revision: str | Sequence[str] | None = "9f2c4e6a8b1d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("devices", schema=None) as batch_op:
        batch_op.add_column(sa.Column("cloud_user_id", sa.String(length=64), nullable=True))
        batch_op.create_index("ix_devices_cloud_user_id", ["cloud_user_id"])

    with op.batch_alter_table("pairing_codes", schema=None) as batch_op:
        batch_op.add_column(sa.Column("cloud_user_id", sa.String(length=64), nullable=True))
        batch_op.create_index("ix_pairing_codes_cloud_user_id", ["cloud_user_id"])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("pairing_codes", schema=None) as batch_op:
        batch_op.drop_index("ix_pairing_codes_cloud_user_id")
        batch_op.drop_column("cloud_user_id")

    with op.batch_alter_table("devices", schema=None) as batch_op:
        batch_op.drop_index("ix_devices_cloud_user_id")
        batch_op.drop_column("cloud_user_id")
