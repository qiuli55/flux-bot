"""Phase 4：用户画像 / 共享日志按 Flux 云用户隔离

- `user_profile_entries` / `user_journal_entries` 增加可空 `cloud_user_id`（索引）。
- 删除原先的全局唯一约束（画像 key / 日志 content_hash），改为同一用户内唯一：
  两个人可以各有一个 `name`，也可以各写一条完全相同的观察。
- 历史 NULL 行不迁移、不猜归属：读取时按 `FLUX_OWNER_CLOUD_USER_ID` 兼容
  （见 `flux.core.user_scope.owned_clause`）。SQL 的 UNIQUE 把 NULL 视为互不相等，
  因此再用部分唯一索引保证历史 NULL 行仍然最多只有一条（旧全局约束已经保证）。

Revision ID: c4f1a8e2d907
Revises: b9f3c1d8a4e2
Create Date: 2026-11-14 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4f1a8e2d907"
down_revision: str | Sequence[str] | None = "b9f3c1d8a4e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("user_profile_entries", schema=None) as batch_op:
        batch_op.add_column(sa.Column("cloud_user_id", sa.String(length=64), nullable=True))
        batch_op.drop_constraint("uq_user_profile_entries_key", type_="unique")
        batch_op.create_unique_constraint(
            "uq_user_profile_entries_user_key", ["cloud_user_id", "key"]
        )
        batch_op.create_index(
            batch_op.f("ix_user_profile_entries_cloud_user_id"), ["cloud_user_id"]
        )

    with op.batch_alter_table("user_journal_entries", schema=None) as batch_op:
        batch_op.add_column(sa.Column("cloud_user_id", sa.String(length=64), nullable=True))
        batch_op.drop_constraint("uq_user_journal_entries_hash", type_="unique")
        batch_op.create_unique_constraint(
            "uq_user_journal_entries_user_hash", ["cloud_user_id", "content_hash"]
        )
        batch_op.create_index(
            batch_op.f("ix_user_journal_entries_cloud_user_id"), ["cloud_user_id"]
        )

    # 历史 NULL 行只保留一条（旧全局唯一已经保证）；新写入永远带 cloud_user_id。
    op.create_index(
        "uq_user_profile_entries_legacy_key",
        "user_profile_entries",
        ["key"],
        unique=True,
        sqlite_where=sa.text("cloud_user_id IS NULL"),
        postgresql_where=sa.text("cloud_user_id IS NULL"),
    )
    op.create_index(
        "uq_user_journal_entries_legacy_hash",
        "user_journal_entries",
        ["content_hash"],
        unique=True,
        sqlite_where=sa.text("cloud_user_id IS NULL"),
        postgresql_where=sa.text("cloud_user_id IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_user_journal_entries_legacy_hash", table_name="user_journal_entries")
    op.drop_index("uq_user_profile_entries_legacy_key", table_name="user_profile_entries")

    with op.batch_alter_table("user_journal_entries", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_journal_entries_cloud_user_id"))
        batch_op.drop_constraint("uq_user_journal_entries_user_hash", type_="unique")
        batch_op.create_unique_constraint("uq_user_journal_entries_hash", ["content_hash"])
        batch_op.drop_column("cloud_user_id")

    with op.batch_alter_table("user_profile_entries", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_profile_entries_cloud_user_id"))
        batch_op.drop_constraint("uq_user_profile_entries_user_key", type_="unique")
        batch_op.create_unique_constraint("uq_user_profile_entries_key", ["key"])
        batch_op.drop_column("cloud_user_id")
