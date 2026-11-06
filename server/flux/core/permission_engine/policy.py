"""权限策略：RBAC + Capability（主规格 §5.5 / §8.4 / §14.5 最小权限）。"""

from __future__ import annotations

from collections.abc import Iterable

from flux.enums import Capability, Role
from flux.errors import PermissionDeniedError

# 角色 → 能力集（主规格 §5.5 企业角色）
ROLE_CAPABILITIES: dict[Role, frozenset[Capability]] = {
    Role.ADMIN: frozenset(Capability),
    Role.DEVELOPER: frozenset(
        {
            Capability.FILE_READ,
            Capability.FILE_WRITE,
            Capability.TERMINAL_EXECUTE,
            Capability.GITHUB_CREATE_PR,
            Capability.BROWSER_OPEN,
        }
    ),
    Role.REVIEWER: frozenset({Capability.FILE_READ, Capability.BROWSER_OPEN}),
    Role.OBSERVER: frozenset({Capability.FILE_READ}),
}


class PermissionPolicy:
    def capabilities_for(self, role: Role) -> frozenset[Capability]:
        return ROLE_CAPABILITIES.get(role, frozenset())

    def grants(self, role: Role, capability: Capability) -> bool:
        return capability in self.capabilities_for(role)

    def require(self, role: Role, capability: Capability) -> None:
        """不符则抛 PermissionDeniedError。"""
        if not self.grants(role, capability):
            raise PermissionDeniedError(
                f"角色 {role} 不具备能力 {capability}",
                details={"role": str(role), "capability": str(capability)},
            )

    def require_agent_capability(
        self, granted: Iterable[Capability], required: Capability, *, agent: str = ""
    ) -> None:
        """校验 Agent 声明的能力是否覆盖操作所需能力（§14.5 Agent 只获得必需能力）。"""
        if required not in frozenset(granted):
            raise PermissionDeniedError(
                f"Agent {agent or '<unknown>'} 未获授权的能力 {required}",
                details={"agent": agent, "capability": str(required)},
            )
