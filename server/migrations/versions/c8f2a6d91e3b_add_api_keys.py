"""add api_keys 密钥库与任务级 key 引用

密钥库让 codex / dsh / opencode 等 Agent 的模型通道可以使用界面里配置的 key，
而不是只能读服务进程环境变量：

- `provider`：deepseek / openai / openai_compatible / anthropic；
- `key_ciphertext`：SecretBox 信封密文，明文不落库、不出接口；
- `models`：该 key 可用的模型 id 列表；
- `tasks.api_key_id`：任务级模型来自哪条 key；NULL = 原生模型。

Revision ID: c8f2a6d91e3b
Revises: b3e8c1a7d2f4
Create Date: 2026-10-23 18:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8f2a6d91e3b"
down_revision: str | Sequence[str] | None = "b3e8c1a7d2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "api_keys",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("wire_api", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("models", sa.JSON(), nullable=False),
        sa.Column("key_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_hint", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_api_keys_name"), ["name"], unique=True)
        batch_op.create_index(batch_op.f("ix_api_keys_provider"), ["provider"], unique=False)
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("api_key_id", sa.Uuid(), nullable=True))
        batch_op.create_index(batch_op.f("ix_tasks_api_key_id"), ["api_key_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_tasks_api_key_id"))
        batch_op.drop_column("api_key_id")
    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_api_keys_provider"))
        batch_op.drop_index(batch_op.f("ix_api_keys_name"))
    op.drop_table("api_keys")
