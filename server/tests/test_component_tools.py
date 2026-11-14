"""Bot / MCP 工具 `component.emit`。

口径：
- 写入前必须通过组件 schema 校验；失败时一条消息都不能留下；
- 模型不能自带服务端动作（服务端动作只能由平台映射既有消息时生成）；
- 工具只写一条 kind=component 的助手消息，content 是纯文本兜底；
- Plan 模式的工具白名单不被放宽（component.emit 不在 PLAN_ALLOWED_TOOLS 里）。
"""

from __future__ import annotations

import uuid

import pytest

from flux.core.mcp.auth import AgentIdentity
from flux.core.mcp.tools import TOOLS
from flux.core.mcp.tools.base import ToolContext
from flux.core.mcp.tools.component_tools import (
    COMPONENT_EMIT_TOOL_NAME,
    component_emit,
    run_component_tool,
)
from flux.core.permissions.modes import PLAN_ALLOWED_TOOLS
from flux.enums import Capability, DshRunStatus
from flux.errors import ValidationError

_CARD = {
    "type": "card",
    "title": "今日进度",
    "children": [
        {"type": "text", "text": "已经完成 2 项"},
        {"type": "progress", "label": "处理中", "percent": 60, "status": "running"},
    ],
}


async def _seed(container, *, with_run: bool = True) -> tuple[ToolContext, uuid.UUID]:
    bot = await container.bots.create(
        name=f"组件Bot-{uuid.uuid4().hex[:6]}",
        description="发卡片",
    )
    task = await container.task_repo.create(
        task_id=uuid.uuid4(),
        description="组件工具用例",
        priority=1,
        project_id=None,
        agent_id=None,
        bot_id=bot.id,
        cloud_user_id="1",
    )
    run_id = uuid.uuid4().hex
    agent_id = str(uuid.uuid4())
    if with_run:
        await container.run_repo.create(
            run_id=run_id,
            session_id="sess",
            instruction="emit a card",
            status=DshRunStatus.RUNNING,
            agent_id=agent_id,
            task_id=task.id,
            permission_mode="normal",
        )
    ctx = ToolContext(
        container=container,
        identity=AgentIdentity(
            agent_id=agent_id,
            token_id="t",
            scopes=frozenset({Capability.FILE_READ}),
            run_id=run_id if with_run else None,
        ),
        tool_name=COMPONENT_EMIT_TOOL_NAME,
    )
    return ctx, task.id


async def _messages(container, task_id: uuid.UUID):
    messages, _ = await container.task_repo.list_messages(task_id, limit=100)
    return messages


def test_tool_is_registered_but_not_allowed_in_plan_mode() -> None:
    assert COMPONENT_EMIT_TOOL_NAME in TOOLS
    assert COMPONENT_EMIT_TOOL_NAME not in PLAN_ALLOWED_TOOLS


async def test_emit_writes_one_component_message(container) -> None:
    ctx, task_id = await _seed(container)
    result = await component_emit(
        ctx, {"component": _CARD, "fallback_text": "今日进度：已完成 2 项"}
    )
    assert result["written"] is True
    assert result["schema_version"] == 1
    assert result["node_count"] == 3

    messages = await _messages(container, task_id)
    assert len(messages) == 1
    stored = messages[0]
    assert stored.kind == "component"
    assert stored.role == "assistant"
    assert stored.content == "今日进度：已完成 2 项"
    assert stored.payload["schema_version"] == 1
    assert stored.payload["fallback_text"] == "今日进度：已完成 2 项"
    assert stored.payload["component"]["type"] == "card"


async def test_invalid_tree_does_not_write_a_message(container) -> None:
    ctx, task_id = await _seed(container)
    with pytest.raises(ValidationError):
        await component_emit(ctx, {"component": {"type": "iframe"}, "fallback_text": "x"})
    assert await _messages(container, task_id) == []


async def test_server_actions_are_rejected_for_model_emitted_cards(container) -> None:
    ctx, task_id = await _seed(container)
    node = {
        "type": "button",
        "label": "同意",
        "action": {
            "type": "server",
            "name": "decision.choose",
            "action_id": "decision:forged:0",
            "decision_id": "forged",
            "option": "A",
        },
    }
    with pytest.raises(ValidationError) as excinfo:
        await component_emit(ctx, {"component": node, "fallback_text": "选择"})
    assert "服务端动作" in excinfo.value.message
    assert await _messages(container, task_id) == []


async def test_unknown_params_and_missing_fallback_are_rejected(container) -> None:
    ctx, task_id = await _seed(container)
    with pytest.raises(ValidationError):
        await component_emit(ctx, {"component": _CARD, "fallback_text": "x", "chat_id": "其他会话"})
    with pytest.raises(ValidationError):
        await component_emit(ctx, {"component": _CARD})
    assert await _messages(container, task_id) == []


async def test_emit_requires_a_run_for_the_mcp_entrypoint(container) -> None:
    ctx, _task_id = await _seed(container, with_run=False)
    with pytest.raises(ValidationError) as excinfo:
        await component_emit(ctx, {"component": _CARD, "fallback_text": "x"})
    assert "Run" in excinfo.value.message


async def test_bot_entrypoint_uses_the_current_task(container) -> None:
    _ctx, task_id = await _seed(container, with_run=False)
    result = await run_component_tool(
        container,
        task_id,
        COMPONENT_EMIT_TOOL_NAME,
        {"component": _CARD, "fallback_text": "给 Bot 对话的卡片"},
    )
    assert result["message_id"]
    messages = await _messages(container, task_id)
    assert [message.kind for message in messages] == ["component"]
