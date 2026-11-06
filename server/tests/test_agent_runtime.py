"""Agent Manager 档案注册表测试（主规格 §5.1；目标架构 §1）。

Flux 不执行 Agent——这里只验证档案的注册、查询与状态跃迁。
"""

from __future__ import annotations

import asyncio

import pytest

from flux.container import Container
from flux.core.agent_runtime.manager import AgentSpec
from flux.core.event.bus import Events
from flux.enums import AgentRole, AgentState, Capability
from flux.errors import NotFoundError


def _spec(**overrides: object) -> AgentSpec:
    base: dict[str, object] = {
        "name": "开发档案",
        "role": AgentRole.DEVELOPER,
        "permissions": frozenset({Capability.FILE_WRITE}),
    }
    base.update(overrides)
    return AgentSpec(**base)  # type: ignore[arg-type]


def test_create_agent_ends_ready(container: Container) -> None:
    handle = asyncio.run(container.agents.create(_spec()))
    assert handle.state is AgentState.READY
    assert container.agents.count() == 1
    assert container.agents.get(handle.id_str) is handle


def test_create_publishes_state_change_events(container: Container) -> None:
    asyncio.run(container.agents.create(_spec()))
    events = [
        payload for event, payload in container.bus.history if event == Events.AGENT_STATE_CHANGED
    ]
    assert [item["to"] for item in events] == ["INITIALIZING", "READY"]
    assert events[0]["agent_id"] == container.agents.list()[0].id_str


def test_spec_serialises_without_model_fields(container: Container) -> None:
    """档案只含身份、权限边界与执行引擎（runtime）：模型与 system prompt 是 Agent 自己的事。"""
    handle = asyncio.run(container.agents.create(_spec(description="负责实现代码改动")))
    assert handle.to_dict() == {
        "id": handle.id_str,
        "state": "READY",
        "spec": {
            "name": "开发档案",
            "role": "developer",
            "description": "负责实现代码改动",
            "skills": [],
            "tools": [],
            "permissions": ["file.write"],
            # UI 要显示"这个 Agent 由谁执行"（2026-10-06 起随 to_dict 输出）
            "runtime": "dsh",
            # 客户端据此标注「非逐字流式」；内置 DSH 走模型 SSE 旁路，是原生流式
            "native_streaming": True,
        },
    }


def test_list_keeps_creation_order(container: Container) -> None:
    first = asyncio.run(container.agents.create(_spec(name="甲")))
    second = asyncio.run(container.agents.create(_spec(name="乙")))
    assert container.agents.count() == 2
    assert [handle.id_str for handle in container.agents.list()] == [first.id_str, second.id_str]


def test_stop_is_idempotent(container: Container) -> None:
    handle = asyncio.run(container.agents.create(_spec()))
    assert asyncio.run(container.agents.stop(handle.id_str)).state is AgentState.STOPPED
    assert asyncio.run(container.agents.stop(handle.id_str)).state is AgentState.STOPPED


def test_unknown_and_malformed_agent_id(container: Container) -> None:
    with pytest.raises(NotFoundError):
        container.agents.get("0f5b6f4c-0000-0000-0000-000000000000")
    with pytest.raises(NotFoundError):
        container.agents.get("不是-uuid")
