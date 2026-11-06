"""对话上下文上云：`task_messages` 增加 `cloud_uploaded_at`

隐私输入以外（`secure_input_result` 除外）的对话消息要增量推到账号云，作为
「换设备也能回看对话」的上下文。上传队列直接落在消息行上，不另建游标文件：

- 新列 `cloud_uploaded_at`（可空）：NULL = 尚未处理；有值 = 已上传，
  或命中本地密钥红线被主动跳过（两类都表示"这条不用再传"）。
- 加索引：后台循环只扫「未处理」的少量行，全表扫描在大对话上会越来越慢。

Revision ID: d2f8a6c41e07
Revises: c7a3e9d42b18
Create Date: 2026-11-02 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2f8a6c41e07"
down_revision: str | Sequence[str] | None = "c7a3e9d42b18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("task_messages", recreate="always") as batch:
        batch.add_column(sa.Column("cloud_uploaded_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index(
            "ix_task_messages_cloud_uploaded_at", ["cloud_uploaded_at"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("task_messages", recreate="always") as batch:
        batch.drop_index("ix_task_messages_cloud_uploaded_at")
        batch.drop_column("cloud_uploaded_at")
