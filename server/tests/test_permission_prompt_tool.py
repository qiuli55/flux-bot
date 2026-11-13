"""Claude Code 权限审批桥测试（`permission_prompt` + `--permission-prompt-tool`）。

覆盖三层：

1. 工具层：allow / deny / timeout / cancelled / 坏参数 / 找不到 Run / 非正常模式 /
   内部异常；输出必须是恰好一个 text block 的合法 JSON，且不带 structuredContent；
2. 安全边界：预览按敏感键与 `redact()` 打码，日志不记输入值；审批桥自身不可再审批；
3. argv：只有 Flux 正常模式且 mcp.json 里有 flux 端点时才追加
   `--permission-prompt-tool mcp__flux__permission_prompt`。
"""

from __future__ import annotations

import importlib
import json
import logging
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from flux.core.agent_runtime.runtimes.base import RUNTIME_CLAUDE
from flux.core.agent_runtime.runtimes.cli import (
    CLAUDE_PERMISSION_PROMPT_TOOL,
    CliRuntime,
)
from flux.core.mcp.auth import AgentIdentity
from flux.core.mcp.server import McpServer
from flux.core.mcp.tools import TOOLS
from flux.core.mcp.tools.base import ToolContext
from flux.core.mcp.tools.permission_tools import PERMISSION_PROMPT_TOOL, permission_prompt
from flux.core.permissions.modes import tool_allowed
from flux.core.permissions.risk import LEVEL_HIGH
from flux.core.task_engine.approvals import APPROVAL_KIND_TOOL_PROMPT, APPROVAL_TIMEOUT_SECONDS
from flux.enums import Capability, DshRunStatus, PermissionMode

#: 包 __init__ 里的同名属性是工具工厂函数，会遮蔽子模块；按模块路径取模块后再打补丁。
permission_tools = importlib.import_module("flux.core.mcp.tools.permission_tools")

AGENT_ID = str(uuid.uuid4())
TASK_ID = uuid.uuid4()
RUN_ID = "run-1"
TOOL_INPUT = {"file_path": "/workspace/hello.txt", "content": "hi\n"}


class _RunRepo:
    def __init__(self, run: Any) -> None:
        self._run = run

    async def get(self, run_id: str) -> Any:
        return self._run


def _run(
    *,
    task_id: uuid.UUID | None = TASK_ID,
    agent_id: str = AGENT_ID,
    status: str = DshRunStatus.RUNNING.value,
) -> Any:
    return SimpleNamespace(task_id=task_id, agent_id=agent_id, status=status)


_MISSING = object()


def _ctx(
    run: Any = _MISSING,
    *,
    run_id: str | None = RUN_ID,
    mode: str = PermissionMode.NORMAL.value,
) -> ToolContext:
    container = SimpleNamespace(run_repo=_RunRepo(_run() if run is _MISSING else run))
    return ToolContext(
        container=container,  # type: ignore[arg-type]
        identity=AgentIdentity(
            agent_id=AGENT_ID,
            token_id="t",
            scopes=frozenset({Capability.FILE_READ}),
            run_id=run_id,
            permission_mode=mode,
        ),
        tool_name="permission_prompt",
    )


def _install_approval(monkeypatch: pytest.MonkeyPatch, outcome: str) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def fake_raise(container: Any, **kwargs: Any) -> dict[str, Any]:
        captured["raise"] = kwargs
        return {"id": "decision-1"}

    async def fake_wait(container: Any, **kwargs: Any) -> str:
        captured["wait"] = kwargs
        return outcome

    monkeypatch.setattr(permission_tools, "raise_approval", fake_raise)
    monkeypatch.setattr(permission_tools, "wait_for_approval", fake_wait)
    return captured


# ---------------------------------------------------------------- 1. 工具面与映射


def test_permission_prompt_is_registered_and_exposed_in_normal_mode() -> None:
    assert "permission_prompt" in TOOLS
    spec = TOOLS["permission_prompt"]
    assert spec.capability is Capability.FILE_READ
    assert spec.input_schema["additionalProperties"] is True
    assert tool_allowed("normal", "permission_prompt") is True


def test_tool_constant_matches_the_cli_flag_value() -> None:
    assert PERMISSION_PROMPT_TOOL == CLAUDE_PERMISSION_PROMPT_TOOL == "mcp__flux__permission_prompt"


