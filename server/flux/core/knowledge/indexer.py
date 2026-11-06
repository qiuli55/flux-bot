"""增量索引器：把对话、docs、项目记忆写进知识库。

增量口径「宁可多切一次，不可漏更新」：

- 对话：按 (消息数, 最大 seq) 判断；消息没变就完全跳过，变了整段重切。
- 文档：按 (mtime_ns, size) 判断；文件变了整份重切。
- 项目记忆：按条目 id + updated_at 的哈希判断。

所有正文在入库前统一脱敏（`flux.core.context.redact`），避免聊天里贴过的密钥
被索引进知识库后又被检索出来。
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.core.context.redact import redact
from flux.core.knowledge.chunker import (
    ConversationMessage,
    chunk_conversation,
    chunk_markdown,
)
from flux.core.knowledge.embedding import EmbeddingClient
from flux.core.knowledge.repository import KnowledgeRepository, PreparedChunk
from flux.core.knowledge.text import estimate_tokens
from flux.core.project_brain.repository import ProjectBrainRepository
from flux.logging import get_logger
from flux.models.project import ProjectMemory
from flux.models.task import Task, TaskMessage

logger = get_logger(__name__)

CONVERSATION = "conversation"
DOC = "doc"
BRAIN = "brain"

# 单次索引批量：逐份嵌入，避免一份巨型对话把请求体撑爆。
_MAX_TITLE_CHARS = 160


class KnowledgeIndexer:
    """三类来源的增量索引入口。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        repository: KnowledgeRepository,
        embedder: EmbeddingClient,
        brain_repo: ProjectBrainRepository,
        docs_path: Path,
        docs_max_bytes: int,
        docs_max_files: int,
        chunk_tokens: int,
        chunk_overlap_tokens: int,
        conversation_window_tokens: int,
        max_messages_per_conversation: int,
    ) -> None:
        self._session_factory = session_factory
        self._repo = repository
        self._embedder = embedder
        self._brain = brain_repo
        self._docs_path = docs_path
        self._docs_max_bytes = max(1, docs_max_bytes)
        self._docs_max_files = max(1, docs_max_files)
        self._chunk_tokens = max(80, chunk_tokens)
        self._chunk_overlap = max(0, min(chunk_overlap_tokens, self._chunk_tokens // 2))
        self._window_tokens = max(80, conversation_window_tokens)
        self._max_messages = max(1, max_messages_per_conversation)

    async def index_all(self) -> dict[str, int]:
        """跑一轮全量增量索引，返回各类来源本次更新的份数。"""
        result: dict[str, int] = {}
        for name, handler in (
            (CONVERSATION, self.index_conversations),
            (DOC, self.index_docs),
            (BRAIN, self.index_brain),
        ):
            try:
                result[name] = await handler()
            except Exception as exc:  # noqa: BLE001 - 单类失败不能拖垮整轮
                logger.warning("知识库索引 %s 失败：%s", name, exc)
                result[name] = 0
        return result

    # --- 对话 ---

    async def index_conversations(self) -> int:
        existing = {
            document.source_id: document
            for document in await self._repo.list_documents(CONVERSATION)
        }
        stats = await self._conversation_stats()
        seen: set[str] = set()
        changed = 0
        for task_id, message_count, last_seq in stats:
            key = str(task_id)
            seen.add(key)
            document = existing.get(key)
            if (
                document is not None
                and document.status == "ready"
                and document.cursor("message_count") == message_count
                and document.cursor("last_seq") == last_seq
            ):
                continue
            try:
                if await self._index_conversation(task_id, message_count, last_seq):
                    changed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("对话 %s 索引失败：%s", key, exc)
                await self._repo.mark_error(source_type=CONVERSATION, source_id=key, error=str(exc))
        await self._repo.delete_orphans(CONVERSATION, seen)
        return changed

    async def _conversation_stats(self) -> list[tuple[uuid.UUID, int, int]]:
        statement = select(
            TaskMessage.task_id,
            func.count(TaskMessage.id),
            func.max(TaskMessage.seq),
        ).group_by(TaskMessage.task_id)
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        return [(row[0], int(row[1] or 0), int(row[2] or 0)) for row in rows]

    async def _index_conversation(
        self, task_id: uuid.UUID, message_count: int, last_seq: int
    ) -> bool:
        async with self._session_factory() as session:
            task = await session.get(Task, task_id)
            messages = list(
                await session.scalars(
                    select(TaskMessage)
                    .where(TaskMessage.task_id == task_id)
                    .order_by(TaskMessage.seq.desc())
                    .limit(self._max_messages)
                )
            )
        messages.reverse()
        if not messages:
            return False

        title = _clip(_one_line(task.description if task is not None else ""), _MAX_TITLE_CHARS)
        converted: list[ConversationMessage] = []
        for message in messages:
            content = redact(message.content or "").text.strip()
            if not content:
                continue
            converted.append(
                ConversationMessage(
                    seq=message.seq,
                    role=message.role,
                    content=content,
                    occurred_at=message.created_at,
                )
            )
        if not converted:
            return False

        chunked = chunk_conversation(
            converted,
            max_tokens=self._window_tokens,
            title=title or None,
        )
        occurred = {message.seq: message.occurred_at for message in converted}
        task_uuid = task.id if task is not None else None
        project_id = task.project_id if task is not None else None

        prepared: list[PreparedChunk] = []
        for chunk in chunked:
            last = chunk.meta.get("last_seq")
            prepared.append(
                PreparedChunk(
                    seq=chunk.seq,
                    title=chunk.title,
                    content=chunk.content,
                    content_hash=_sha256(chunk.content),
                    token_count=chunk.token_count,
                    char_count=len(chunk.content),
                    task_id=task_uuid,
                    occurred_at=occurred.get(last),
                    meta=dict(chunk.meta),
                )
            )
        await self._attach_embeddings(prepared)
        await self._repo.replace_source(
            source_type=CONVERSATION,
            source_id=str(task_id),
            content_hash=f"conversation:{message_count}:{last_seq}",
            chunks=prepared,
            project_id=project_id,
            title=title or None,
            meta={"message_count": message_count, "last_seq": last_seq},
        )
        return True

    # --- 文档 ---

    async def index_docs(self) -> int:
        existing = {
            document.source_id: document for document in await self._repo.list_documents(DOC)
        }
        files, truncated = self._scan_docs()
        seen: set[str] = set()
        changed = 0
        for path in files:
            relative = path.relative_to(self._docs_path).as_posix()
            seen.add(relative)
            try:
                stat = path.stat()
            except OSError as exc:
                logger.warning("文档 %s 读取失败：%s", relative, exc)
                continue
            if stat.st_size > self._docs_max_bytes:
                logger.info("文档 %s 超过大小上限，跳过索引", relative)
                continue
            document = existing.get(relative)
            if (
                document is not None
                and document.status == "ready"
                and document.cursor("mtime_ns") == stat.st_mtime_ns
                and document.cursor("size") == stat.st_size
            ):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.warning("文档 %s 读取失败：%s", relative, exc)
                await self._repo.mark_error(source_type=DOC, source_id=relative, error=str(exc))
                continue
            try:
                if await self._index_doc(
                    relative=relative,
                    text=text,
                    mtime_ns=stat.st_mtime_ns,
                    size=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                ):
                    changed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("文档 %s 索引失败：%s", relative, exc)
                await self._repo.mark_error(source_type=DOC, source_id=relative, error=str(exc))
        if not truncated:
            await self._repo.delete_orphans(DOC, seen)
        return changed

    async def _index_doc(
        self,
        *,
        relative: str,
        text: str,
        mtime_ns: int,
        size: int,
        modified_at: datetime,
    ) -> bool:
        cleaned = redact(text).text
        chunked = chunk_markdown(
            cleaned,
            max_tokens=self._chunk_tokens,
            overlap_tokens=self._chunk_overlap,
        )
        if not chunked:
            await self._repo.delete_source(DOC, relative)
            return False
        prepared = [
            PreparedChunk(
                seq=chunk.seq,
                title=chunk.title,
                content=chunk.content,
                content_hash=_sha256(chunk.content),
                token_count=chunk.token_count or estimate_tokens(chunk.content),
                char_count=len(chunk.content),
                occurred_at=modified_at,
                meta=dict(chunk.meta),
            )
            for chunk in chunked
        ]
        await self._attach_embeddings(prepared)
        await self._repo.replace_source(
            source_type=DOC,
            source_id=relative,
            content_hash=_sha256(cleaned),
            chunks=prepared,
            title=chunked[0].title or Path(relative).name,
            meta={"mtime_ns": mtime_ns, "size": size, "path": relative},
        )
        return True

    def _scan_docs(self) -> tuple[list[Path], bool]:
        root = self._docs_path
        if not root.is_dir():
            return [], False
        files: list[Path] = []
        truncated = False
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
            for name in sorted(filenames):
                if not name.endswith(".md"):
                    continue
                if len(files) >= self._docs_max_files:
                    truncated = True
                    break
                files.append(Path(dirpath) / name)
            if truncated:
                break
        return files, truncated

    # --- 项目记忆 ---

    async def index_brain(self) -> int:
        existing = {
            document.source_id: document for document in await self._repo.list_documents(BRAIN)
        }
        projects = await self._brain.list_projects()
        seen: set[str] = set()
        changed = 0
        for project in projects:
            entries = await self._brain.entries(project.id)
            if not entries:
                continue
            key = str(project.id)
            seen.add(key)
            fingerprint = _sha256(
                "|".join(
                    f"{entry.id}:{entry.updated_at.isoformat() if entry.updated_at else ''}"
                    for entry in entries
                )
            )
            document = existing.get(key)
            if (
                document is not None
                and document.status == "ready"
                and document.content_hash == fingerprint
            ):
                continue
            try:
                if await self._index_brain_project(project.id, project.name, entries, fingerprint):
                    changed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("项目记忆 %s 索引失败：%s", key, exc)
                await self._repo.mark_error(source_type=BRAIN, source_id=key, error=str(exc))
        await self._repo.delete_orphans(BRAIN, seen)
        return changed

    async def _index_brain_project(
        self,
        project_id: uuid.UUID,
        name: str,
        entries: list[ProjectMemory],
        fingerprint: str,
    ) -> bool:
        lines = [f"# {name}", ""]
        latest: datetime | None = None
        for entry in entries:
            content = redact(entry.content or "").text.strip()
            if not content:
                continue
            lines.append(f"## {entry.type}")
            lines.append(content)
            lines.append("")
            if entry.updated_at is not None and (latest is None or entry.updated_at > latest):
                latest = entry.updated_at
        if len(lines) <= 2:
            return False
        chunked = chunk_markdown(
            "\n".join(lines),
            max_tokens=self._chunk_tokens,
            overlap_tokens=self._chunk_overlap,
        )
        prepared = [
            PreparedChunk(
                seq=chunk.seq,
                title=chunk.title or name,
                content=chunk.content,
                content_hash=_sha256(chunk.content),
                token_count=chunk.token_count or estimate_tokens(chunk.content),
                char_count=len(chunk.content),
                occurred_at=latest,
                meta=dict(chunk.meta),
            )
            for chunk in chunked
        ]
        if not prepared:
            return False
        await self._attach_embeddings(prepared)
        await self._repo.replace_source(
            source_type=BRAIN,
            source_id=str(project_id),
            content_hash=fingerprint,
            chunks=prepared,
            project_id=project_id,
            title=name,
            meta={"entry_count": len(entries)},
        )
        return True

    # --- 内部 ---

    async def _attach_embeddings(self, chunks: list[PreparedChunk]) -> None:
        """就地补 embedding；失败时保持 None，检索自动走词法。"""
        if not chunks or not self._embedder.enabled:
            return
        texts = [
            f"{chunk.title}\n{chunk.content}" if chunk.title else chunk.content for chunk in chunks
        ]
        vectors = await self._embedder.embed(texts)
        if len(vectors) != len(chunks):
            return
        model = self._embedder.model
        for index, vector in enumerate(vectors):
            chunks[index] = replace(chunks[index], embedding=vector, embedding_model=model)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]
