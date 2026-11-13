"""记忆工具：`memory.recall`（只读）与 `memory.remember`（运行中写入用户偏好）。

三层记忆的统一读取口（与 `context.get` / `flux_context` 语义不同、并存不合并；
开工时 `context.boot` 会把三者一次并行取回）：

- `flux_context` = 我在哪里、我能做什么（运行时身份与能力）；
- `context.get`  = 这个项目是什么（项目事实，只读现状）；
- `memory.recall` = Flux 记住的结论（Environment / User / Project 三层，跨会话）。

`memory.remember` 让 Agent 在**任务执行过程中**把用户表现出的稳定偏好/习惯/禁忌写进
User Memory：用的是本轮任务自己的模型通道，不需要任务结束后再单独调模型提炼。
写入仍受控：密钥红线、去重、单条长度与总量上限都由 MemoryService 统一执行。
"""

from __future__ import annotations

import uuid
from typing import Any

from flux.core.mcp.tools.base import (
    ToolContext,
    ToolSpec,
    optional_str,
    reject_unknown,
    require_str,
)
from flux.enums import Capability, MemoryLayer
from flux.errors import NotFoundError, ValidationError

MEMORY_RECALL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project_id": {
            "type": "string",
            "description": "项目 UUID；不传时若给了 task_id 则从任务推导，都拿不到就只返回两层",
        },
        "task_id": {"type": "string", "description": "任务 UUID；用于推导所属项目"},
    },
    "additionalProperties": False,
}


MEMORY_REMEMBER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": (
                "一条跨项目长期有效的用户偏好/习惯/禁忌，尽量 30 字以内；"
                "不要放项目细节、文件路径、密钥"
            ),
        }
    },
    "required": ["content"],
    "additionalProperties": False,
}

#: 单条上限与任务级提炼器一致：太长的「记忆」通常是把执行日志抄了一遍
MAX_REMEMBER_CHARS = 200


async def memory_recall(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"project_id", "task_id"})
    project_id = optional_str(params, "project_id")
    if project_id is None:
        task_id = optional_str(params, "task_id")
        if task_id is not None:
            project_id = await _project_from_task(ctx, task_id)
    # MCP 请求没有登录态：本次 Run 的任务行是唯一可靠的「这是谁」。
    # 解析不到时返回 None，由 MemoryService 按历史口径回退主用户。
    return await ctx.container.memory.recall(
        project_id=project_id,
        cloud_user_id=await _cloud_user_for_run(ctx),
    )


async def memory_remember(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    """写一条 User Memory；重复内容直接返回 duplicate，不新增条目。

    MCP 面没有逐请求的登录态：记忆归属由 MemoryService 按
    `FLUX_OWNER_CLOUD_USER_ID`（部署主用户）解析，与 REST 面同一套隔离规则。
    """
    reject_unknown(params, {"content"})
    content = require_str(params, "content")
    if len(content) > MAX_REMEMBER_CHARS:
        raise ValidationError(
            f"记忆内容不能超过 {MAX_REMEMBER_CHARS} 字",
            details={"param": "content", "max": MAX_REMEMBER_CHARS},
        )
    user_id = await _cloud_user_for_run(ctx)
    entries = await ctx.container.memory.list_entries(layer=MemoryLayer.USER, cloud_user_id=user_id)
    normalized = _normalize(content)
    for entry in entries:
        if _normalize(entry.content) == normalized:
            return {"saved": False, "duplicate": True, "id": str(entry.id)}
    entry = await ctx.container.memory.write_user(
        content=content,
        source="auto",
        meta={"auto": True, "origin": "agent", "agent_id": ctx.identity.agent_id},
        cloud_user_id=user_id,
    )
    return {"saved": True, "duplicate": False, "id": str(entry.id), "content": entry.content}


def _normalize(text: str) -> str:
    return " ".join((text or "").split()).lower()


async def _cloud_user_for_run(ctx: ToolContext) -> str | None:
    """本次 Run 代表哪个 Flux 云用户：run → task → tasks.cloud_user_id。"""
    run_id = ctx.identity.run_id
    if not run_id:
        return None
    run = await ctx.container.run_repo.get(run_id)
    if run is None or run.task_id is None:
        return None
    try:
        task = await ctx.container.task_repo.get(run.task_id)
    except NotFoundError:
        return None
    return getattr(task, "cloud_user_id", None)


async def _project_from_task(ctx: ToolContext, task_id: str) -> str | None:
    try:
        key = uuid.UUID(task_id)
    except ValueError as exc:
        raise ValidationError(
            f"task_id 不是合法 UUID：{task_id}", details={"task_id": task_id}
        ) from exc
    task = await ctx.container.task_repo.get(key)
    return str(task.project_id) if task.project_id is not None else None


def memory_tools() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name="memory.recall",
            title="读取三层长期记忆",
            description=(
                "读取 Flux 侧存档的长期记忆（只读）：Environment（平台运行规则，优先）/ "
                "User（跨 Workspace 的偏好与约定）/ Project（当前项目 Brain）。"
                "记忆是被记住的结论（快照），不替代事实核对——事实用 context.get / "
                "workspace.read 核实。"
            ),
            input_schema=MEMORY_RECALL_SCHEMA,
            capability=Capability.FILE_READ,
            handler=memory_recall,
        ),
        ToolSpec(
            name="memory.remember",
            title="记下用户的长期偏好",
            description=(
                "把本轮对话里用户表现出的稳定偏好、习惯或禁忌写进账号级 User Memory"
                "（跨设备、跨项目长期有效）。只记长期有效的信息，不记项目细节、文件路径、"
                "一次性任务和密钥；同一内容已存在时不会重复写入。"
            ),
            input_schema=MEMORY_REMEMBER_SCHEMA,
            capability=Capability.FILE_READ,
            handler=memory_remember,
        ),
    )
