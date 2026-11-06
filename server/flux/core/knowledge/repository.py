"""知识库持久化：来源元数据 + 分块的读写与关键词预筛。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.logging import get_logger
from flux.models.knowledge import KnowledgeChunk, KnowledgeDocument

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PreparedChunk:
    """索引器产出的待写入分块（已切好、已脱敏、可选已向量化）。"""

    seq: int
    content: str
    content_hash: str
    token_count: int
    char_count: int
    title: str | None = None
    task_id: uuid.UUID | None = None
    occurred_at: datetime | None = None
    embedding: list[float] | None = None
    embedding_model: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class KnowledgeRepository:
    """knowledge_documents / knowledge_chunks 的读写入口。每次调用自开会话。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # --- 来源 ---

    async def get_document(self, source_type: str, source_id: str) -> KnowledgeDocument | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_type == source_type,
                    KnowledgeDocument.source_id == source_id,
                )
            )

    async def list_documents(self, source_type: str | None = None) -> list[KnowledgeDocument]:
        statement = select(KnowledgeDocument).order_by(KnowledgeDocument.source_type)
        if source_type is not None:
            statement = statement.where(KnowledgeDocument.source_type == source_type)
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def delete_source(self, source_type: str, source_id: str) -> bool:
        async with self._session_factory() as session:
            document = await session.scalar(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_type == source_type,
                    KnowledgeDocument.source_id == source_id,
                )
            )
            if document is None:
                return False
            await session.delete(document)
            await session.commit()
            return True

    async def delete_orphans(self, source_type: str, keep: set[str]) -> int:
        """删掉某类来源里已不在 keep 中的文档（文件删除 / 会话清理）。"""
        async with self._session_factory() as session:
            statement = select(KnowledgeDocument).where(
                KnowledgeDocument.source_type == source_type
            )
            if keep:
                statement = statement.where(KnowledgeDocument.source_id.notin_(keep))
            documents = list(await session.scalars(statement))
            for document in documents:
                await session.delete(document)
            if documents:
                await session.commit()
            return len(documents)

    async def mark_error(
        self, *, source_type: str, source_id: str, error: str, project_id: uuid.UUID | None = None
    ) -> None:
        """记录来源读取/索引失败；旧分块一并清掉，避免继续命中过期内容。"""
        now = _utcnow()
        async with self._session_factory() as session:
            document = await session.scalar(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_type == source_type,
                    KnowledgeDocument.source_id == source_id,
                )
            )
            if document is None:
                document = KnowledgeDocument(
                    id=uuid.uuid4(),
                    source_type=source_type,
                    source_id=source_id,
                    project_id=project_id,
                    content_hash="",
                    meta={},
                )
                session.add(document)
                await session.flush()
            document.status = "error"
            document.error = error[:2000]
            document.chunk_count = 0
            document.char_count = 0
            document.updated_at = now
            await session.execute(
                delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
            )
            await session.commit()

    async def replace_source(
        self,
        *,
        source_type: str,
        source_id: str,
        content_hash: str,
        chunks: list[PreparedChunk],
        project_id: uuid.UUID | None = None,
        title: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> KnowledgeDocument:
        """整份来源替换：同事务内更新元数据并重建全部分块。"""
        now = _utcnow()
        async with self._session_factory() as session:
            document = await session.scalar(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_type == source_type,
                    KnowledgeDocument.source_id == source_id,
                )
            )
            if document is None:
                document = KnowledgeDocument(
                    id=uuid.uuid4(),
                    source_type=source_type,
                    source_id=source_id,
                    content_hash=content_hash,
                    meta={},
                )
                session.add(document)
                await session.flush()
            document.project_id = project_id
            document.title = title
            document.content_hash = content_hash
            document.status = "ready"
            document.error = None
            document.chunk_count = len(chunks)
            document.char_count = sum(chunk.char_count for chunk in chunks)
            document.indexed_at = now
            document.meta = meta or {}
            document.updated_at = now

            await session.execute(
                delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
            )
            for chunk in chunks:
                session.add(
                    KnowledgeChunk(
                        id=uuid.uuid4(),
                        document_id=document.id,
                        source_type=source_type,
                        source_id=source_id,
                        project_id=project_id,
                        task_id=chunk.task_id,
                        seq=chunk.seq,
                        title=chunk.title,
                        content=chunk.content,
                        content_hash=chunk.content_hash,
                        token_count=chunk.token_count,
                        char_count=chunk.char_count,
                        occurred_at=chunk.occurred_at,
                        embedding=chunk.embedding,
                        embedding_model=chunk.embedding_model,
                        meta=chunk.meta,
                        created_at=now,
                        updated_at=now,
                    )
                )
            await session.commit()
            await session.refresh(document)
            return document

    # --- 检索 ---

    async def search_candidates(
        self,
        terms: list[str],
        *,
        limit: int,
        source_type: str | None = None,
        project_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[KnowledgeChunk]:
        """关键词预筛：只召回正文/标题命中任一词元的分块，再由检索层精排。

        取最近的分块而不是全表扫描：知识库会持续增长，候选集必须有界。
        词元按原样做 LIKE；调用方负责去重并限制数量。
        """
        if not terms:
            return []
        conditions = [KnowledgeChunk.content.ilike(f"%{term}%") for term in terms]
        conditions.extend(KnowledgeChunk.title.ilike(f"%{term}%") for term in terms)
        statement = select(KnowledgeChunk).where(or_(*conditions))
        if source_type is not None:
            statement = statement.where(KnowledgeChunk.source_type == source_type)
        if project_id is not None:
            statement = statement.where(KnowledgeChunk.project_id == project_id)
        if task_id is not None:
            statement = statement.where(KnowledgeChunk.task_id == task_id)
        if since is not None:
            statement = statement.where(KnowledgeChunk.occurred_at >= since)
        if until is not None:
            statement = statement.where(KnowledgeChunk.occurred_at <= until)
        statement = statement.order_by(
            KnowledgeChunk.occurred_at.is_(None),
            KnowledgeChunk.occurred_at.desc(),
            KnowledgeChunk.seq.desc(),
        ).limit(limit)
        async with self._session_factory() as session:
            return list(await session.scalars(statement))
