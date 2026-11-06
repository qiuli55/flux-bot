"""助理检索工具：在知识库里跨对话 / 文档 / 项目记忆做混合检索。

和 `assistant.conversations` 的区别：那些工具是"按时间浏览"，本工具是"按问题找证据"。
检索是只读的，能力按 `file.read` 计费。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from flux.core.mcp.tools.base import (
    ToolContext,
    ToolSpec,
    optional_str,
    reject_unknown,
    require_str,
)
from flux.enums import Capability
from flux.errors import ValidationError

__all__ = ["knowledge_tools"]

_SOURCES = ("conversation", "doc", "brain")


async def assistant_search(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"query", "limit", "source", "project_id", "task_id", "since", "until"})
    query = require_str(params, "query")
    container = ctx.container
    if not container.knowledge_enabled:
        return {"query": query, "results": [], "count": 0, "enabled": False}
    limit = _limit(params)
    source = _source(params)
    hits = await container.knowledge.search(
        query,
        limit=limit,
        source_type=source,
        project_id=_optional_uuid(optional_str(params, "project_id"), "project_id"),
        task_id=_optional_uuid(optional_str(params, "task_id"), "task_id"),
        since=_optional_time(optional_str(params, "since"), "since"),
        until=_optional_time(optional_str(params, "until"), "until"),
    )
    return {
        "query": query,
        "results": [
            {
                "score": hit.score,
                "source_type": hit.chunk.source_type,
                "source_id": hit.chunk.source_id,
                "title": hit.chunk.title,
                "project_id": (
                    str(hit.chunk.project_id) if hit.chunk.project_id is not None else None
                ),
                "task_id": str(hit.chunk.task_id) if hit.chunk.task_id is not None else None,
                "occurred_at": (
                    hit.chunk.occurred_at.isoformat() if hit.chunk.occurred_at is not None else None
                ),
                "snippet": hit.snippet,
                "metadata": hit.chunk.meta or {},
            }
            for hit in hits
        ],
        "count": len(hits),
        "enabled": True,
    }


def _limit(params: dict[str, Any]) -> int:
    value = params.get("limit", 8)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("limit 必须是整数", details={"param": "limit"})
    return max(1, min(value, 50))


def _source(params: dict[str, Any]) -> str | None:
    value = optional_str(params, "source")
    if value is None:
        return None
    if value not in _SOURCES:
        raise ValidationError(
            f"source 取值不合法：{value}；可选值：{'、'.join(_SOURCES)}",
            details={"param": "source", "allowed": list(_SOURCES)},
        )
    return value


def _optional_uuid(value: str | None, key: str) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ValidationError(f"{key} 不是合法 UUID：{value}", details={"param": key}) from exc


def _optional_time(value: str | None, key: str) -> datetime | None:
    if value is None:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(
            f"{key} 必须是 ISO 8601 时间：{value}", details={"param": key}
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def knowledge_tools() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name="assistant.search",
            title="检索知识库",
            description=(
                "在助理知识库里做混合检索（只读）：覆盖全部历史对话、仓库 docs 与项目记忆。"
                "适合回答“之前那个问题是怎么解决的 / 这个模块的约定是什么 / 哪个对话提过它”。"
                "结果带来源与时间，需要原文时用 assistant.conversation 按 task_id 展开。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索问题或关键词"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "返回条数上限（默认 8）",
                    },
                    "source": {
                        "type": "string",
                        "enum": list(_SOURCES),
                        "description": "限定来源类型（可选）：conversation / doc / brain",
                    },
                    "project_id": {"type": "string", "description": "限定项目 UUID（可选）"},
                    "task_id": {"type": "string", "description": "限定对话 UUID（可选）"},
                    "since": {
                        "type": "string",
                        "description": "只取该时间之后的内容（ISO 8601，可选）",
                    },
                    "until": {
                        "type": "string",
                        "description": "只取该时间之前的内容（ISO 8601，可选）",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            capability=Capability.FILE_READ,
            handler=assistant_search,
        ),
    )
