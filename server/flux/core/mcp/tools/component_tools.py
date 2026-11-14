"""生成式组件工具 `component.emit`：把一张经过校验的交互卡片写进当前对话。

设计边界：

- 只写一条 `kind="component"` 的助手消息，`content` 是必填纯文本兜底（IM 频道与老客户端
  看到的就是它），结构化内容在 `payload` 里；
- 写入前必须通过 `validate_component_payload`：非法树不落库；
- 模型不能自带服务端动作（`action.type == "server"`）——服务端动作的 `action_id` 只能由
  服务端在映射既有消息时铸造，避免模型构造内部回调；
- 不触碰审批规则：本工具只是"把 UI 写进对话"，不发送 IM 附件、不执行命令，
  因此也不会绕过既有的出站审批 / 权限闸门。
"""

from __future__ import annotations

import uuid
from typing import Any

from flux.core.components.schema import (
    MAX_TEXT_LENGTH,
    SCHEMA_VERSION,
    validate_component_payload,
)
from flux.core.mcp.tools.base import (
    ToolContext,
    ToolSpec,
    reject_unknown,
    require_str,
)
from flux.core.task_engine.repository import MessageDraft
from flux.enums import Capability, TaskMessageRole
from flux.errors import ValidationError

COMPONENT_EMIT_TOOL_NAME = "component.emit"

COMPONENT_EMIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "component": {
            "type": "object",
            "description": (
                "组件树根节点。type 只能是 card/text/image/file/audio/button/choice/"
                "input/progress/list/divider；按钮动作只能是客户端动作 "
                "open_desktop / open_url。服务端动作由平台生成，不能用本工具创建。"
            ),
        },
        "fallback_text": {
            "type": "string",
            "description": "纯文本兜底（IM 频道与老客户端展示用），必填",
        },
    },
    "required": ["component", "fallback_text"],
    "additionalProperties": False,
}


def _reject_server_actions(node: Any) -> None:
    """工具层禁止模型铸造服务端动作；服务端只在映射既有消息时生成。"""
    if isinstance(node, dict):
        action = node.get("action")
        if isinstance(action, dict) and action.get("type") == "server":
            raise ValidationError(
                "component.emit 不能创建服务端动作；需要决策/密钥输入时请让平台生成卡片",
                details={"action": action.get("name")},
            )
        for value in node.values():
            _reject_server_actions(value)
    elif isinstance(node, list):
        for item in node:
            _reject_server_actions(item)


async def emit_component(
    container: Any, task_id: uuid.UUID, params: dict[str, Any]
) -> dict[str, Any]:
    """校验并写入一条 component 消息；供 MCP 工具与 Bot 对话共用。"""
    reject_unknown(params, {"component", "fallback_text"})
    raw_component = params.get("component")
    if not isinstance(raw_component, dict):
        raise ValidationError("参数 component 必须是对象", details={"param": "component"})
    fallback = require_str(params, "fallback_text")
    if len(fallback) > MAX_TEXT_LENGTH:
        raise ValidationError(
            f"fallback_text 最多 {MAX_TEXT_LENGTH} 个字符",
            details={"param": "fallback_text", "limit": MAX_TEXT_LENGTH},
        )
    _reject_server_actions(raw_component)
    payload = validate_component_payload(
        {
            "schema_version": SCHEMA_VERSION,
            "component": raw_component,
            "fallback_text": fallback,
        }
    )
    message = (
        await container.task_repo.add_messages(
            task_id,
            [
                MessageDraft(
                    role=TaskMessageRole.ASSISTANT.value,
                    kind="component",
                    content=payload["fallback_text"],
                    payload=payload,
                )
            ],
        )
    )[0]
    await container.task_stream.publish(
        task_id, "assistant_message", {"message": message.to_dict()}
    )
    return {
        "written": True,
        "task_id": str(task_id),
        "message_id": str(message.id),
        "schema_version": SCHEMA_VERSION,
        "node_count": _node_count(payload["component"]),
        "note": "组件卡片已写入当前对话；纯文本兜底同时供 IM 频道与老客户端使用。",
    }


async def _task_for_run(ctx: ToolContext) -> uuid.UUID:
    run_id = ctx.identity.run_id
    if not run_id:
        raise ValidationError(
            f"{ctx.tool_name} 必须在 Flux 任务 Run 内调用（当前调用没有关联 Run）"
        )
    run = await ctx.container.run_repo.get(run_id)
    if run is None or run.task_id is None:
        raise ValidationError(
            f"{ctx.tool_name} 找不到当前 Run 归属的任务",
            details={"run_id": run_id},
        )
    return run.task_id


async def component_emit(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    """MCP 入口：从 Run 推出当前任务，再写组件消息。"""
    task_id = await _task_for_run(ctx)
    return await emit_component(ctx.container, task_id, params)


async def run_component_tool(
    container: Any, task_id: uuid.UUID, name: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Bot 对话工具入口：任务由调用方（当前轮次）提供。"""
    if name != COMPONENT_EMIT_TOOL_NAME:
        raise ValidationError(f"不支持的组件工具：{name}", details={"tool": name})
    return await emit_component(container, task_id, params)


def _node_count(node: Any) -> int:
    if not isinstance(node, dict):
        return 0
    total = 1
    for key in ("children", "items"):
        value = node.get(key)
        if isinstance(value, list):
            total += sum(_node_count(item) for item in value)
    return total


def component_tools() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name=COMPONENT_EMIT_TOOL_NAME,
            title="发送交互卡片",
            description=(
                "把一张交互卡片写进当前任务对话：支持标题/正文/图片/文件/音频/按钮/"
                "选项/输入框/进度/列表/分隔线。用户在手机上看到的是可交互卡片，"
                "IM 频道与老客户端看到 fallback_text 纯文本。"
                "按钮只能使用客户端动作 open_desktop（打开桌面接管）或 open_url"
                "（https 公网地址）；决策与安全输入等需要服务端处理的动作由平台生成，"
                "不能用本工具伪造。非法组件树会被拒绝且不会写入任何消息。"
            ),
            input_schema=COMPONENT_EMIT_SCHEMA,
            capability=Capability.FILE_READ,
            handler=component_emit,
        ),
    )


__all__ = [
    "COMPONENT_EMIT_TOOL_NAME",
    "COMPONENT_TOOLS",
    "component_emit",
    "component_tools",
    "emit_component",
    "run_component_tool",
]

#: 供 Bot 对话直接拼进 tools 列表（与 SUBTASK_TOOLS / ROUTINE_TOOLS 一致）。
COMPONENT_TOOLS: tuple[ToolSpec, ...] = component_tools()
