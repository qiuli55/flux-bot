"""新增 context_verifications 验证账本（FLUX_CONTEXT_MECHANISM §4.4）

"不信任"是默认起点，但必须是动态量：账本按「指纹 × 断言类」记下每次验证的结论，
信任分级据此判定，让信任能长回来、也能一次被抓就摔回去（回落 L0）。

账本是派生数据，故随会话级联删除（§8.2 / §13-5）。
主键用自增整数而非 UUID：分级要判"最近一次、连胜多长"，账本必须能按发生顺序排，
而 SQLite 的 created_at 只到秒，同一秒内的多条排不出先后。

Revision ID: e2a7c4f19b83
Revises: c5d09e27b4a1
Create Date: 2026-10-16 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2a7c4f19b83"
down_revision: str | Sequence[str] | None = "c5d09e27b4a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 与 context_steps.id 同型（SQLite 只有 INTEGER 主键才自增）
_STEP_ID_TYPE = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "context_verifications",
        sa.Column("id", _STEP_ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column("agent_fp", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("claim_kind", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("turn_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("step_id", _STEP_ID_TYPE, nullable=True),
        sa.Column("note", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_context_verifications_agent_fp", "context_verifications", ["agent_fp"])
    op.create_index("ix_context_verifications_claim_kind", "context_verifications", ["claim_kind"])
    op.create_index("ix_context_verifications_outcome", "context_verifications", ["outcome"])
    op.create_index("ix_context_verifications_run_id", "context_verifications", ["run_id"])
    op.create_index("ix_context_verifications_turn_id", "context_verifications", ["turn_id"])
    op.create_index("ix_context_verifications_step_id", "context_verifications", ["step_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_context_verifications_step_id", table_name="context_verifications")
    op.drop_index("ix_context_verifications_turn_id", table_name="context_verifications")
    op.drop_index("ix_context_verifications_run_id", table_name="context_verifications")
    op.drop_index("ix_context_verifications_outcome", table_name="context_verifications")
    op.drop_index("ix_context_verifications_claim_kind", table_name="context_verifications")
    op.drop_index("ix_context_verifications_agent_fp", table_name="context_verifications")
    op.drop_table("context_verifications")
