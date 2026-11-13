"""Claude Code 鉴权变量解析：官方直连 vs LLM 网关。

官方口径（code.claude.com/docs/en/env-vars，[2026-11-12 快照]）：
`ANTHROPIC_API_KEY` 走 X-Api-Key，`ANTHROPIC_AUTH_TOKEN` 走 Authorization: Bearer，
两者互斥。这里覆盖 auto 判定、强制模式、非法值、空 key、父进程变量的继承与裁剪，
以及 `_write_claude_config` / Run 启动环境的集成路径。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from flux.config import Settings
from flux.core.agent_runtime.claude_auth import (
    API_KEY_VAR,
    AUTH_TOKEN_VAR,
    claude_auth_env,
    is_official_base_url,
    normalize_auth_mode,
    resolve_auth_mode,
    trim_inherited_auth_env,
)
from flux.core.agent_runtime.runtimes.base import RUNTIME_CLAUDE, ModelChannel
from flux.core.agent_runtime.runtimes.cli import CliRuntime

SECRET = "sk-flux-secret-123456"
OFFICIAL = "https://api.anthropic.com"
GATEWAY = "https://opencode.ai/inference/go/anthropic"


def _runtime(tmp_path: Path, **overrides: object) -> CliRuntime:
    values: dict[str, object] = {
        "env": "test",
        "log_level": "WARNING",
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}",
        "anthropic_api_key": None,
        "anthropic_base_url": "",
        "runtime_home": str(tmp_path / "runtime-home"),
        "runtime_workspace": str(tmp_path / "runtime-ws"),
        "mcp_enabled": False,
    }
    values.update(overrides)
    return CliRuntime(
        runtime_id=RUNTIME_CLAUDE,
        adapter=SimpleNamespace(_path="/usr/local/bin/claude"),
        settings=Settings(**values),  # type: ignore[arg-type]
        supervisor=None,
    )


def _channel(base_url: str) -> ModelChannel:
    return ModelChannel(provider="anthropic", base_url=base_url, api_key=SECRET)


# --- 纯解析 ---------------------------------------------------------------


def test_auto_without_base_url_uses_api_key() -> None:
    assert claude_auth_env(SECRET, "", "auto") == {API_KEY_VAR: SECRET}
    assert claude_auth_env(SECRET, None, "auto") == {API_KEY_VAR: SECRET}
    assert claude_auth_env(SECRET, "   ", "auto") == {API_KEY_VAR: SECRET}


@pytest.mark.parametrize(
    "url",
    [
        OFFICIAL,
        "https://api.anthropic.com/v1",
        "https://anthropic.com",
        "https://foo.anthropic.com",
        "api.anthropic.com",
    ],
)
def test_auto_with_official_hosts_uses_api_key(url: str) -> None:
    assert is_official_base_url(url) is True
    assert claude_auth_env(SECRET, url, "auto") == {API_KEY_VAR: SECRET}


@pytest.mark.parametrize(
    "url",
    [
        GATEWAY,
        "https://openclaw.local/anthropic",
        "http://127.0.0.1:9355/v1",
        "notanthropic.com",
    ],
)
def test_auto_with_gateway_uses_auth_token(url: str) -> None:
    assert is_official_base_url(url) is False
    env = claude_auth_env(SECRET, url, "auto")
    assert env == {AUTH_TOKEN_VAR: SECRET}
    assert API_KEY_VAR not in env


def test_forced_modes_override_base_url() -> None:
    assert claude_auth_env(SECRET, GATEWAY, "api_key") == {API_KEY_VAR: SECRET}
    assert claude_auth_env(SECRET, OFFICIAL, "auth_token") == {AUTH_TOKEN_VAR: SECRET}


def test_invalid_mode_warns_and_falls_back_to_auto(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert claude_auth_env(SECRET, GATEWAY, "bearer") == {AUTH_TOKEN_VAR: SECRET}
    assert "按 auto 处理" in caplog.text
    # auto 语义仍然生效：官方地址回到 API Key
    assert claude_auth_env(SECRET, OFFICIAL, "bogus") == {API_KEY_VAR: SECRET}


def test_normalize_auth_mode_accepts_empty_and_case() -> None:
    assert normalize_auth_mode("") == "auto"
    assert normalize_auth_mode(None) == "auto"
    assert normalize_auth_mode(" AUTH_TOKEN ") == "auth_token"


def test_empty_key_sets_neither_variable() -> None:
    for key in (None, "", "   "):
        assert claude_auth_env(key, GATEWAY, "auth_token") == {}
        assert claude_auth_env(key, OFFICIAL, "auto") == {}
    assert resolve_auth_mode(GATEWAY, "auto") == "auth_token"


def test_injected_var_drops_only_the_opposite_inherited_one() -> None:
    """Flux 注入 API Key：只摘继承来的 AUTH_TOKEN，同名继承值留给后续覆盖。"""
    env = {API_KEY_VAR: "parent-key", AUTH_TOKEN_VAR: "parent-token", "PATH": "/bin"}
    removed = trim_inherited_auth_env(env, injected={API_KEY_VAR: SECRET})
    assert removed == [AUTH_TOKEN_VAR]
    assert env == {API_KEY_VAR: "parent-key", "PATH": "/bin"}


def test_injected_var_drops_nothing_when_no_opposite_inherited() -> None:
    env = {API_KEY_VAR: "parent-key", "PATH": "/bin"}
    assert trim_inherited_auth_env(env, injected={API_KEY_VAR: SECRET}) == []
    assert env == {API_KEY_VAR: "parent-key", "PATH": "/bin"}


@pytest.mark.parametrize("name", [API_KEY_VAR, AUTH_TOKEN_VAR])
def test_no_injection_keeps_a_single_inherited_var(name: str) -> None:
    """向后兼容：Flux 不下发密钥时，父进程自己配的那一套原样继承。"""
    env = {name: "parent-value", "PATH": "/bin"}
    assert trim_inherited_auth_env(env, injected={}, base_url=GATEWAY, mode="auto") == []
    assert env == {name: "parent-value", "PATH": "/bin"}


def test_no_injection_keeps_resolved_var_when_both_inherited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = {API_KEY_VAR: "parent-key", AUTH_TOKEN_VAR: "parent-token", "PATH": "/bin"}
    with caplog.at_level(logging.WARNING):
        removed = trim_inherited_auth_env(env, injected={}, base_url=GATEWAY, mode="auto")
    # 网关（非官方）按 auto 解析成 Bearer
    assert removed == [API_KEY_VAR]
    assert env == {AUTH_TOKEN_VAR: "parent-token", "PATH": "/bin"}
    assert API_KEY_VAR in caplog.text
    assert "parent-key" not in caplog.text
    assert "parent-token" not in caplog.text


def test_no_injection_respects_forced_mode_when_both_inherited() -> None:
    env = {API_KEY_VAR: "parent-key", AUTH_TOKEN_VAR: "parent-token"}
    removed = trim_inherited_auth_env(env, injected={}, base_url=GATEWAY, mode="api_key")
    assert removed == [AUTH_TOKEN_VAR]
    assert env == {API_KEY_VAR: "parent-key"}


def test_trim_inherited_is_noop_when_absent() -> None:
    env = {"PATH": "/bin"}
    assert trim_inherited_auth_env(env, injected={}) == []
    assert env == {"PATH": "/bin"}


# --- 配置写入集成 ---------------------------------------------------------


def test_config_official_direct_connection_keeps_api_key(tmp_path: Path) -> None:
    """向后兼容：官方直连（无自定义 base_url）仍注入 ANTHROPIC_API_KEY。"""
    runtime = _runtime(
        tmp_path,
        anthropic_api_key=SECRET,
        anthropic_base_url=OFFICIAL,
    )
    env = CliRuntime._write_runtime_config(  # noqa: SLF001
        runtime, tmp_path / "official", "tok", "r1", task_id="task-1"
    )
    assert env[API_KEY_VAR] == SECRET
    assert AUTH_TOKEN_VAR not in env
    assert env["ANTHROPIC_BASE_URL"] == OFFICIAL


def test_config_gateway_uses_auth_token(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        anthropic_api_key=SECRET,
        anthropic_base_url=GATEWAY,
    )
    env = CliRuntime._write_runtime_config(  # noqa: SLF001
        runtime, tmp_path / "gateway", "tok", "r1", task_id="task-1"
    )
    assert env[AUTH_TOKEN_VAR] == SECRET
    assert API_KEY_VAR not in env
    assert env["ANTHROPIC_BASE_URL"] == GATEWAY


def test_config_forced_mode_wins_over_official_host(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        anthropic_api_key=SECRET,
        anthropic_base_url=OFFICIAL,
        anthropic_auth_mode="auth_token",
    )
    env = CliRuntime._write_runtime_config(  # noqa: SLF001
        runtime, tmp_path / "forced", "tok", "r1", task_id="task-1"
    )
    assert env[AUTH_TOKEN_VAR] == SECRET
    assert API_KEY_VAR not in env


def test_config_logs_mode_but_never_the_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = _runtime(tmp_path, anthropic_api_key=SECRET, anthropic_base_url=GATEWAY)
    with caplog.at_level(logging.INFO):
        CliRuntime._write_runtime_config(  # noqa: SLF001
            runtime, tmp_path / "logged", "tok", "r1", task_id="task-1"
        )
    assert "mode=auth_token" in caplog.text
    assert SECRET not in caplog.text


def test_config_empty_key_keeps_login_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path, anthropic_api_key=None, anthropic_base_url=GATEWAY)
    injected: list[Path] = []
    monkeypatch.setattr(runtime, "_inject_claude_credentials", injected.append)
    env = CliRuntime._write_runtime_config(  # noqa: SLF001
        runtime, tmp_path / "nolkey", "tok", "r1", task_id="task-1"
    )
    assert API_KEY_VAR not in env
    assert AUTH_TOKEN_VAR not in env
    assert injected == [tmp_path / "nolkey"]


def test_settings_invalid_mode_warns_and_normalizes(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        settings = Settings(env="test", anthropic_auth_mode="bearer")
    assert settings.anthropic_auth_mode == "auto"
    assert "按 auto 处理" in caplog.text


# --- Run 启动环境集成 -----------------------------------------------------


def test_prepare_removes_inherited_opposite_auth_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """父进程残留的 AUTH_TOKEN 不能传给子进程：Flux 已选 API Key。"""
    monkeypatch.setenv(API_KEY_VAR, "parent-key")
    monkeypatch.setenv(AUTH_TOKEN_VAR, "parent-token")
    runtime = _runtime(tmp_path, anthropic_api_key=SECRET, anthropic_base_url=OFFICIAL)
    launch = asyncio.run(
        runtime.prepare(
            task=SimpleNamespace(id="t1"),
            agent=SimpleNamespace(id_str="a1"),
            run_id="r1",
            instruction="hi",
        )
    )
    assert launch.env[API_KEY_VAR] == SECRET
    assert AUTH_TOKEN_VAR not in launch.env


def test_prepare_gateway_channel_injects_only_bearer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(API_KEY_VAR, "parent-key")
    runtime = _runtime(tmp_path, anthropic_api_key=None, anthropic_base_url="")
    launch = asyncio.run(
        runtime.prepare(
            task=SimpleNamespace(id="t1"),
            agent=SimpleNamespace(id_str="a1"),
            run_id="r1",
            instruction="hi",
            channel=_channel(GATEWAY),
        )
    )
    assert launch.env[AUTH_TOKEN_VAR] == SECRET
    assert API_KEY_VAR not in launch.env
    assert launch.env["ANTHROPIC_BASE_URL"] == GATEWAY


def test_prepare_without_key_keeps_single_inherited_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有密钥通道时回到 claude login，但父进程自己配的单一变量要原样保留。"""
    monkeypatch.delenv(API_KEY_VAR, raising=False)
    monkeypatch.delenv(AUTH_TOKEN_VAR, raising=False)
    monkeypatch.setenv(API_KEY_VAR, "parent-key")
    runtime = _runtime(tmp_path, anthropic_api_key=None, anthropic_base_url="")
    monkeypatch.setattr(runtime, "_inject_claude_credentials", lambda _dir: None)
    launch = asyncio.run(
        runtime.prepare(
            task=SimpleNamespace(id="t1"),
            agent=SimpleNamespace(id_str="a1"),
            run_id="r1",
            instruction="hi",
        )
    )
    assert launch.env[API_KEY_VAR] == "parent-key"
    assert AUTH_TOKEN_VAR not in launch.env


def test_prepare_without_key_keeps_resolved_var_when_both_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """两套同时继承又没有注入通道：按 base_url 解析留一个，并告警点名丢弃的变量。"""
    monkeypatch.setenv(API_KEY_VAR, "parent-key")
    monkeypatch.setenv(AUTH_TOKEN_VAR, "parent-token")
    runtime = _runtime(tmp_path, anthropic_api_key=None, anthropic_base_url=GATEWAY)
    monkeypatch.setattr(runtime, "_inject_claude_credentials", lambda _dir: None)
    with caplog.at_level(logging.WARNING):
        launch = asyncio.run(
            runtime.prepare(
                task=SimpleNamespace(id="t1"),
                agent=SimpleNamespace(id_str="a1"),
                run_id="r1",
                instruction="hi",
            )
        )
    # 网关地址按 auto 解析成 Bearer
    assert launch.env[AUTH_TOKEN_VAR] == "parent-token"
    assert API_KEY_VAR not in launch.env
    assert API_KEY_VAR in caplog.text
    assert "parent-key" not in caplog.text
    assert "parent-token" not in caplog.text
