"""Bot 产出的可分享制品页面

- `artifact_pages`：页面当前状态（标题 / slug / 公开 share_token / 状态 /
  当前版本号 / 内容类型）。公开链接按 `share_token` 寻址，不暴露自增 id。
- `artifact_page_revisions`：只追加的版本历史。每次保存正文或标题都新增一行，
  旧版本永不改写；删除页面时版本随之级联清理。

Revision ID: b3d7e1a9c5f2
Revises: f4b7e1c9a260
Create Date: 2026-11-13 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3d7e1a9c5f2"
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
        "artifact_pages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("share_token", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("current_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "content_type",
            sa.String(length=32),
            nullable=False,
            server_default="text/markdown",
        ),
        *_timestamps(),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_artifact_pages_slug"),
        sa.UniqueConstraint("share_token", name="uq_artifact_pages_share_token"),
    )
    op.create_index(op.f("ix_artifact_pages_bot_id"), "artifact_pages", ["bot_id"])
    op.create_index("ix_artifact_pages_status", "artifact_pages", ["status"])

    op.create_table(
        "artifact_page_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(length=32), nullable=False),
        sa.Column("author_bot_id", sa.Uuid(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["page_id"], ["artifact_pages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_bot_id"], ["bots.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("page_id", "revision", name="uq_artifact_page_revisions_page_revision"),
    )
    op.create_index(
        op.f("ix_artifact_page_revisions_page_id"),
        "artifact_page_revisions",
        ["page_id"],
    )
    op.create_index(
        op.f("ix_artifact_page_revisions_author_bot_id"),
        "artifact_page_revisions",
        ["author_bot_id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_artifact_page_revisions_author_bot_id"),
        table_name="artifact_page_revisions",
    )
    op.drop_index(
        op.f("ix_artifact_page_revisions_page_id"),
        table_name="artifact_page_revisions",
    )
    op.drop_table("artifact_page_revisions")
    op.drop_index("ix_artifact_pages_status", table_name="artifact_pages")
    op.drop_index(op.f("ix_artifact_pages_bot_id"), table_name="artifact_pages")
    op.drop_table("artifact_pages")