# ---------------------------------------------------------------- 2. 判定路径


async def test_allow_returns_original_input(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_approval(monkeypatch, "allowed")
    result = await permission_prompt(
        _ctx(),
        {"tool_name": "Write", "input": TOOL_INPUT, "tool_use_id": "call-1"},
    )

    assert result == {"behavior": "allow", "updatedInput": TOOL_INPUT}
    raised = captured["raise"]
    assert raised["kind"] == APPROVAL_KIND_TOOL_PROMPT
    assert raised["task_id"] == TASK_ID
    assert raised["run_id"] == RUN_ID
    assert raised["meta"]["tool_name"] == "Write"
    assert raised["meta"]["tool_use_id"] == "call-1"
    assert raised["meta"]["source"] == "claude-permission-prompt"
    assert "content" in raised["detail"]
    assert captured["wait"]["decision_id"] == "decision-1"


async def test_deny_maps_to_deny_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_approval(monkeypatch, "denied")
    result = await permission_prompt(_ctx(), {"tool_name": "Bash", "input": {"command": "ls"}})
    assert result["behavior"] == "deny"
    assert result["message"] == "用户拒绝"
    assert "updatedInput" not in result


async def test_timeout_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_approval(monkeypatch, "timeout")
    result = await permission_prompt(_ctx(), {"tool_name": "Bash", "input": {"command": "ls"}})
    assert result["behavior"] == "deny"
    assert "超时" in result["message"]


async def test_cancelled_run_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_approval(monkeypatch, "cancelled")
    result = await permission_prompt(_ctx(), {"tool_name": "Bash", "input": {"command": "ls"}})
    assert result["behavior"] == "deny"
    assert "取消" in result["message"]


async def test_bad_arguments_deny_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_approval(monkeypatch, "allowed")
    for params in (
        {},
        {"tool_name": "   ", "input": {}},
        {"tool_name": "Write", "input": "not-an-object"},
        {"tool_name": "Write"},
    ):
        result = await permission_prompt(_ctx(), params)
        assert result["behavior"] == "deny", params
    assert "raise" not in captured


async def test_unknown_run_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_approval(monkeypatch, "allowed")
    assert (await permission_prompt(_ctx(None, run_id=None), {"tool_name": "Write", "input": {}}))[
        "behavior"
    ] == "deny"
    assert (await permission_prompt(_ctx(run=None), {"tool_name": "Write", "input": {}}))[
        "behavior"
    ] == "deny"
    assert "raise" not in captured


async def test_run_without_task_or_owned_by_another_agent_denies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_approval(monkeypatch, "allowed")
    no_task = await permission_prompt(_ctx(_run(task_id=None)), {"tool_name": "Write", "input": {}})
    other_agent = await permission_prompt(
        _ctx(_run(agent_id=str(uuid.uuid4()))), {"tool_name": "Write", "input": {}}
    )
    assert no_task["behavior"] == "deny"
    assert other_agent["behavior"] == "deny"
    assert "raise" not in captured


async def test_finished_run_denies_without_raising_a_card(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_approval(monkeypatch, "allowed")
    result = await permission_prompt(
        _ctx(_run(status=DshRunStatus.CANCELLED.value)), {"tool_name": "Write", "input": {}}
    )
    assert result["behavior"] == "deny"
    assert "raise" not in captured


async def test_non_normal_modes_do_not_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_approval(monkeypatch, "allowed")
    for mode in (PermissionMode.PLAN.value, PermissionMode.FULL.value):
        result = await permission_prompt(
            _ctx(mode=mode), {"tool_name": "Write", "input": TOOL_INPUT}
        )
        assert result["behavior"] == "deny"
    assert "raise" not in captured


async def test_recursive_call_to_the_bridge_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_approval(monkeypatch, "allowed")
    result = await permission_prompt(
        _ctx(), {"tool_name": PERMISSION_PROMPT_TOOL, "input": TOOL_INPUT}
    )
    assert result["behavior"] == "deny"
    assert "raise" not in captured


async def test_internal_error_denies_and_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def boom(container: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("db down")

    monkeypatch.setattr(permission_tools, "raise_approval", boom)
    with caplog.at_level(logging.WARNING, logger="flux.core.mcp.tools.permission_tools"):
        result = await permission_prompt(_ctx(), {"tool_name": "Write", "input": TOOL_INPUT})

    assert result["behavior"] == "deny"
    assert "内部错误" in result["message"]
    assert "reason=internal_error" in caplog.text


async def test_unknown_keys_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_approval(monkeypatch, "allowed")
    result = await permission_prompt(
        _ctx(),
        {"tool_name": "Write", "input": TOOL_INPUT, "tool_use_id": "c1", "future_field": 1},
    )
    assert result["behavior"] == "allow"


# ---------------------------------------------------------------- 3. 安全与日志


async def test_preview_redacts_secret_keys_and_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_approval(monkeypatch, "allowed")

    await permission_prompt(
        _ctx(),
        {"tool_name": "Write", "input": {"password": "hunter2", "path": "a.txt"}},
    )
    write_detail = captured["raise"]["detail"]
    assert "hunter2" not in write_detail
    assert "[REDACTED]" in write_detail

    await permission_prompt(
        _ctx(),
        {
            "tool_name": "Bash",
            "input": {"command": 'curl -H "Authorization: Bearer sk-abcdefgh12345678" https://x'},
        },
    )
    bash_detail = captured["raise"]["detail"]
    assert "sk-abcdefgh12345678" not in bash_detail
    assert "[REDACTED" in bash_detail


async def test_bash_preview_reports_risk(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_approval(monkeypatch, "allowed")
    await permission_prompt(_ctx(), {"tool_name": "Bash", "input": {"command": "rm -rf build"}})
    raised = captured["raise"]
    assert raised["meta"]["risk_level"] == LEVEL_HIGH
    assert "rm -rf build" in raised["detail"]


async def test_decisions_are_logged_without_input_values(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install_approval(monkeypatch, "allowed")
    with caplog.at_level(logging.INFO, logger="flux.core.mcp.tools.permission_tools"):
        await permission_prompt(
            _ctx(),
            {"tool_name": "Write", "input": {"password": "hunter2", "path": "a.txt"}},
        )
    assert "claude_permission.allow" in caplog.text
    assert RUN_ID in caplog.text
    assert "tool=Write" in caplog.text
    assert "hunter2" not in caplog.text
    assert "a.txt" not in caplog.text


# ---------------------------------------------------------------- 4. MCP 输出形状


async def _seed_run(container: Any) -> tuple[Any, str]:
    task = await container.task_repo.create(
        task_id=uuid.uuid4(),
        description="审批桥用例",
        priority=1,
        project_id=None,
        agent_id=None,
    )
    run_id = uuid.uuid4().hex
    await container.run_repo.create(
        run_id=run_id,
        session_id="sess",
        instruction="do it",
        status=DshRunStatus.RUNNING,
        agent_id=AGENT_ID,
        task_id=task.id,
        permission_mode=PermissionMode.NORMAL.value,
    )
    return task, run_id


async def test_mcp_result_is_one_text_block_of_json(
    apply_container: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _task, run_id = await _seed_run(apply_container)
    _install_approval(monkeypatch, "allowed")
    identity = AgentIdentity(
        agent_id=AGENT_ID,
        token_id="t",
        scopes=frozenset({Capability.FILE_READ}),
        run_id=run_id,
        permission_mode=PermissionMode.NORMAL.value,
    )
    result = await McpServer(apply_container).handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "permission_prompt",
                "arguments": {"tool_name": "Write", "input": TOOL_INPUT},
            },
        },
        identity,
    )

    payload = result["result"]
    assert payload["isError"] is False
    assert "structuredContent" not in payload
    assert len(payload["content"]) == 1
    assert payload["content"][0]["type"] == "text"
    body = json.loads(payload["content"][0]["text"])
    assert body == {"behavior": "allow", "updatedInput": TOOL_INPUT}


async def test_bot_tool_whitelist_does_not_block_the_bridge(
    apply_container: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """审批桥不是 Agent 能力：Bot 工具白名单不该把它挡在门外（否则正常模式形同虚设）。"""
    _task, run_id = await _seed_run(apply_container)
    _install_approval(monkeypatch, "allowed")
    identity = AgentIdentity(
        agent_id=AGENT_ID,
        token_id="t",
        scopes=frozenset({Capability.FILE_READ}),
        run_id=run_id,
        permission_mode=PermissionMode.NORMAL.value,
        # Bot 只允许读工作区——审批桥仍应可调用，其它工具照旧被挡。
        allowed_tools=("workspace.read",),
    )
    server = McpServer(apply_container)

    bridge = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "permission_prompt",
                "arguments": {"tool_name": "Write", "input": TOOL_INPUT},
            },
        },
        identity,
    )
    assert bridge["result"]["isError"] is False

    blocked = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "workspace.tree", "arguments": {}},
        },
        identity,
    )
    assert blocked["result"]["isError"] is True


