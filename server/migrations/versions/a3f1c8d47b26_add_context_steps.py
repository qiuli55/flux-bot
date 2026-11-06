"""新增 context_steps / context_blobs（FLUX_CONTEXT_MECHANISM §3.3 / §3.5）

上下文机制的最小闭环：一份只追加的 step 事件日志 + 一个内容寻址的原文库。
step 是压缩 / 检索 / 原子事实的最小单元；原文按 sha256 去重、存储层无损压缩，
取出来逐字节可还原。

Revision ID: a3f1c8d47b26
Revises: f2a7c4d9e1b3
Create Date: 2026-10-16 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f1c8d47b26"
down_revision: str | Sequence[str] | None = "f2a7c4d9e1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: SQLite 只有 INTEGER 主键才自增，故对 sqlite 方言降级为 Integer（与模型一致）。
_STEP_ID_TYPE = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "context_steps",
        sa.Column("id", _STEP_ID_TYPE, autoincrement=True, primary_key=True),
        sa.Column("run_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("turn_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("seq_in_turn", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("agent_fp", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("tool", sa.String(length=64), nullable=True),
        sa.Column("argv_digest", sa.String(length=128), nullable=True),
        sa.Column("target_ref", sa.String(length=1024), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("result_sha256", sa.String(length=64), nullable=True),
        sa.Column("result_bytes", sa.BigInteger(), nullable=True),
        sa.Column("result_head", sa.Text(), nullable=True),
        sa.Column("facts", sa.JSON(), nullable=True),
        sa.Column("unverified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_context_steps_run_id", "context_steps", ["run_id"])
    op.create_index("ix_context_steps_turn_id", "context_steps", ["turn_id"])
    op.create_index("ix_context_steps_kind", "context_steps", ["kind"])
    op.create_index("ix_context_steps_agent_fp", "context_steps", ["agent_fp"])
    op.create_index("ix_context_steps_target_ref", "context_steps", ["target_ref"])
    op.create_index("ix_context_steps_status", "context_steps", ["status"])
    op.create_index("ix_context_steps_result_sha256", "context_steps", ["result_sha256"])

    op.create_table(
        "context_blobs",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("codec", sa.String(length=16), nullable=False),
        sa.Column("size_raw", sa.BigInteger(), nullable=False),
        sa.Column("size_stored", sa.BigInteger(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_context_blobs_sha256", "context_blobs", ["sha256"], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_context_blobs_sha256", table_name="context_blobs")
    op.drop_table("context_blobs")
    op.drop_index("ix_context_steps_result_sha256", table_name="context_steps")
    op.drop_index("ix_context_steps_status", table_name="context_steps")
    op.drop_index("ix_context_steps_target_ref", table_name="context_steps")
    op.drop_index("ix_context_steps_agent_fp", table_name="context_steps")
    op.drop_index("ix_context_steps_kind", table_name="context_steps")
    op.drop_index("ix_context_steps_turn_id", table_name="context_steps")
    op.drop_index("ix_context_steps_run_id", table_name="context_steps")
    op.drop_table("context_steps")
