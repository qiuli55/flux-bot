"""add outbound_drafts.attachments 出站图片 / 文件元数据

出站不再只有文本：草稿新增 `attachments` JSON 数组，保存 attachment_store 的
upload_id、文件名、mime、大小与 image/file 类型。文件本体仍在 attachment_store，
进程重启后按 upload_id 还原；历史纯文本草稿回填空数组。

Revision ID: b7d3e5f9a2c1
Revises: c4f1a8e2d907
Create Date: 2026-11-14 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d3e5f9a2c1"
down_revision: str | Sequence[str] | None = "c4f1a8e2d907"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("outbound_drafts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "attachments",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("outbound_drafts", schema=None) as batch_op:
        batch_op.drop_column("attachments")
