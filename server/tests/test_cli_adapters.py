"""CLI Agent Adapter 测试（最终方案 §3.3）。

探测命令用注入的 `which` / `probe_runner` 替身，不依赖本机装没装；生命周期用一个
真实子进程验证"取消能清理进程组"，不 mock 掉进程语义。
"""

from __future__ import annotations

import sys

from flux.core.agent_runtime.adapters import (
    CodexAdapter,
    GenericCliAdapter,
    OpenCodeAdapter,
    build_default_adapters,
)
from flux.core.agent_runtime.protocol import RUNTIME_BOOTSTRAP
from flux.enums import AgentInstallStatus


def _probe_table(table: dict[tuple[str, ...], tuple[int, str]]):
    """按命令尾部匹配返回预设输出。"""

    def _run(argv, *, timeout: float = 10.0):  # noqa: ANN001, ARG001
        key = tuple(argv[1:])
        if key in table:
            return table[key]
        raise AssertionError(f"未预设的探测命令：{argv}")

    return _run


def _installed(version: str = "1.2.3", auth: str = "ok"):
    return _probe_table(
        {
            ("--version",): (0, version),
            ("auth", "list"): (0, "0 credentials" if auth == "missing" else "3 credentials"),
        }
    )


# --- 发现事实 ---


def test_generic_adapter_reports_not_installed_when_absent() -> None:
    adapter = GenericCliAdapter(name="ghost", executable="ghost", which=lambda _: None)
    probe = adapter.discover()
    assert probe.status is AgentInstallStatus.NOT_INSTALLED
    assert probe.path is None
    assert probe.version is None


def test_generic_adapter_verified_then_connected() -> None:
    adapter = GenericCliAdapter(
        name="x",
        executable="x",
        version_args=("--version",),
        auth_args=("auth", "list"),
        auth_ok_markers=("credentials",),
        auth_missing_markers=("0 credentials",),
        which=lambda _: "/usr/bin/x",
        probe_runner=_installed(),
    )
    # 有版本 + 已认证 → CONNECTED（READY 由 connect 推进，不由 Scanner 决定）
    probe = adapter.discover()
    assert probe.status is AgentInstallStatus.CONNECTED
    assert probe.version == "1.2.3"
    assert probe.auth_status == "ok"
    assert probe.path == "/usr/bin/x"


def test_generic_adapter_version_only_is_verified_and_auth_missing() -> None:
    adapter = GenericCliAdapter(
        name="x",
        executable="x",
        auth_args=("auth", "list"),
        auth_missing_markers=("0 credentials",),
        auth_ok_markers=("credentials",),
        which=lambda _: "/usr/bin/x",
        probe_runner=_installed(auth="missing"),
    )
    probe = adapter.discover()
    assert probe.status is AgentInstallStatus.VERIFIED
    assert probe.auth_status == "missing"


def test_generic_adapter_without_auth_command_is_unknown() -> None:
    adapter = GenericCliAdapter(
        name="x",
        executable="x",
        which=lambda _: "/usr/bin/x",
        probe_runner=_probe_table({("--version",): (0, "x 9.9.9")}),
    )
    assert adapter.check_auth() == "unknown"
    assert adapter.discover().status is AgentInstallStatus.VERIFIED


def test_version_regex_extracts_semver_from_prefixed_output() -> None:
    adapter = GenericCliAdapter(
        name="x",
        executable="x",
        which=lambda _: "/usr/bin/x",
        probe_runner=_probe_table({("--version",): (0, "codex-cli 0.157.1")}),
    )
    assert adapter.get_version() == "0.157.1"


# --- 具体 Adapter（真实行为已核对）---


def test_opencode_adapter_config() -> None:
    adapter = OpenCodeAdapter(
        which=lambda _: "/usr/local/bin/opencode", probe_runner=_installed("1.18.29")
    )
    probe = adapter.discover()
    assert probe.status is AgentInstallStatus.CONNECTED
    assert probe.version == "1.18.29"
    assert probe.capabilities == ("mcp", "stream")
    # 默认带上 Runtime Bootstrap：Flux 托管启动的 Agent 要知道改动走 proposal（§8.2）
    argv = adapter.build_run_argv("修一下")
    assert argv[:2] == ("/usr/local/bin/opencode", "run")
    assert argv[2].startswith(RUNTIME_BOOTSTRAP)
    assert argv[2].endswith("修一下")
    # 批次①验收：Bootstrap 是"第一份强制上下文"，四条硬规则必须在文案里
    for marker in ("flux_context", "context.get", "proposal.create", "flux tools call"):
        assert marker in RUNTIME_BOOTSTRAP, marker
    # 显式关掉时保持纯指令，便于需要原文的调用方
    assert adapter.build_run_argv("修一下", bootstrap=False) == (
        "/usr/local/bin/opencode",
        "run",
        "修一下",
    )


def test_codex_adapter_auth_markers() -> None:
    def _run(argv, *, timeout: float = 10.0):  # noqa: ANN001, ARG001
        if tuple(argv[1:]) == ("--version",):
            return 0, "codex-cli 0.157.1"
        if tuple(argv[1:]) == ("login", "status"):
            return 0, "Not logged in"
        raise AssertionError(argv)

    adapter = CodexAdapter(which=lambda _: "/usr/bin/codex", probe_runner=_run)
    probe = adapter.discover()
    assert probe.status is AgentInstallStatus.VERIFIED
    assert probe.auth_status == "missing"
    assert probe.version == "0.157.1"


# --- 事件解析 / 握手 ---


def test_parse_event_normalizes_lines() -> None:
    adapter = GenericCliAdapter(name="x", executable="x")
    assert adapter.parse_event("") is None
    assert adapter.parse_event('{"type": "delta", "text": "hi"}') == {
        "type": "delta",
        "text": "hi",
    }
    # 非 JSON 行带 non_json 标记：runtime 据此决定去向（codex 侧只当状态，不进答复）
    assert adapter.parse_event("plain log line") == {
        "type": "text",
        "text": "plain log line",
        "non_json": True,
    }


def test_handshake_carries_protocol_and_capabilities() -> None:
    adapter = OpenCodeAdapter(
        which=lambda _: "/usr/local/bin/opencode", probe_runner=_installed("1.18.29")
    )
    reply = adapter.handshake()
    assert reply["protocol"] == "flux-agent"
    assert reply["version"] == "1"
    assert reply["agent"]["name"] == "opencode"
    assert reply["agent"]["version"] == "1.18.29"
    assert reply["capabilities"] == ["mcp", "stream"]


# --- 生命周期 ---


def test_start_then_stop_cleans_process_group() -> None:
    adapter = GenericCliAdapter(name="x", executable=sys.executable)
    process = adapter.start([sys.executable, "-c", "import time; time.sleep(30)"])
    assert process.pid > 0
    assert process.pgid == process.pid  # start_new_session ⇒ 自成进程组
    adapter.stop(process)
    assert process.popen.poll() is not None  # 已退出，不留孤儿


def test_default_registry_has_opencode_and_codex() -> None:
    names = {adapter.name for adapter in build_default_adapters()}
    assert names == {"opencode", "codex"}
