"""Bot 节点启动自动接入 Claude Code：开关、成功与缺 CLI 三条路径。"""

from __future__ import annotations

import logging

from flux.bot_node.main import auto_connect_claude_code
from flux.config import Settings
from flux.container import Container
from flux.core.agent_runtime.adapters.base import CliAgentAdapter, CliAgentProbe
from flux.core.agent_runtime.installation import InstallationRepository, InstallationService
from flux.enums import AgentInstallStatus


class _StubAdapter(CliAgentAdapter):
    """按预设 probe 返回事实的 Adapter 替身，并记录 verify 调用次数。"""

    def __init__(self, probe: CliAgentProbe) -> None:
        self.name = probe.name
        self.probe = probe
        self.verify_calls = 0

    def discover(self) -> CliAgentProbe:
        return self.probe

    def verify(self) -> CliAgentProbe:
        self.verify_calls += 1
        return self.probe

    def get_version(self) -> str | None:
        return self.probe.version

    def check_auth(self) -> str:
        return self.probe.auth_status


def _install_stub(container: Container, *, installed: bool) -> _StubAdapter:
    """把容器里的 installations 换成受控的服务：只保留 claude-code。"""
    probe = CliAgentProbe(
        name="claude-code",
        adapter="claude-code",
        status=(AgentInstallStatus.CONNECTED if installed else AgentInstallStatus.NOT_INSTALLED),
        executable="claude",
        path="/usr/local/bin/claude" if installed else None,
        version="2.1.323" if installed else None,
        auth_status="ok" if installed else "missing",
        capabilities=("mcp", "stream"),
        detail={} if installed else {"reason": "PATH 中未找到可执行文件"},
    )
    adapter = _StubAdapter(probe)
    container.installations = InstallationService(
        InstallationRepository(container.session_factory),
        adapters=[adapter],
    )
    return adapter


async def test_enabled_and_binary_found_marks_ready(
    container: Container, settings: Settings
) -> None:
    _install_stub(container, installed=True)
    enabled = settings.model_copy(update={"auto_connect_claude_code": True})

    await auto_connect_claude_code(container, enabled)

    rows = await container.installations.list()
    assert [(row.name, row.status) for row in rows] == [("claude-code", "READY")]


async def test_disabled_leaves_installation_untouched(
    container: Container, settings: Settings
) -> None:
    adapter = _install_stub(container, installed=True)
    disabled = settings.model_copy(update={"auto_connect_claude_code": False})

    await auto_connect_claude_code(container, disabled)

    assert adapter.verify_calls == 0
    assert await container.installations.list() == []


async def test_binary_missing_warns_without_crashing(
    container: Container,
    settings: Settings,
    caplog,
) -> None:
    _install_stub(container, installed=False)
    enabled = settings.model_copy(update={"auto_connect_claude_code": True})

    with caplog.at_level(logging.WARNING, logger="flux.bot_node.main"):
        # 缺 CLI 时 connect 抛 ValidationError；这里必须被吞掉，不能中断启动流程。
        await auto_connect_claude_code(container, enabled)

    assert await container.installations.list() == []
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert any("claude" in record.getMessage().lower() for record in warnings)
