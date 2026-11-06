"""Connector 契约与注册表测试（主规格 §8）。"""

from __future__ import annotations

import asyncio

import pytest

from flux.connectors.base import ConnectorRegistry
from flux.core.event.bus import EventBus, Events
from flux.enums import Capability
from flux.errors import ConnectorNotRegisteredError, PermissionDeniedError
from tests.fakes import FakeTerminalConnector


def test_unknown_connector_raises() -> None:
    registry = ConnectorRegistry()
    with pytest.raises(ConnectorNotRegisteredError):
        asyncio.run(registry.execute("github", "create_pr"))


def test_execute_without_capability_is_denied() -> None:
    """未授权（含未提供能力）必须拒绝，不得 fail-open。"""
    registry = ConnectorRegistry()
    registry.register(FakeTerminalConnector())

    with pytest.raises(PermissionDeniedError):
        asyncio.run(registry.execute("terminal", "run", {"command": "ls"}, agent_id="agent-1"))
    assert registry.logs == []


def test_execute_with_capability_logs_and_emits_event() -> None:
    bus = EventBus()
    registry = ConnectorRegistry(bus)
    registry.register(FakeTerminalConnector())

    entry = asyncio.run(
        registry.execute(
            "terminal",
            "run",
            {"command": "pytest"},
            agent_id="agent-1",
            granted=frozenset({Capability.TERMINAL_EXECUTE}),
        )
    )

    assert entry["result"]["exit_code"] == 0
    assert entry["agent_id"] == "agent-1"
    # §8.5 每次执行必须留日志
    assert len(registry.logs) == 1
    assert registry.logs[0]["action"] == "run"
    assert Events.CONNECTOR_EXECUTED in [event for event, _ in bus.history]


def test_manifest_serialization() -> None:
    registry = ConnectorRegistry()
    registry.register(FakeTerminalConnector())
    manifest = registry.manifests()[0]
    assert manifest["name"] == "terminal"
    assert manifest["required_permissions"] == ["terminal.execute"]
    assert registry.names() == ["terminal"]


def test_connector_lifecycle_methods() -> None:
    connector = FakeTerminalConnector()
    assert connector.health_check() is False
    connector.initialize()
    assert connector.health_check() is True
    connector.close()
    assert connector.health_check() is False
