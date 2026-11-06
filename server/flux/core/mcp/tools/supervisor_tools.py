"""助理（Supervisor）只读工具：让子 Agent 能问"现在整体在做什么"。

六个工具全是只读，能力统一按 `file.read` 计费：它们只回放平台已经记录的事实
（事件、工单、报告、对话），不接受任何"帮我派活/改状态"的入参——派发与停止仍然只有
人通过 REST 接口才能触发（观察模式的红线：模型不越过授权面）。

其中 `assistant.conversations` / `assistant.conversation` 是助理通读全部对话的入口：
列表一次取齐（消息条数 + 最后一条摘录），再按需要展开某一段对话的消息。
"""

from __future__ import annotations

import uuid
from typing import Any

from flux.core.mcp.tools.base import ToolContext, ToolSpec, optional_str, reject_unknown
from flux.enums import Capability, TaskOrigin, TaskStatus
from flux.errors import ValidationError

__all__ = ["supervisor_tools"]

#: description / 最后一条消息的摘录长度：够判断"这条对话在聊什么"，又不至于把
#: 整个列表撑成几万字——助理要细节时再展开 assistant.conversation 看全文。
_CLIP = 200

_LIMIT_SCHEMA: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "maximum": 200,
    "description": "返回条数上限（默认 20）",
}


async def assistant_overview(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"limit"})
    return await ctx.container.supervisor.overview(limit=_limit(params))


async def assistant_ticket(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"ticket_id"})
    ticket_id = _uuid(params.get("ticket_id"), "ticket_id")
    return await ctx.container.supervisor.ticket_detail(ticket_id)


async def assistant_reports(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"limit"})
    return {"reports": await ctx.container.supervisor.reports(limit=_limit(params))}


async def assistant_timeline(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"thread_id", "project_id", "limit"})
    thread_id = _optional_uuid(optional_str(params, "thread_id"), "thread_id")
    project_id = _optional_uuid(optional_str(params, "project_id"), "project_id")
    events = await ctx.container.supervisor.timeline(
        thread_id=thread_id, project_id=project_id, limit=_limit(params)
    )
    return {"events": events}


async def assistant_conversations(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"limit", "project_id", "status", "keyword", "include_assistant"})
    summaries = await ctx.container.task_repo.list_conversations(
        project_id=_optional_uuid(optional_str(params, "project_id"), "project_id"),
        status=_status(params.get("status")),
        origin=None if _bool(params, "include_assistant") else TaskOrigin.CHAT,
        keyword=optional_str(params, "keyword"),
        limit=_limit(params),
    )
    return {
        "conversations": [
            {
                "id": str(item.task.id),
                "description": _clip(item.task.description),
                "status": item.task.status,
                "origin": item.task.origin or "chat",
                "agent_id": (str(item.task.agent_id) if item.task.agent_id is not None else None),
                "project_id": (
                    str(item.task.project_id) if item.task.project_id is not None else None
                ),
                "model": item.task.model,
                "created_at": _iso(item.task.created_at),
                "message_count": item.message_count,
                "last_message_at": _iso(item.last_message_at),
                "last_message": _clip(item.last_message),
            }
            for item in summaries
        ],
        "count": len(summaries),
    }


async def assistant_conversation(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"task_id", "limit", "before"})
    task_id = _uuid(params.get("task_id"), "task_id")
    task = await ctx.container.task_repo.get(task_id)
    messages, has_more = await ctx.container.task_repo.list_messages(
        task_id,
        limit=_limit(params),
        before=_optional_int(params.get("before"), "before"),
    )
    return {
        "task": task.to_dict(),
        "messages": [message.to_dict() for message in messages],
        "has_more": has_more,
        "count": len(messages),
    }


def _limit(params: dict[str, Any]) -> int:
    value = params.get("limit", 20)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("limit 必须是整数", details={"param": "limit"})
    return max(1, min(value, 200))


def _bool(params: dict[str, Any], key: str, *, default: bool = False) -> bool:
    value = params.get(key, default)
    if not isinstance(value, bool):
        raise ValidationError(f"参数 {key} 必须是布尔值", details={"param": key})
    return value


def _optional_int(value: object, key: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"参数 {key} 必须是正整数", details={"param": key})
    return value