# ---------------------------------------------------------------- 5. Claude argv


def _argv_runtime() -> Any:
    return SimpleNamespace(
        runtime_id=RUNTIME_CLAUDE,
        _build_argv=lambda instruction, mode: ("claude", "--print", instruction),
    )


def _write_mcp_config(config_dir: Any, *, with_flux: bool) -> None:
    servers = {"flux": {"type": "http", "url": "http://flux.local/mcp"}} if with_flux else {}
    (config_dir / "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def _argv(config_dir: Any, mode: str) -> tuple[str, ...]:
    return CliRuntime._build_claude_argv(  # noqa: SLF001 - 直接测 argv 组装
        _argv_runtime(), "do it", mode, config_dir=config_dir
    )


def test_argv_has_prompt_tool_in_normal_mode_with_flux(tmp_path: Any) -> None:
    _write_mcp_config(tmp_path, with_flux=True)
    argv = _argv(tmp_path, PermissionMode.NORMAL.value)
    assert "--permission-prompt-tool" in argv
    assert argv[argv.index("--permission-prompt-tool") + 1] == CLAUDE_PERMISSION_PROMPT_TOOL
    assert argv[argv.index("--permission-mode") + 1] == "default"


def test_argv_omits_prompt_tool_in_other_modes(tmp_path: Any) -> None:
    _write_mcp_config(tmp_path, with_flux=True)
    for mode in (
        PermissionMode.PLAN.value,
        PermissionMode.UNATTENDED.value,
        PermissionMode.FULL.value,
    ):
        assert "--permission-prompt-tool" not in _argv(tmp_path, mode), mode


def test_argv_omits_prompt_tool_when_flux_server_is_absent(
    tmp_path: Any, caplog: pytest.LogCaptureFixture
) -> None:
    _write_mcp_config(tmp_path, with_flux=False)
    with caplog.at_level(logging.WARNING, logger="flux.core.agent_runtime.runtimes.cli"):
        argv = _argv(tmp_path, PermissionMode.NORMAL.value)
    assert "--permission-prompt-tool" not in argv
    assert "缺少 flux MCP 端点" in caplog.text


def test_argv_tolerates_missing_or_broken_mcp_json(tmp_path: Any) -> None:
    assert "--permission-prompt-tool" not in _argv(tmp_path, PermissionMode.NORMAL.value)
    (tmp_path / "mcp.json").write_text("{not json", encoding="utf-8")
    assert "--permission-prompt-tool" not in _argv(tmp_path, PermissionMode.NORMAL.value)


def test_normal_mode_raises_the_mcp_tool_timeout(tmp_path: Any) -> None:
    runtime = SimpleNamespace(
        runtime_id=RUNTIME_CLAUDE,
        _settings=SimpleNamespace(
            anthropic_api_key=None,
            anthropic_base_url=None,
            anthropic_auth_mode="auto",
        ),
        _secure=lambda path: True,
        _inject_claude_credentials=lambda config_dir: None,
    )
    env = CliRuntime._write_claude_config(  # noqa: SLF001
        runtime, tmp_path, "", None, "r1", mode=PermissionMode.NORMAL.value
    )
    assert env["MCP_TOOL_TIMEOUT"] == str((APPROVAL_TIMEOUT_SECONDS + 60) * 1000)
    assert "IS_SANDBOX" not in env

    bypass_env = CliRuntime._write_claude_config(  # noqa: SLF001
        runtime, tmp_path, "", None, "r1", mode=PermissionMode.UNATTENDED.value
    )
    assert "MCP_TOOL_TIMEOUT" not in bypass_env
    assert bypass_env["IS_SANDBOX"] == "1"
