"""密钥库 → Runtime 注入测试。

验证三条边界：
- codex 的 config.toml 只写 env_key 变量名，密钥不落盘；
- opencode 的含密钥配置只经 OPENCODE_CONFIG_CONTENT 传给子进程；
- claude-code 的密钥与网关地址只进子进程环境，MCP 配置文件里不出现明文。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from flux.config import Settings
from flux.core.agent_runtime.runtimes.base import (
    RUNTIME_CLAUDE,
    RUNTIME_CODEX,
    RUNTIME_OPENCODE,
    ModelChannel,
)
from flux.core.agent_runtime.runtimes.cli import (
    CliRuntime,
    _codex_config,
    _opencode_config,
    _opencode_model_id,
)
from flux.errors import ConfigurationError

SECRET = "sk-flux-secret-123456"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}",
        openai_api_key=None,
        anthropic_api_key=None,
        deepseek_api_key=None,
        default_provider="local",
        runtime_home=str(tmp_path / "runtime-home"),
        runtime_workspace=str(tmp_path / "runtime-ws"),
        mcp_enabled=False,
    )


def _channel(**overrides: object) -> ModelChannel:
    values = {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "api_key": SECRET,
        "wire_api": "",
        "label": "DSK 官方",
        "key_id": "abcd1234-5678-90ab-cdef-1234567890ab",
    }
    values.update(overrides)
    return ModelChannel(**values)  # type: ignore[arg-type]


def test_codex_config_writes_env_key_name_not_the_secret(tmp_path: Path) -> None:
    channel = _channel()
    runtime = SimpleNamespace(
        runtime_id=RUNTIME_CODEX,
        _settings=_settings(tmp_path),
        _secure=lambda path: True,
    )
    env = CliRuntime._write_runtime_config(
        runtime, tmp_path, "tok", "r1", model="deepseek-v4-pro", channel=channel
    )
    config = (tmp_path / "codex-home" / "config.toml").read_text(encoding="utf-8")

    assert 'model_provider = "fluxkeyabcd1234"' in config
    assert "[model_providers.fluxkeyabcd1234]" in config
    assert 'env_key = "FLUX_MODEL_API_KEY"' in config
    assert 'wire_api = "responses"' in config
    assert SECRET not in config
    assert env["FLUX_MODEL_API_KEY"] == SECRET


def test_codex_config_rejects_anthropic_keys() -> None:
    with pytest.raises(ConfigurationError):
        _codex_config(
            "http://127.0.0.1:8801/mcp",
            "tok",
            settings=Settings(env="test"),
            model="claude-x",
            channel=_channel(provider="anthropic"),
        )


def test_opencode_config_keeps_secret_out_of_the_run_file(tmp_path: Path) -> None:
    channel = _channel(provider="openai", base_url="https://api.openai.com/v1")
    runtime = SimpleNamespace(
        runtime_id=RUNTIME_OPENCODE,
        _settings=_settings(tmp_path),
        _secure=lambda path: True,
    )
    env = CliRuntime._write_runtime_config(
        runtime, tmp_path, "tok", "r1", model="gpt-5.5", channel=channel
    )
    file_config = (tmp_path / "opencode.json").read_text(encoding="utf-8")
    content = json.loads(env["OPENCODE_CONFIG_CONTENT"])

    assert SECRET not in file_config
    assert SECRET not in env["OPENCODE_CONFIG"]
    assert content["model"] == "fluxkeyabcd1234/gpt-5.5"
    assert content["provider"]["fluxkeyabcd1234"]["options"]["apiKey"] == SECRET


def test_opencode_config_without_channel_keeps_native_model() -> None:
    payload = json.loads(
        _opencode_config("http://x/mcp", "tok", "r1", model="deepseek/deepseek-flash")
    )
    assert payload["model"] == "deepseek/deepseek-flash"
    assert "provider" not in payload


def test_opencode_model_id_prefixes_the_key_provider() -> None:
    """密钥库通道下任务只记裸模型名，opencode 侧必须补上 provider 前缀。

    不补的话 `POST /session` 解析不出 provider，opencode 会静默退回会话默认模型，
    表现为「选了密钥库模型却还在跑全局默认」。
    """
    channel = _channel(provider="openai_compatible", base_url="http://127.0.0.1:9355/v1")
    assert _opencode_model_id("proxy-deepseek-v4.1-flash", channel) == (
        "fluxkeyabcd1234/proxy-deepseek-v4.1-flash"
    )
    # 原生模型本身带前缀，不能被二次拼接
    assert _opencode_model_id("deepseek/deepseek-flash", channel) == "deepseek/deepseek-flash"
    assert _opencode_model_id("deepseek/deepseek-flash", None) == "deepseek/deepseek-flash"
    assert _opencode_model_id("", channel) == ""


def test_claude_config_keeps_key_out_of_the_mcp_file(tmp_path: Path) -> None:
    """密钥通道的 api_key / base_url 只进子进程环境；MCP 配置只有一次性令牌。"""
    channel = _channel(provider="anthropic", base_url="https://openclaw.local/anthropic")
    runtime = SimpleNamespace(
        runtime_id=RUNTIME_CLAUDE,
        _settings=_settings(tmp_path),
        _secure=lambda path: True,
        _inject_claude_credentials=lambda config_dir: None,
    )
    # claude-code 的配置写入是实例方法：把真实实现绑到替身上，避免复制一份逻辑
    runtime._write_claude_config = MethodType(  # type: ignore[attr-defined]
        CliRuntime._write_claude_config,  # noqa: SLF001
        runtime,
    )
    env = CliRuntime._write_runtime_config(
        runtime,
        tmp_path,
        "tok",
        "r1",
        model="claude-sonnet-5-5",
        channel=channel,
        mode="unattended",
    )

    mcp = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["flux"]["headers"]["Authorization"] == "Bearer tok"
    assert SECRET not in (tmp_path / "mcp.json").read_text(encoding="utf-8")

    # 非官方网关：官方文档口径是 Bearer（ANTHROPIC_AUTH_TOKEN），不能下发 X-Api-Key 变量
    assert env["ANTHROPIC_AUTH_TOKEN"] == SECRET
    assert "ANTHROPIC_API_KEY" not in env
    assert env["ANTHROPIC_BASE_URL"] == "https://openclaw.local/anthropic"
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path)
    # 容器内 root + bypassPermissions 必须显式声明沙箱环境，否则 CLI 拒绝启动
    assert env["IS_SANDBOX"] == "1"
