"""会话上下文规模：`task_runtime_sessions.context_tokens`

外部会话跨 Run 续聊时，平台需要在下一轮开始前知道「这个会话已经有多大」，
才能决定要不要让 runtime 压缩 / 重开会话（`agent_context_compact_tokens`）。
`thread/resume` 之类的恢复接口不返回用量，只能由平台自己记住上一轮的规模。

- 新列 `context_tokens`，NOT NULL DEFAULT 0：0 = 未知（旧数据 / 该 runtime 不报送）。

Revision ID: d4f8b1c6e2a9
Revises: c9e2a5f7b3d1
Create Date: 2026-11-01 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4f8b1c6e2a9"
down_revision: str | Sequence[str] | None = "c9e2a5f7b3d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("task_runtime_sessions", recreate="always") as batch:
        batch.add_column(
            sa.Column("context_tokens", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("task_runtime_sessions", recreate="always") as batch:
        batch.drop_column("context_tokens")