def _status(value: object) -> TaskStatus | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("参数 status 必须是字符串", details={"param": "status"})
    try:
        return TaskStatus(value.strip())
    except ValueError as exc:
        allowed = [item.value for item in TaskStatus]
        raise ValidationError(
            f"status 取值不合法：{value}；可选值：{'、'.join(allowed)}",
            details={"param": "status", "allowed": allowed},
        ) from exc


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    if len(text) <= _CLIP:
        return text
    return text[:_CLIP].rstrip() + "…"


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _uuid(value: object, key: str) -> uuid.UUID:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"参数 {key} 必须是非空字符串", details={"param": key})
    try:
        return uuid.UUID(value.strip())
    except ValueError as exc:
        raise ValidationError(f"{key} 不是合法 UUID：{value}", details={"param": key}) from exc


def _optional_uuid(value: str | None, key: str) -> uuid.UUID | None:
    if value is None:
        return None
    return _uuid(value, key)


def supervisor_tools() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name="assistant.overview",
            title="查看助理看板",
            description=(
                "查看 Flux 助理此刻掌握的整体进度（只读）：设置快照、各阶段工单计数、"
                "最近工单（含最新派活与报告摘要）。回答“现在在做什么 / 卡在哪”时先看它。"
            ),
            input_schema={
                "type": "object",
                "properties": {"limit": _LIMIT_SCHEMA},
                "additionalProperties": False,
            },
            capability=Capability.FILE_READ,
            handler=assistant_overview,
        ),
        ToolSpec(
            name="assistant.ticket",
            title="查看单张工单",
            description=(
                "查看一张工单的完整事实（只读）：派生阶段、全部派活记录、全部报告与订阅目标。"
                "阶段由任务/派活/报告的真实状态派生，不采信任何自报进度。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string", "description": "工单 UUID"},
                },
                "required": ["ticket_id"],
                "additionalProperties": False,
            },
            capability=Capability.FILE_READ,
            handler=assistant_ticket,
        ),
        ToolSpec(
            name="assistant.reports",
            title="查看最近报告",
            description=(
                "按时间倒序读取最近的工单报告（只读），用于汇总“哪些事情已经收尾、"
                "结论是什么”。报告只由平台按任务真实终态写入，不是子 Agent 自报。"
            ),
            input_schema={
                "type": "object",
                "properties": {"limit": _LIMIT_SCHEMA},
                "additionalProperties": False,
            },
            capability=Capability.FILE_READ,
            handler=assistant_reports,
        ),
        ToolSpec(
            name="assistant.timeline",
            title="查看事件时间线",
            description=(
                "把外部事件、工单、报告合成一条时间倒序的时间线（只读），可按线程或项目过滤。"
                "用于还原“这件事从哪来、走到哪一步”。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string", "description": "线程 UUID（可选）"},
                    "project_id": {"type": "string", "description": "项目 UUID（可选）"},
                    "limit": _LIMIT_SCHEMA,
                },
                "additionalProperties": False,
            },
            capability=Capability.FILE_READ,
            handler=assistant_timeline,
        ),
        ToolSpec(
            name="assistant.conversations",
            title="通读全部对话",
            description=(
                "按最近消息倒序列出用户的全部对话（只读）：描述、状态、所用 Agent/模型、"
                "消息条数与最后一条摘录。梳理项目进度时先扫一遍它，再挑相关的展开。"
                "助理自己的对话默认不出现（需要时传 include_assistant=true）。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": _LIMIT_SCHEMA,
                    "project_id": {"type": "string", "description": "项目 UUID（可选）"},
                    "status": {
                        "type": "string",
                        "description": "按任务状态过滤（可选）",
                        "enum": [item.value for item in TaskStatus],
                    },
                    "keyword": {
                        "type": "string",
                        "description": "按对话描述模糊匹配（可选）",
                    },
                    "include_assistant": {
                        "type": "boolean",
                        "description": "是否连助理自己的对话一起列出（默认 false）",
                    },
                },
                "additionalProperties": False,
            },
            capability=Capability.FILE_READ,
            handler=assistant_conversations,
        ),
        ToolSpec(
            name="assistant.conversation",
            title="展开单条对话",
            description=(
                "读取一条对话的完整任务信息与消息记录（只读）。默认取最新一段消息；"
                "has_more 为 true 时把最早一条的 seq 作为 before 继续向上翻。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "对话（任务）UUID"},
                    "limit": _LIMIT_SCHEMA,
                    "before": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "只取 seq 小于该值的更早消息（向上翻页游标，可选）",
                    },
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
            capability=Capability.FILE_READ,
            handler=assistant_conversation,
        ),
    )
