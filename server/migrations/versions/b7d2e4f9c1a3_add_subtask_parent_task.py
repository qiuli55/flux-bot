"""Bot 后台子任务：tasks.parent_task_id

Bot 对话里的长活可以派成独立子任务，用户继续聊天。子任务复用 tasks 表，
用 origin=subtask 区分普通对话/助理对话，并用 parent_task_id 指回父对话任务，
状态与结果以 kind=subtask 的消息追加到父对话里。

Revision ID: b7d2e4f9c1a3
Revises: f4b7e1c9a260
Create Date: 2026-11-13 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d2e4f9c1a3"
down_revision: str | Sequence[str] | None = "f4b7e1c9a260"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("parent_task_id", sa.Uuid(), nullable=True))
        batch_op.create_index("ix_tasks_parent_task_id", ["parent_task_id"])
        batch_op.create_foreign_key(
            "fk_tasks_parent_task_id_tasks",
            "tasks",
            ["parent_task_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_constraint("fk_tasks_parent_task_id_tasks", type_="foreignkey")
        batch_op.drop_index("ix_tasks_parent_task_id")
        batch_op.drop_column("parent_task_id")
