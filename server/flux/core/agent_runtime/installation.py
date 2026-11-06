"""Agent Installation：发现、接入与状态机（最终方案 §3.2 / §4 / §20-7）。

Scanner 只负责发现事实，不强行修改用户环境；状态沿
`NOT_INSTALLED → DISCOVERED → VERIFIED → CONNECTED → READY` 单向收敛，
重新扫描不会把已接入的 Agent 倒退回去（卸载除外：明确回到 NOT_INSTALLED）。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.core.agent_runtime.adapters.base import CliAgentAdapter, CliAgentProbe
from flux.core.event.bus import EventBus, Events
from flux.enums import AgentInstallStatus
from flux.errors import ValidationError
from flux.logging import get_logger
from flux.models.agent_installation import AgentInstallation

logger = get_logger(__name__)

#: 状态机的推进顺序；NOT_INSTALLED 作为"未安装"事实排在起点之前
_RANK: dict[AgentInstallStatus, int] = {
    AgentInstallStatus.NOT_INSTALLED: -1,
    AgentInstallStatus.DISCOVERED: 0,
    AgentInstallStatus.VERIFIED: 1,
    AgentInstallStatus.CONNECTED: 2,
    AgentInstallStatus.READY: 3,
}


def can_transition(current: AgentInstallStatus, target: AgentInstallStatus) -> bool:
    """允许前进或原地不动；回到 NOT_INSTALLED 永远允许（Agent 被卸载）。"""
    if target is AgentInstallStatus.NOT_INSTALLED:
        return True
    return _RANK[target] >= _RANK[current]


class InstallationRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_by_name(self, name: str) -> AgentInstallation | None:
        statement = select(AgentInstallation).where(AgentInstallation.name == name)
        async with self._session_factory() as session:
            return await session.scalar(statement)

    async def list(self) -> list[AgentInstallation]:
        statement = select(AgentInstallation).order_by(AgentInstallation.name)
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def upsert(
        self, probe: CliAgentProbe, *, status: AgentInstallStatus
    ) -> AgentInstallation:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(AgentInstallation).where(AgentInstallation.name == probe.name)
            )
            if row is None:
                row = AgentInstallation(name=probe.name)
                session.add(row)
            row.adapter = probe.adapter
            row.status = str(status)
            row.executable = probe.executable
            row.path = probe.path
            row.version = probe.version
            row.source = probe.source
            row.auth_status = probe.auth_status
            row.capabilities = list(probe.capabilities)
            row.detail = dict(probe.detail)
            await session.commit()
            await session.refresh(row)
            return row

    async def set_status(self, name: str, status: AgentInstallStatus) -> AgentInstallation | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(AgentInstallation).where(AgentInstallation.name == name)
            )
            if row is None:
                return None
            row.status = str(status)
            await session.commit()
            await session.refresh(row)
            return row

    async def delete(self, name: str) -> bool:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(AgentInstallation).where(AgentInstallation.name == name)
            )
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True


class InstallationService:
    """编排发现 → 状态机 → 持久化；Adapter 只提供事实。"""

    def __init__(
        self,
        repository: InstallationRepository,
        *,
        bus: EventBus | None = None,
        adapters: Sequence[CliAgentAdapter] = (),
    ) -> None:
        self._repository = repository
        self._bus = bus
        self._adapters: dict[str, CliAgentAdapter] = {a.name: a for a in adapters}

    @property
    def adapters(self) -> tuple[str, ...]:
        return tuple(self._adapters)

    async def scan(self) -> list[AgentInstallation]:
        """扫描全部内置 Adapter，落库并返回当前状态。"""
        results: list[AgentInstallation] = []
        for adapter in self._adapters.values():
            results.append(await self._record(adapter.discover()))
        return results

    async def list(self) -> list[AgentInstallation]:
        return await self._repository.list()

    async def get(self, name: str) -> AgentInstallation | None:
        return await self._repository.get_by_name(name)

    async def connect(self, name: str) -> AgentInstallation:
        """把已安装的 Agent 接入 Flux：复验事实后推进到 READY。"""
        adapter = self._require_adapter(name)
        probe = adapter.verify()
        if probe.status is AgentInstallStatus.NOT_INSTALLED:
            raise ValidationError(f"Agent 未安装，无法接入：{name}", details={"agent": name})
        row = await self._record(probe)
        return await self._advance(row.name, AgentInstallStatus.READY)

    async def connect_all(self) -> list[AgentInstallation]:
        connected: list[AgentInstallation] = []
        for name in self._adapters:
            try:
                connected.append(await self.connect(name))
            except ValidationError:
                # 未安装的 Agent 直接跳过；connect_all 不该因为"某个没装"而整体失败
                logger.info("agent.connect_all 跳过未安装的 Agent name=%s", name)
        return connected

    async def remove(self, name: str) -> None:
        if not await self._repository.delete(name):
            raise ValidationError(f"没有该 Agent 的接入记录：{name}", details={"agent": name})

    # --- 内部 ---

    def _require_adapter(self, name: str) -> CliAgentAdapter:
        adapter = self._adapters.get(name)
        if adapter is None:
            raise ValidationError(
                f"未知的 Agent：{name}", details={"agent": name, "known": list(self._adapters)}
            )
        return adapter

    async def _record(self, probe: CliAgentProbe) -> AgentInstallation:
        existing = await self._repository.get_by_name(probe.name)
        target = probe.status
        if existing is not None:
            current = AgentInstallStatus(existing.status)
            if not can_transition(current, target):
                target = current  # 不倒退：已接入的 Agent 不会因一次扫描降级
        row = await self._repository.upsert(probe, status=target)
        await self._publish(existing, row)
        return row

    async def _advance(self, name: str, target: AgentInstallStatus) -> AgentInstallation:
        existing = await self._repository.get_by_name(name)
        if existing is None:
            raise ValidationError(f"没有该 Agent 的接入记录：{name}", details={"agent": name})
        current = AgentInstallStatus(existing.status)
        if not can_transition(current, target):
            raise ValidationError(
                f"状态不允许从 {current} 变到 {target}",
                details={"from": str(current), "to": str(target)},
            )
        row = await self._repository.set_status(name, target)
        assert row is not None  # 上面刚查到，同一进程内不会消失
        await self._publish(existing, row)
        return row

    async def _publish(self, before: AgentInstallation | None, after: AgentInstallation) -> None:
        if self._bus is None:
            return
        previous = before.status if before is not None else None
        if previous == after.status:
            return
        await self._bus.publish(
            Events.AGENT_INSTALLATION_CHANGED,
            {"agent": after.name, "from": previous, "to": after.status},
        )


__all__ = [
    "InstallationRepository",
    "InstallationService",
    "can_transition",
]
