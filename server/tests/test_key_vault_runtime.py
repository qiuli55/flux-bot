"""密钥库 → Runtime 注入测试。

验证三条边界：
- codex 的 config.toml 只写 env_key 变量名，密钥不落盘；
- opencode 的含密钥配置只经 OPENCODE_CONFIG_CONTENT 传给子进程；
- dsh 把 base_url / api_key / model 真正交给 harness。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from flux.config import Settings
from flux.core.agent_runtime.dsh_client import TERMINAL_STATUSES, FluxDshClient
from flux.core.agent_runtime.runtimes.base import RUNTIME_CODEX, RUNTIME_OPENCODE, ModelChannel
from flux.core.agent_runtime.runtimes.cli import (
    CliRuntime,
    _codex_config,
    _opencode_config,
    _opencode_model_id,
)
from flux.errors import ConfigurationError
from tests.fakes import FakeDshHarnessFactory

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
        dsh_enabled=True,
        dsh_home=str(tmp_path / "dsh-home"),
        dsh_workspace=str(tmp_path / "dsh-ws"),
        dsh_mcp_enabled=False,
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


async def _wait_terminal(client: FluxDshClient, run_id: str, timeout: float = 3.0) -> object:
    deadline = time.monotonic() + timeout
    run = client.get_run(run_id)
    while run.status not in TERMINAL_STATUSES and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return run


async def test_dsh_harness_receives_key_channel(tmp_path: Path) -> None:
    factory = FakeDshHarnessFactory()
    client = FluxDshClient(_settings(tmp_path), harness_factory=factory)

    run = await client.start_run("hi", model="deepseek-v4-pro", channel=_channel())
    await _wait_terminal(client, run.run_id)

    kwargs = factory.created[0].kwargs
    assert kwargs["provider"] == "deepseek-official"
    assert kwargs["model"] == "deepseek-v4-pro"
    assert kwargs["base_url"] == "https://api.deepseek.com"
    assert kwargs["api_key"] == SECRET
    # 对外快照只暴露 key id，不暴露明文
    snapshot = run.to_dict()
    assert snapshot["model_key_id"] == _channel().key_id
    assert SECRET not in json.dumps(snapshot, ensure_ascii=False)
