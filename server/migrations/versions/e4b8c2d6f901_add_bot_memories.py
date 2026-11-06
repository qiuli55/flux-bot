"""Bot 分层长期记忆

- bot_memories：摘要层。同一 Bot + 同一相关性 + 同一来源群 + 同一 topic
  合并成一条，每次合并 `version + 1`；注入上下文只读这一层。
- bot_memory_chunks：全文层。每条原始发言/消息只追加，需要追溯原话时取这里。

相关性只有 direct / indirect 两档：与自己无关的内容不落库。
出群后的 indirect 记忆按关键词低权重召回，`weight` 记录该权重。

Revision ID: e4b8c2d6f901
Revises: d3e7c1b94a60
Create Date: 2026-11-05 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4b8c2d6f901"
down_revision: str | Sequence[str] | None = "d3e7c1b94a60"
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
        "bot_memories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="summary"),
        sa.Column("relevance", sa.String(length=16), nullable=False, server_default="direct"),
        sa.Column("topic", sa.String(length=128), nullable=True),
        sa.Column("self_authored", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="group"),
        sa.Column("source_group_id", sa.Uuid(), nullable=True),
        sa.Column("source_group_name", sa.String(length=128), nullable=True),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_group_id"], ["chat_groups.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_bot_memories_bot_id"), "bot_memories", ["bot_id"])
    op.create_index(op.f("ix_bot_memories_relevance"), "bot_memories", ["relevance"])
    op.create_index(op.f("ix_bot_memories_source_group_id"), "bot_memories", ["source_group_id"])
    op.create_index("ix_bot_memories_bot_relevance", "bot_memories", ["bot_id", "relevance"])
    op.create_index("ix_bot_memories_bot_group", "bot_memories", ["bot_id", "source_group_id"])

    op.create_table(
        "bot_memory_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="group"),
        sa.Column("source_group_id", sa.Uuid(), nullable=True),
        sa.Column("source_message_id", sa.Uuid(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["memory_id"], ["bot_memories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_group_id"], ["chat_groups.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bot_memory_chunks_memory", "bot_memory_chunks", ["memory_id"])
    op.create_index("ix_bot_memory_chunks_bot", "bot_memory_chunks", ["bot_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_bot_memory_chunks_bot", table_name="bot_memory_chunks")
    op.drop_index("ix_bot_memory_chunks_memory", table_name="bot_memory_chunks")
    op.drop_table("bot_memory_chunks")
    op.drop_index("ix_bot_memories_bot_group", table_name="bot_memories")
    op.drop_index("ix_bot_memories_bot_relevance", table_name="bot_memories")
    op.drop_index(op.f("ix_bot_memories_source_group_id"), table_name="bot_memories")
    op.drop_index(op.f("ix_bot_memories_relevance"), table_name="bot_memories")
    op.drop_index(op.f("ix_bot_memories_bot_id"), table_name="bot_memories")
    op.drop_table("bot_memories")
