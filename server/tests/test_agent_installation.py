"""Agent Installation 状态机与持久化测试（最终方案 §3.2 / §4 / §20-7）。"""

from __future__ import annotations

import pytest

from flux.container import Container
from flux.core.agent_runtime.adapters.base import CliAgentAdapter, CliAgentProbe
from flux.core.agent_runtime.installation import (
    InstallationRepository,
    InstallationService,
    can_transition,
)
from flux.enums import AgentInstallStatus
from flux.errors import ValidationError


class _StubAdapter(CliAgentAdapter):
    """按预设 probe 返回事实的 Adapter 替身；probe 可在用例中改写以模拟重扫。"""

    def __init__(self, name: str, probe: CliAgentProbe) -> None:
        self.name = name
        self.probe = probe

    def discover(self) -> CliAgentProbe:
        return self.probe

    def verify(self) -> CliAgentProbe:
        return self.probe

    def get_version(self) -> str | None:
        return self.probe.version

    def check_auth(self) -> str:
        return self.probe.auth_status


def _probe(status: AgentInstallStatus, *, name: str = "alpha") -> CliAgentProbe:
    return CliAgentProbe(
        name=name,
        adapter=name,
        status=status,
        executable=name,
        path=f"/usr/bin/{name}",
        version="1.0.0",
        auth_status="ok",
        capabilities=("mcp",),
    )


def _service(container: Container, adapter: _StubAdapter) -> InstallationService:
    repository = InstallationRepository(container.session_factory)
    return InstallationService(repository, adapters=[adapter])


# --- 状态机纯函数 ---


def test_can_transition_allows_forward_and_same() -> None:
    assert can_transition(AgentInstallStatus.DISCOVERED, AgentInstallStatus.VERIFIED)
    assert can_transition(AgentInstallStatus.VERIFIED, AgentInstallStatus.CONNECTED)
    assert can_transition(AgentInstallStatus.CONNECTED, AgentInstallStatus.READY)
    assert can_transition(AgentInstallStatus.READY, AgentInstallStatus.READY)


def test_can_transition_forbids_regression_except_uninstall() -> None:
    assert not can_transition(AgentInstallStatus.READY, AgentInstallStatus.CONNECTED)
    assert can_transition(AgentInstallStatus.READY, AgentInstallStatus.NOT_INSTALLED)


# --- 生命周期 ---


async def test_scan_persists_and_lists(container: Container) -> None:
    service = _service(container, _StubAdapter("alpha", _probe(AgentInstallStatus.CONNECTED)))
    rows = await service.scan()
    assert [row.name for row in rows] == ["alpha"]
    assert rows[0].status == str(AgentInstallStatus.CONNECTED)
    listed = await service.list()
    assert [row.name for row in listed] == ["alpha"]
    assert listed[0].path == "/usr/bin/alpha"


async def test_connect_advances_to_ready(container: Container) -> None:
    service = _service(container, _StubAdapter("alpha", _probe(AgentInstallStatus.CONNECTED)))
    await service.scan()
    row = await service.connect("alpha")
    assert row.status == str(AgentInstallStatus.READY)


async def test_rescan_does_not_regress_ready(container: Container) -> None:
    adapter = _StubAdapter("alpha", _probe(AgentInstallStatus.CONNECTED))
    service = _service(container, adapter)
    await service.scan()
    await service.connect("alpha")
    # 重扫只拿到 DISCOVERED（例如认证命令临时探测失败）：不得把已接入的 Agent 降级
    adapter.probe = _probe(AgentInstallStatus.DISCOVERED)
    rows = await service.scan()
    assert rows[0].status == str(AgentInstallStatus.READY)


async def test_uninstalled_regresses_to_not_installed(container: Container) -> None:
    adapter = _StubAdapter("alpha", _probe(AgentInstallStatus.CONNECTED))
    service = _service(container, adapter)
    await service.scan()
    await service.connect("alpha")
    adapter.probe = _probe(AgentInstallStatus.NOT_INSTALLED)
    rows = await service.scan()
    assert rows[0].status == str(AgentInstallStatus.NOT_INSTALLED)


async def test_connect_not_installed_is_rejected(container: Container) -> None:
    service = _service(container, _StubAdapter("alpha", _probe(AgentInstallStatus.NOT_INSTALLED)))
    with pytest.raises(ValidationError):
        await service.connect("alpha")


async def test_connect_unknown_agent_is_rejected(container: Container) -> None:
    service = _service(container, _StubAdapter("alpha", _probe(AgentInstallStatus.CONNECTED)))
    with pytest.raises(ValidationError):
        await service.connect("nope")


async def test_connect_all_skips_uninstalled(container: Container) -> None:
    ready = _StubAdapter("alpha", _probe(AgentInstallStatus.CONNECTED))
    missing = _StubAdapter("beta", _probe(AgentInstallStatus.NOT_INSTALLED, name="beta"))
    service = InstallationService(
        InstallationRepository(container.session_factory), adapters=[ready, missing]
    )
    rows = await service.connect_all()
    assert [row.name for row in rows] == ["alpha"]


async def test_remove_deletes_record(container: Container) -> None:
    service = _service(container, _StubAdapter("alpha", _probe(AgentInstallStatus.CONNECTED)))
    await service.scan()
    await service.remove("alpha")
    assert await service.list() == []
    with pytest.raises(ValidationError):
        await service.remove("alpha")
