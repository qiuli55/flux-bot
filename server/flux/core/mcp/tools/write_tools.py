"""写入工具：`proposal.create`（目标架构 §3.3）。

这是 agent 唯一能改变"世界"的通道——而且只改变 Flux 的**虚拟提案**，
不碰用户的真实文件：真实落盘必须经人审 + ApplyEngine（§3.5 硬禁令）。

入参解析复用 `proposal_parser`（与 REST 导入路径同一份校验），
provenance 的 `agent_source` 由服务端用令牌身份盖章，客户端传什么都不作数。
P3-16 §4.4：请求体里若自带 `agent_id`，必须与令牌身份一致，否则直接拒绝——
不允许 Agent 假冒别的 Agent 提交提案。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from flux.core.mcp.tools.base import (
    ToolContext,
    ToolSpec,
    optional_str,
    reject_unknown,
)
from flux.core.virtual_workspace.proposal_parser import parse_code_change_set
from flux.enums import Capability
from flux.errors import NotFoundError, PermissionDeniedError, ValidationError

PROPOSAL_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "payload": {
            "description": (
                "提案内容：形如 {summary, changes:[{path, op, content, reason}]} 的 JSON 对象"
                "或其 JSON 文本（也接受 ```json 围栏）。op 可选 create/modify/delete，缺省按"
                "原文件是否存在自动判定；delete 时禁止携带 content，其余 op 必须给出改动后的"
                "完整内容。"
            ),
            "anyOf": [{"type": "object"}, {"type": "string"}],
        },
        "task_id": {"type": "string", "description": "该改动服务的任务 UUID（可空）"},
        "project_id": {"type": "string", "description": "项目 UUID（可空）"},
        "agent_id": {
            "type": "string",
            "description": "（可空）调用方声明的 Agent canonical UUID；与令牌不一致会被拒绝",
        },
    },
    "required": ["payload"],
    "additionalProperties": False,
}


async def _run_scope(ctx: ToolContext) -> tuple[str | None, str | None]:
    """提案没写 task/project 时，从发起它的 Run 找回归属（task_id, project_id）。

    模型常常只传 payload（2026-10-23 基准 T02 实测），提案落成 task_id=None：人审队列
    看不出它属于哪个任务，Apply 后的 Brain 沉淀也无处可写。Run 本身记着 task_id，
    由服务端补上比让模型猜 UUID 可靠。
    """
    run_id = getattr(ctx.identity, "run_id", None)
    if not run_id:
        return None, None
    run = await ctx.container.run_repo.get(run_id)
    if run is None or run.task_id is None:
        return None, None
    task_id = str(run.task_id)
    try:
        task = await ctx.container.task_repo.get(run.task_id)
    except NotFoundError:
        # Run 记着已不存在的任务：保留 task_id（审计仍可见归属），项目只能留空
        return task_id, None
    return task_id, str(task.project_id) if task.project_id else None


async def proposal_create(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"payload", "task_id", "project_id", "agent_id"})
    _reject_impersonation(ctx, optional_str(params, "agent_id"))
    change_set = parse_code_change_set(_as_text(params.get("payload")), source="proposal.create")

    task_id = optional_str(params, "task_id")
    project_id = optional_str(params, "project_id")
    if task_id is None or project_id is None:
        run_task, run_project = await _run_scope(ctx)
        task_id = task_id or run_task
        project_id = project_id or run_project

    originals: dict[str, str | None] = {}
    for change in change_set.changes:
        originals[change.path] = await _original_content(ctx, change.path)

    created = await ctx.container.workspace.propose_changes(
        change_set,
        project_id=project_id,
        task_id=task_id,
        # 服务端盖章：提案归属只看令牌，不看请求体
        agent_source=ctx.identity.agent_id,
        original_files=originals,
    )
    return {
        "count": len(created),
        "agent": ctx.identity.agent_id,
        "changes": [
            {
                "change_id": str(change.id),
                "file_path": change.file_path,
                "kind": change.kind,
                "status": change.status,
                "added_lines": change.added_lines,
                "removed_lines": change.removed_lines,
                "hunks": change.hunks,
            }
            for change in created
        ],
        "note": "提案已进入人工审核队列，尚未落盘；落盘只在人审通过后由 Apply Engine 执行。",
    }


def _reject_impersonation(ctx: ToolContext, declared: str | None) -> None:
    """请求自报的 agent_id 与令牌不一致时拒绝（P3-16 §4.4 / TC-16C）。

    身份以令牌为准；这里只额外检查"有没有人试图冒充别人"，不做任何"以请求体为准"的兜底。
    """
    if not declared:
        return
    if declared.strip() != ctx.identity.agent_id:
        raise PermissionDeniedError(
            "请求声明的 agent_id 与令牌身份不一致，已拒绝（禁止身份冒充）",
            details={
                "declared": declared.strip(),
                "token_agent_id": ctx.identity.agent_id,
                "token_id": ctx.identity.token_id,
            },
        )


def _as_text(payload: Any) -> str:
    if isinstance(payload, (dict, list)):
        return json.dumps(payload, ensure_ascii=False)
    if isinstance(payload, str) and payload.strip():
        return payload
    raise ValidationError("参数 payload 必须是提案对象或其 JSON 文本", details={"param": "payload"})


async def _original_content(ctx: ToolContext, path: str) -> str | None:
    """取提案所依据的原文件内容；文件不存在返回 None（与"存在但为空文件"区分开）。"""
    try:
        content = await asyncio.to_thread(ctx.container.files.read, path=path)
    except NotFoundError:
        return None
    if content.truncated:
        # 读到的只是前 256KB，用它当 original 会生成一份错误的 diff，
        # 人审时看到的"改动"就是假的——宁可明确拒绝。
        raise ValidationError(
            f"文件过大（超过读取上限），无法作为提案基线：{path}",
            details={"path": path},
        )
    return content.content


def write_tools() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name="proposal.create",
            title="提交改动提案",
            description=(
                "把一组文件改动作为提案提交进 Flux 的人工审核队列（不落盘）。"
                "每个文件必须给出改动后的完整内容；提案不会直接写入真实文件，"
                "落盘由人审通过后经 Apply Engine 执行。"
            ),
            input_schema=PROPOSAL_CREATE_SCHEMA,
            capability=Capability.FILE_WRITE,
            handler=proposal_create,
            approval_capable=True,
        ),
    )
