"""知识库（RAG）模型：助理检索全部对话 / 文档 / 项目记忆的索引层。

两层结构：

- `knowledge_documents`：一个「来源」的索引元数据（source_type + source_id 唯一），
  记录内容哈希与索引游标，增量索引靠它判断「这份来源有没有变」。
- `knowledge_chunks`：检索的最小单位。对话按「会话 × 时间窗」切、文档按标题层级切，
  每块保留来源 id / 时间 / 任务 id，命中后能直接回跳到原始对话或文件。

embedding 在 P1 以 JSON 列表存储（SQLite 测试库与 PostgreSQL 都能跑），
P2 换成 pgvector 向量列 + HNSW 索引时只改本表，不影响检索层契约。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class KnowledgeDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一个可索引来源的元数据（对话 / 文档 / 项目记忆）。"""

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        UniqueConstraint("source_type", "source_id", name="uq_knowledge_documents_source"),
        Index("ix_knowledge_documents_type_indexed", "source_type", "indexed_at"),
    )

    # 取值见 flux.core.knowledge.indexer.KnowledgeSource：conversation / doc / brain
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 稳定标识：对话 = task_id，文档 = 相对路径，项目记忆 = project_id
    source_id: Mapped[str] = mapped_column(String(512), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 整份来源的 sha256；对话用「最后一条消息 seq + 消息数」算出的稳定指纹
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # ready / indexing / error
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ready", server_default="ready"
    )
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 增量游标：对话 {last_seq, message_count}；文档 {mtime_ns, size}；记忆 {updated_at}
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "source_type": self.source_type,
            "source_id": self.source_id,
            "project_id": str(self.project_id) if self.project_id is not None else None,
            "title": self.title,
            "content_hash": self.content_hash,
            "status": self.status,
            "chunk_count": self.chunk_count,
            "char_count": self.char_count,
            "indexed_at": self.indexed_at.isoformat() if self.indexed_at is not None else None,
            "error": self.error,
            "metadata": self.meta or {},
        }

    def cursor(self, key: str, default: Any = None) -> Any:
        """读增量游标（meta 里的一个键）；缺失时返回 default。"""
        return (self.meta or {}).get(key, default)


class KnowledgeChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """检索的最小单位：一块原文 + 来源定位 + 可选向量。"""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "seq", name="uq_knowledge_chunks_document_seq"),
        Index("ix_knowledge_chunks_type_time", "source_type", "occurred_at"),
        Index("ix_knowledge_chunks_project_time", "project_id", "occurred_at"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(512), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # 对话块回跳用：命中后 assistant.conversation(task_id) 直接展开原文
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # 文档内序号（同一来源从 0 递增），配合 document_id 唯一
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 估算 token 数（按字符量折算），用于预算裁剪与排序时的长度归一
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # 内容发生时间：对话 = 窗口最后一条消息时间；文档 = 文件 mtime；记忆 = updated_at
    occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # P1 用 JSON 存向量；P2 迁移到 pgvector 的 vector 列
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 切块附加信息：对话 {first_seq,last_seq,roles}；文档 {heading_path,path}
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "source_type": self.source_type,
            "source_id": self.source_id,
            "project_id": str(self.project_id) if self.project_id is not None else None,
            "task_id": str(self.task_id) if self.task_id is not None else None,
            "seq": self.seq,
            "title": self.title,
            "content": self.content,
            "char_count": self.char_count,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at is not None else None,
            "metadata": self.meta or {},
        }
