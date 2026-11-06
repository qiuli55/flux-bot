"""Connector 契约与注册表（主规格 §8.1–8.5）。

契约取 SDK Guide 的五方法版本（裁决 A5）：initialize / validate_permission / execute /
health_check / close。Agent 只能通过 Connector 访问外部系统（ADR-005）。

M0 只交付契约与注册表；GitHub / Terminal / Browser / File System 等具体连接器是 M4 交付物
（主规格 §19.1 Issues #040–#044）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from flux.core.event.bus import EventBus, Events
from flux.core.permission_engine.policy import PermissionPolicy
from flux.enums import Capability
from flux.errors import ConnectorNotRegisteredError
from flux.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ConnectorManifest:
    """Connector 清单（主规格 §8.3）。"""

    name: str
    version: str
    type: str
    actions: tuple[str, ...]
    required_permissions: tuple[Capability, ...] = ()
    configuration: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "type": self.type,
            "actions": list(self.actions),
            "required_permissions": [str(p) for p in self.required_permissions],
            "configuration": dict(self.configuration),
        }


class Connector(ABC):
    manifest: ConnectorManifest

    @abstractmethod
    def initialize(self) -> None:
        """建立连接、加载配置。"""

    @abstractmethod
    def validate_permission(self, capability: Capability) -> None:
        """校验本次操作所需能力，不满足则抛 PermissionDeniedError。"""

    @abstractmethod
    async def execute(self, action: str, parameters: dict[str, Any]) -> dict[str, Any]:
        """执行动作，返回结果。实现方必须附带执行日志字段。"""

    @abstractmethod
    def health_check(self) -> bool:
        """健康检查。"""

    @abstractmethod
    def close(self) -> None:
        """释放资源。"""


class ConnectorRegistry:
    """Connector 注册表 + 带审计的执行入口（主规格 §8.5：每次执行必须留日志）。"""

    def __init__(self, bus: EventBus | None = None, policy: PermissionPolicy | None = None) -> None:
        self._connectors: dict[str, Connector] = {}
        self._logs: list[dict[str, Any]] = []
        self._bus = bus
        self._policy = policy or PermissionPolicy()

    def register(self, connector: Connector) -> None:
        self._connectors[connector.manifest.name] = connector
        logger.info("connector.register name=%s", connector.manifest.name)

    def get(self, name: str) -> Connector:
        connector = self._connectors.get(name)
        if connector is None:
            raise ConnectorNotRegisteredError(
                f"Connector {name} 未注册",
                details={"connector": name, "registered": sorted(self._connectors)},
            )
        return connector

    def names(self) -> list[str]:
        return sorted(self._connectors)

    def manifests(self) -> list[dict[str, Any]]:
        return [c.manifest.to_dict() for c in self._connectors.values()]

    @property
    def logs(self) -> list[dict[str, Any]]:
        return list(self._logs)

    async def execute(
        self,
        name: str,
        action: str,
        parameters: dict[str, Any] | None = None,
        *,
        agent_id: str | None = None,
        granted: frozenset[Capability] = frozenset(),
    ) -> dict[str, Any]:
        connector = self.get(name)
        params = dict(parameters or {})

        # Agent 只能做它被授权的事（ADR-005 / §14.5 最小权限）。
        # 未提供 granted 时按"无任何能力"处理（fail-closed），而不是放行。
        for capability in connector.manifest.required_permissions:
            self._policy.require_agent_capability(granted, capability, agent=agent_id or "")
            connector.validate_permission(capability)

        result = await connector.execute(action, params)

        entry = {
            "connector": name,
            "agent_id": agent_id,
            "action": action,
            "parameters": params,
            "result": result,
        }
        # M0 记在进程内；M4 落 connector_logs 表（主规格 §11.2）
        self._logs.append(entry)
        if self._bus is not None:
            await self._bus.publish(Events.CONNECTOR_EXECUTED, entry)
        return entry
