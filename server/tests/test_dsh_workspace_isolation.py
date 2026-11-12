"""S4：DSH runtime 必须按任务使用独立工作目录（Bot 之间不能共享 cwd）。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

from flux.config import Settings
from flux.core.agent_runtime.dsh_client import TERMINAL_STATUSES, FluxDshClient
from flux.core.agent_runtime.runtimes.dsh_runtime import DshRuntime
from tests.fakes import FakeDshHarnessFactory


def _settings(tmp_path: Path, *, workspace_root: str | None = None) -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'dsh.db'}",
        workspace_root=workspace_root,
        openai_api_key=None,
        anthropic_api_key=None,
        deepseek_api_key=None,
        default_provider="local",
        dsh_enabled=True,
        dsh_home=str(tmp_path / "dsh-home"),
        dsh_workspace=str(tmp_path / "dsh-ws"),
        dsh_mcp_enabled=False,
    )


async def _wait_terminal(client: FluxDshClient, run_id: str, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    run = client.get_run(run_id)
    while run.status not in TERMINAL_STATUSES and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert run.status in TERMINAL_STATUSES


async def test_dsh_runtime_uses_each_tasks_workspace(tmp_path: Path) -> None:
    """两个任务带不同 workspace：harness 的 cwd 与 FLUX_WORKSPACE 必须各用各的目录。"""
    settings = _settings(tmp_path)
    factory = FakeDshHarnessFactory()
    client = FluxDshClient(settings, harness_factory=factory)
    runtime = DshRuntime(client, workspace=settings.dsh_workspace)
    workspace_a = tmp_path / "bots" / "a" / "workspace"
    workspace_b = tmp_path / "bots" / "b" / "workspace"

    try:
        for run_id, task_id, workspace in (
            ("run-a", "task-a", workspace_a),
            ("run-b", "task-b", workspace_b),
        ):
            task = SimpleNamespace(id=task_id, workspace=str(workspace))
            launch = await runtime.prepare(task=task, agent=None, run_id=run_id, instruction="hi")
            assert launch.cwd == str(workspace)
            assert workspace.is_dir()
            await runtime.start(launch)
            await _wait_terminal(client, run_id)

        assert [h.kwargs["cwd"] for h in factory.created] == [
            str(workspace_a),
            str(workspace_b),
        ]
        assert [h.kwargs["env"]["FLUX_WORKSPACE"] for h in factory.created] == [
            str(workspace_a),
            str(workspace_b),
        ]
    finally:
        client.close()


async def test_dsh_runtime_without_task_workspace_falls_back_to_default(tmp_path: Path) -> None:
    """没有 tasks.workspace 的 Run 保持原默认：settings.dsh_workspace。"""
    settings = _settings(tmp_path)
    factory = FakeDshHarnessFactory()
    client = FluxDshClient(settings, harness_factory=factory)
    runtime = DshRuntime(client, workspace=settings.dsh_workspace)
    task = SimpleNamespace(id="task-no-ws", workspace=None)

    try:
        launch = await runtime.prepare(
            task=task, agent=None, run_id="run-default", instruction="hi"
        )
        assert launch.cwd == settings.dsh_workspace
        await runtime.start(launch)
        await _wait_terminal(client, launch.run_id)

        harness = factory.created[0]
        assert harness.kwargs["cwd"] == settings.dsh_workspace
        assert harness.kwargs["env"]["FLUX_WORKSPACE"] == settings.dsh_workspace
    finally:
        client.close()


async def test_dsh_runtime_without_task_workspace_keeps_workspace_root_env(
    tmp_path: Path,
) -> None:
    """没有 tasks.workspace：harness cwd 用 dsh_workspace，FLUX_WORKSPACE 仍是 workspace_root。"""
    workspace_root = str(tmp_path / "root")
    settings = _settings(tmp_path, workspace_root=workspace_root)
    factory = FakeDshHarnessFactory()
    client = FluxDshClient(settings, harness_factory=factory)
    runtime = DshRuntime(client, workspace=settings.dsh_workspace)
    task = SimpleNamespace(id="task-no-ws", workspace=None)

    try:
        launch = await runtime.prepare(task=task, agent=None, run_id="run-root", instruction="hi")
        assert launch.cwd == settings.dsh_workspace
        assert launch.workspace is None
        await runtime.start(launch)
        await _wait_terminal(client, launch.run_id)

        harness = factory.created[0]
        assert harness.kwargs["cwd"] == settings.dsh_workspace
        assert harness.kwargs["env"]["FLUX_WORKSPACE"] == workspace_root
    finally:
        client.close()
