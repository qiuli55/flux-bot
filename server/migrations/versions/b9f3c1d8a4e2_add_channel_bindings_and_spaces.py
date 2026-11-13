"""Phase 3：IM 账号绑定 + 共享空间（shared 记忆）

- `channel_user_bindings`：`(channel, external_user_id)` → Flux 云用户 id。
- `channel_bind_codes`：一次性绑定码（只存 sha256）。
- `spaces` / `space_members`：两个云用户共享一层记忆的最小容器；不复用指向本地
  `users.id` 的 organizations。
- `memories.space_id`：Shared 层条目所属空间（User / Environment 恒 NULL）。
- `bot_turns` / `bot_routines` / `outbound_drafts` / `tasks` 增加 `cloud_user_id`：
  对话、例程、出站审批与 Agent 开工按人运行。全部可空，历史 NULL 归
  `FLUX_OWNER_CLOUD_USER_ID`（见 flux.core.user_scope）。

Revision ID: b9f3c1d8a4e2
Revises: e2b9c4d71a58
Create Date: 2026-11-14 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9f3c1d8a4e2"
down_revision: str | Sequence[str] | None = "e2b9c4d71a58"
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
        "spaces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("created_by_cloud_user_id", sa.String(length=64), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_spaces_created_by_cloud_user_id"),
        "spaces",
        ["created_by_cloud_user_id"],
        unique=False,
    )

    op.create_table(
        "space_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("cloud_user_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), server_default="member", nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("space_id", "cloud_user_id", name="uq_space_members_space_user"),
    )
    op.create_index(op.f("ix_space_members_space_id"), "space_members", ["space_id"])
    op.create_index(op.f("ix_space_members_cloud_user_id"), "space_members", ["cloud_user_id"])

    op.create_table(
        "channel_user_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("external_user_id", sa.String(length=128), nullable=False),
        sa.Column("cloud_user_id", sa.String(length=64), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel", "external_user_id", name="uq_channel_bindings_channel_user"),
    )
    op.create_index(op.f("ix_channel_user_bindings_channel"), "channel_user_bindings", ["channel"])
    op.create_index(
        op.f("ix_channel_user_bindings_cloud_user_id"),
        "channel_user_bindings",
        ["cloud_user_id"],
    )

    op.create_table(
        "channel_bind_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("cloud_user_id", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_channel_bind_codes_code_hash"), "channel_bind_codes", ["code_hash"], unique=True
    )
    op.create_index(
        op.f("ix_channel_bind_codes_cloud_user_id"),
        "channel_bind_codes",
        ["cloud_user_id"],
    )
    op.create_index(op.f("ix_channel_bind_codes_expires_at"), "channel_bind_codes", ["expires_at"])

    # memories.space_id：Shared 层所属空间；User / Environment 行保持 NULL。
    with op.batch_alter_table("memories", schema=None) as batch_op:
        batch_op.add_column(sa.Column("space_id", sa.Uuid(), nullable=True))
        batch_op.create_index(op.f("ix_memories_space_id"), ["space_id"])
        batch_op.create_foreign_key(
            "fk_memories_space_id_spaces",
            "spaces",
            ["space_id"],
            ["id"],
            ondelete="CASCADE",
        )

    for table in ("tasks", "bot_turns", "bot_routines", "outbound_drafts"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("cloud_user_id", sa.String(length=64), nullable=True))
            batch_op.create_index(
                op.f(f"ix_{table}_cloud_user_id"), ["cloud_user_id"], unique=False
            )


def downgrade() -> None:
    """Downgrade schema."""
    for table in ("outbound_drafts", "bot_routines", "bot_turns", "tasks"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_index(op.f(f"ix_{table}_cloud_user_id"))
            batch_op.drop_column("cloud_user_id")

    with op.batch_alter_table("memories", schema=None) as batch_op:
        batch_op.drop_constraint("fk_memories_space_id_spaces", type_="foreignkey")
        batch_op.drop_index(op.f("ix_memories_space_id"))
        batch_op.drop_column("space_id")

    op.drop_index(op.f("ix_channel_bind_codes_expires_at"), table_name="channel_bind_codes")
    op.drop_index(op.f("ix_channel_bind_codes_cloud_user_id"), table_name="channel_bind_codes")
    op.drop_index(op.f("ix_channel_bind_codes_code_hash"), table_name="channel_bind_codes")
    op.drop_table("channel_bind_codes")

    op.drop_index(
        op.f("ix_channel_user_bindings_cloud_user_id"), table_name="channel_user_bindings"
    )
    op.drop_index(op.f("ix_channel_user_bindings_channel"), table_name="channel_user_bindings")
    op.drop_table("channel_user_bindings")

    op.drop_index(op.f("ix_space_members_cloud_user_id"), table_name="space_members")
    op.drop_index(op.f("ix_space_members_space_id"), table_name="space_members")
    op.drop_table("space_members")

    op.drop_index(op.f("ix_spaces_created_by_cloud_user_id"), table_name="spaces")
    op.drop_table("spaces")
