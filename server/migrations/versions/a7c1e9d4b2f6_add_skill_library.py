"""技能库：技能包与 Bot 分配

- skill_packages：技能正文（body）与 FluxSkill 对齐的元数据（spec）存在库里，
  可由 API 创建 / 编辑；name 唯一。
- bot_skills：Bot 与技能包的分配关系；unique (bot_id, skill_id)，
  position 决定注入顺序。skill_id 外键用 RESTRICT——技能仍被 Bot 使用时
  删除技能包必须先解除分配，不允许静默级联。

Revision ID: a7c1e9d4b2f6
Revises: f4b7e1c9a260
Create Date: 2026-11-13 03:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c1e9d4b2f6"
down_revision: str | Sequence[str] | None = "f4b7e1c9a260"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "skill_packages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("version", sa.String(length=64), nullable=True),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_skill_packages_name"),
    )
    op.create_index("ix_skill_packages_enabled", "skill_packages", ["enabled"])

    op.create_table(
        "bot_skills",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["skill_id"], ["skill_packages.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bot_id", "skill_id", name="uq_bot_skills_bot_skill"),
    )
    op.create_index(op.f("ix_bot_skills_bot_id"), "bot_skills", ["bot_id"])
    op.create_index(op.f("ix_bot_skills_skill_id"), "bot_skills", ["skill_id"])
    op.create_index("ix_bot_skills_bot", "bot_skills", ["bot_id", "position"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_bot_skills_bot", table_name="bot_skills")
    op.drop_index(op.f("ix_bot_skills_skill_id"), table_name="bot_skills")
    op.drop_index(op.f("ix_bot_skills_bot_id"), table_name="bot_skills")
    op.drop_table("bot_skills")
    op.drop_index("ix_skill_packages_enabled", table_name="skill_packages")
    op.drop_table("skill_packages")
