"""知识库索引与检索测试（真实 SQLite + 真实临时目录，不 mock 仓储）。

覆盖 P1 的闭环：docs / 对话 / 项目记忆增量入库 → 混合检索 → `assistant.search`。
测试配置显式关闭后台 worker，索引由用例自己触发，避免时序漂移。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from flux.config import Settings
from flux.container import Container
from flux.core.knowledge.indexer import BRAIN, CONVERSATION, DOC
from flux.core.mcp.auth import AgentIdentity
from flux.core.mcp.tools.base import ToolContext
from flux.core.mcp.tools.knowledge_tools import assistant_search
from flux.core.task_engine.repository import MessageDraft
from flux.enums import BrainSection, Capability
from flux.errors import ValidationError

_DOC = """# 部署手册

## 失败排查

生产环境 deploy 失败时先看 E500 日志，再回滚上一个版本。

## 回滚

执行 scripts/rollback.sh 并确认数据库迁移已回退。
"""


@pytest.fixture()
def knowledge(
    settings: Settings, db_schema: None, tmp_path: Path
) -> Iterator[tuple[Container, Path]]:
    """知识库开启、docs 指向临时目录的容器（embedding 未配置 → 纯词法）。"""
    docs = tmp_path / "knowledge-docs"
    docs.mkdir()
    container = Container(
        settings.model_copy(
            update={
                "knowledge_enabled": True,
                "knowledge_docs_dir": str(docs),
                "knowledge_embedding_api_key": None,
            }
        )
    )
    yield container, docs


def _write_doc(docs: Path, name: str, content: str) -> Path:
    path = docs / name
    path.write_text(content, encoding="utf-8")
    return path


def _create_conversation(container: Container, description: str = "部署失败排查") -> uuid.UUID:
    task_id = uuid.uuid4()
    asyncio.run(
        container.task_repo.create(
            task_id=task_id,
            description=description,
            priority=0,
            project_id=None,
            agent_id=None,
        )
    )
    asyncio.run(
        container.task_repo.add_messages(
            task_id,
            [
                MessageDraft(role="user", content="生产环境 deploy 失败，日志里是 E500"),
                MessageDraft(role="assistant", content="先回滚，再按 E500 查网关配置"),
            ],
        )
    )
    return task_id


# --- docs ---


def test_docs_are_indexed_incrementally_and_orphans_removed(
    knowledge: tuple[Container, Path],
) -> None:
    container, docs = knowledge
    path = _write_doc(docs, "deploy.md", _DOC)

    assert asyncio.run(container.knowledge_indexer.index_docs()) == 1
    documents = asyncio.run(container.knowledge_repo.list_documents(DOC))
    assert len(documents) == 1
    assert documents[0].source_id == "deploy.md"
    assert documents[0].status == "ready"
    assert documents[0].chunk_count >= 2

    # 文件没变：第二轮完全跳过
    assert asyncio.run(container.knowledge_indexer.index_docs()) == 0

    # 文件变了：整份重切
    path.write_text(_DOC + "\n## 新增\n\n补充一条演练记录。\n", encoding="utf-8")
    assert asyncio.run(container.knowledge_indexer.index_docs()) == 1

    # 文件删除：孤儿来源一并清理
    path.unlink()
    assert asyncio.run(container.knowledge_indexer.index_docs()) == 0
    assert asyncio.run(container.knowledge_repo.list_documents(DOC)) == []


def test_search_finds_docs_by_keyword_and_respects_filters(
    knowledge: tuple[Container, Path],
) -> None:
    container, docs = knowledge
    _write_doc(docs, "deploy.md", _DOC)
    asyncio.run(container.knowledge_indexer.index_docs())

    hits = asyncio.run(container.knowledge.search("E500"))

    assert hits
    assert hits[0].chunk.source_type == DOC
    assert "E500" in hits[0].snippet
    assert hits[0].score > 0

    # 中文短语走 bigram 召回
    assert asyncio.run(container.knowledge.search("部署失败"))

    assert asyncio.run(container.knowledge.search("E500", source_type=CONVERSATION)) == []
    assert asyncio.run(container.knowledge.search("完全无关的词汇xyz")) == []


# --- 对话 ---


def test_conversations_are_indexed_incrementally(knowledge: tuple[Container, Path]) -> None:
    container, _ = knowledge
    task_id = _create_conversation(container)

    assert asyncio.run(container.knowledge_indexer.index_conversations()) == 1
    documents = asyncio.run(container.knowledge_repo.list_documents(CONVERSATION))
    assert [document.source_id for document in documents] == [str(task_id)]
    assert documents[0].title == "部署失败排查"

    assert asyncio.run(container.knowledge_indexer.index_conversations()) == 0

    asyncio.run(
        container.task_repo.add_messages(
            task_id, [MessageDraft(role="user", content="回滚完成，E500 已消失")]
        )
    )
    assert asyncio.run(container.knowledge_indexer.index_conversations()) == 1

    hits = asyncio.run(container.knowledge.search("E500", source_type=CONVERSATION))
    assert hits
    assert hits[0].chunk.task_id == task_id


# --- 项目记忆 ---


def test_brain_entries_are_indexed_and_searchable(knowledge: tuple[Container, Path]) -> None:
    container, _ = knowledge
    project = asyncio.run(container.brain.create_project(name="Flux"))
    asyncio.run(
        container.brain.write(
            str(project.id),
            section=BrainSection.DECISIONS,
            content="选 FastAPI 而不是 Flask，测试必须走真实数据库",
        )
    )

    assert asyncio.run(container.knowledge_indexer.index_brain()) == 1
    assert asyncio.run(container.knowledge_indexer.index_brain()) == 0

    hits = asyncio.run(container.knowledge.search("FastAPI", source_type=BRAIN))
    assert hits
    assert hits[0].chunk.project_id == project.id


def test_index_all_reports_each_source(knowledge: tuple[Container, Path]) -> None:
    container, docs = knowledge
    _write_doc(docs, "deploy.md", _DOC)
    _create_conversation(container)
    project = asyncio.run(container.brain.create_project(name="Flux"))
    asyncio.run(
        container.brain.write(
            str(project.id), section=BrainSection.OVERVIEW, content="Flux 是个人 AI 工作台"
        )
    )

    result = asyncio.run(container.knowledge_indexer.index_all())

    assert result == {CONVERSATION: 1, DOC: 1, BRAIN: 1}


# --- MCP 工具 ---


def _tool_ctx(container: Container) -> ToolContext:
    return ToolContext(
        container=container,
        identity=AgentIdentity(
            agent_id=str(uuid.uuid4()),
            token_id="test-token",
            scopes=frozenset({Capability.FILE_READ}),
        ),
        tool_name="assistant.search",
    )


def test_assistant_search_returns_sources_and_metadata(
    knowledge: tuple[Container, Path],
) -> None:
    container, docs = knowledge
    _write_doc(docs, "deploy.md", _DOC)
    asyncio.run(container.knowledge_indexer.index_docs())

    result = asyncio.run(assistant_search(_tool_ctx(container), {"query": "E500"}))

    assert result["enabled"] is True
    assert result["count"] >= 1
    first = result["results"][0]
    assert first["source_type"] == DOC
    assert first["source_id"] == "deploy.md"
    assert "E500" in first["snippet"]
    assert first["task_id"] is None


def test_assistant_search_validates_params(knowledge: tuple[Container, Path]) -> None:
    container, _ = knowledge
    ctx = _tool_ctx(container)

    with pytest.raises(ValidationError):
        asyncio.run(assistant_search(ctx, {"query": "x", "limit": "8"}))
    with pytest.raises(ValidationError):
        asyncio.run(assistant_search(ctx, {"query": "x", "source": "wiki"}))
    with pytest.raises(ValidationError):
        asyncio.run(assistant_search(ctx, {"query": "x", "task_id": "not-a-uuid"}))
    with pytest.raises(ValidationError):
        asyncio.run(assistant_search(ctx, {"query": "x", "since": "昨天"}))
    with pytest.raises(ValidationError):
        asyncio.run(assistant_search(ctx, {"query": "x", "unknown": 1}))


def test_assistant_search_is_disabled_when_knowledge_off(container: Container) -> None:
    result = asyncio.run(assistant_search(_tool_ctx(container), {"query": "任何问题"}))

    assert result == {"query": "任何问题", "results": [], "count": 0, "enabled": False}
