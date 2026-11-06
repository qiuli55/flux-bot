"""群聊成员基准从 tasks 迁到 bots

普通对话（`tasks.bot_id` 为空）不支持群聊，群成员只可能是常驻 Bot，
所以三张表的 `task_id` 直接换成 `bots.id`：

- chat_group_members / chat_group_member_states：`task_id` → `bot_id`，
  NOT NULL + CASCADE；
- chat_group_messages：说话者改成 `bot_id`，可空（用户消息 / Bot 已删除）。

老数据按 `tasks.bot_id` 回填；对不上 Bot 的成员行直接删除——旧契约下
一条 task 就是一个「bot 对话」，没有 Bot 的 task 在新契约里不该出现在群里。
回填后可能把同一群的多个成员折成同一个 Bot，按最早一行保留。

Revision ID: d3e7c1b94a60
Revises: c6b3f8a1d027
Create Date: 2026-11-05 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3e7c1b94a60"
down_revision: str | Sequence[str] | None = "c6b3f8a1d027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 需要改列的表：(表名, 旧唯一约束名, 旧索引名, 新唯一约束名, 新索引名)
_MEMBER_TABLES: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "chat_group_members",
        "uq_chat_group_members_group_task",
        "ix_chat_group_members_task",
        "uq_chat_group_members_group_bot",
        "ix_chat_group_members_bot",
    ),
    (
        "chat_group_member_states",
        "uq_chat_group_member_states_group_task",
        "ix_chat_group_member_states_task",
        "uq_chat_group_member_states_group_bot",
        "ix_chat_group_member_states_bot",
    ),
)


def _add_bot_id(table: str) -> None:
    """加 `bot_id` 列，并按 `tasks.bot_id` 回填。"""
    op.add_column(table, sa.Column("bot_id", sa.Uuid(), nullable=True))
    op.execute(
        f"UPDATE {table} SET bot_id = "  # noqa: S608 — 表名来自本文件的常量
        f"(SELECT tasks.bot_id FROM tasks WHERE tasks.id = {table}.task_id)"
    )


def upgrade() -> None:
    """Upgrade schema."""
    for table in (*[item[0] for item in _MEMBER_TABLES], "chat_group_messages"):
        _add_bot_id(table)

    for table, old_unique, old_index, new_unique, new_index in _MEMBER_TABLES:
        # 没有对应 Bot 的成员、以及回填后重复的同一 Bot，都不该留在群里
        op.execute(f"DELETE FROM {table} WHERE bot_id IS NULL")
        # PostgreSQL 没有 min(uuid)，按文本取最小 UUID 只用于「每组留一行」
        op.execute(
            f"DELETE FROM {table} WHERE CAST(id AS TEXT) NOT IN "  # noqa: S608 — 常量表名
            f"(SELECT MIN(CAST(id AS TEXT)) FROM {table} GROUP BY group_id, bot_id)"
        )
        with op.batch_alter_table(table, recreate="always") as batch:
            batch.drop_index(old_index)
            batch.drop_constraint(old_unique, type_="unique")
            batch.drop_column("task_id")
            batch.alter_column("bot_id", existing_type=sa.Uuid(), nullable=False)
            batch.create_foreign_key(
                f"fk_{table}_bot_id_bots", "bots", ["bot_id"], ["id"], ondelete="CASCADE"
            )
            batch.create_unique_constraint(new_unique, ["group_id", "bot_id"])
            batch.create_index(new_index, ["bot_id"])

    # 消息允许没有说话者（用户消息），只换列与外键
    with op.batch_alter_table("chat_group_messages", recreate="always") as batch:
        batch.drop_column("task_id")
        batch.create_foreign_key(
            "fk_chat_group_messages_bot_id_bots",
            "bots",
            ["bot_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Downgrade schema."""
    # 一条 Bot 可能开过很多工：回退时挑它最早的一条 task 作为「旧成员身份」
    op.add_column("chat_group_messages", sa.Column("task_id", sa.Uuid(), nullable=True))
    op.execute(
        "UPDATE chat_group_messages SET task_id = ("
        "SELECT tasks.id FROM tasks WHERE tasks.bot_id = chat_group_messages.bot_id "
        "ORDER BY tasks.created_at, tasks.id LIMIT 1)"
    )
    with op.batch_alter_table("chat_group_messages", recreate="always") as batch:
        batch.drop_column("bot_id")
        batch.create_foreign_key(
            "fk_chat_group_messages_task_id_tasks",
            "tasks",
            ["task_id"],
            ["id"],
            ondelete="SET NULL",
        )

    for table, old_unique, old_index, new_unique, new_index in _MEMBER_TABLES:
        op.add_column(table, sa.Column("task_id", sa.Uuid(), nullable=True))
        op.execute(
            f"UPDATE {table} SET task_id = ("  # noqa: S608 — 常量表名
            f"SELECT tasks.id FROM tasks WHERE tasks.bot_id = {table}.bot_id "
            "ORDER BY tasks.created_at, tasks.id LIMIT 1)"
        )
        op.execute(f"DELETE FROM {table} WHERE task_id IS NULL")
        op.execute(
            f"DELETE FROM {table} WHERE CAST(id AS TEXT) NOT IN "  # noqa: S608 — 常量表名
            f"(SELECT MIN(CAST(id AS TEXT)) FROM {table} GROUP BY group_id, task_id)"
        )
        with op.batch_alter_table(table, recreate="always") as batch:
            batch.drop_index(new_index)
            batch.drop_constraint(new_unique, type_="unique")
            batch.drop_column("bot_id")
            batch.alter_column("task_id", existing_type=sa.Uuid(), nullable=False)
            batch.create_foreign_key(
                f"fk_{table}_task_id_tasks", "tasks", ["task_id"], ["id"], ondelete="CASCADE"
            )
            batch.create_unique_constraint(old_unique, ["group_id", "task_id"])
            batch.create_index(old_index, ["task_id"])
