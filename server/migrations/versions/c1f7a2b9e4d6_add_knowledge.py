"""知识库（RAG）：documents / chunks 两张表

助理需要「掌握全部项目进度」，本质是把散落在对话、仓库文档、项目记忆里的原文
切成可检索的片段。这里只落索引层，不复制业务真相：

- `knowledge_documents`：一份来源（对话 / 文档 / 项目记忆）的索引元数据，
  `(source_type, source_id)` 唯一；`content_hash` + `metadata` 里的游标决定增量时是否重建。
- `knowledge_chunks`：检索与回跳的最小单位，带来源 id / 任务 id / 发生时间；
  `(document_id, seq)` 唯一，重建时按来源整段替换。

embedding 暂用 JSON 列表存储（SQLite 测试库与 PostgreSQL 都能跑），
P2 迁移到 pgvector 向量列 + HNSW 时只动这两张表，不破坏检索层契约。

Revision ID: c1f7a2b9e4d6
Revises: b6d2e9a4c817
Create Date: 2026-11-03 16:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1f7a2b9e4d6"
down_revision: str | Sequence[str] | None = "b6d2e9a4c817"
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
        "knowledge_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=512), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="ready"),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_type", "source_id", name="uq_knowledge_documents_source"),
    )
    op.create_index(
        op.f("ix_knowledge_documents_source_type"), "knowledge_documents", ["source_type"]
    )
    op.create_index(
        op.f("ix_knowledge_documents_project_id"), "knowledge_documents", ["project_id"]
    )
    op.create_index(
        "ix_knowledge_documents_type_indexed",
        "knowledge_documents",
        ["source_type", "indexed_at"],
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=512), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("embedding", sa.JSON(), nullable=True),
        sa.Column("embedding_model", sa.String(length=128), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "seq", name="uq_knowledge_chunks_document_seq"),
    )
    op.create_index(op.f("ix_knowledge_chunks_document_id"), "knowledge_chunks", ["document_id"])
    op.create_index(op.f("ix_knowledge_chunks_source_type"), "knowledge_chunks", ["source_type"])
    op.create_index(op.f("ix_knowledge_chunks_occurred_at"), "knowledge_chunks", ["occurred_at"])
    op.create_index(
        "ix_knowledge_chunks_type_time", "knowledge_chunks", ["source_type", "occurred_at"]
    )
    op.create_index(
        "ix_knowledge_chunks_project_time", "knowledge_chunks", ["project_id", "occurred_at"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_knowledge_chunks_project_time", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_type_time", table_name="knowledge_chunks")
    op.drop_index(op.f("ix_knowledge_chunks_occurred_at"), table_name="knowledge_chunks")
    op.drop_index(op.f("ix_knowledge_chunks_source_type"), table_name="knowledge_chunks")
    op.drop_index(op.f("ix_knowledge_chunks_document_id"), table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")

    op.drop_index("ix_knowledge_documents_type_indexed", table_name="knowledge_documents")
    op.drop_index(op.f("ix_knowledge_documents_project_id"), table_name="knowledge_documents")
    op.drop_index(op.f("ix_knowledge_documents_source_type"), table_name="knowledge_documents")
    op.drop_table("knowledge_documents")
