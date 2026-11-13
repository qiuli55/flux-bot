"""Bot 节点 MCP 端点推导：默认跟随 FLUX_BOT_PORT，显式覆盖优先。"""

from __future__ import annotations

import pytest

from flux.bot_node.main import resolve_bot_settings
from flux.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """get_settings 是 lru_cache 单例：每个用例前后都清掉，避免环境变量串场。"""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_default_derives_from_bot_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLUX_MCP_URL", raising=False)
    monkeypatch.setenv("FLUX_BOT_PORT", "8611")

    settings = resolve_bot_settings(get_settings())

    assert settings.mcp_url == "http://127.0.0.1:8611/mcp"


def test_default_falls_back_to_8600_without_bot_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FLUX_MCP_URL", raising=False)
    monkeypatch.delenv("FLUX_BOT_PORT", raising=False)

    settings = resolve_bot_settings(get_settings())

    assert settings.mcp_url == "http://127.0.0.1:8600/mcp"


def test_explicit_mcp_url_wins_over_bot_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLUX_BOT_PORT", "8611")
    monkeypatch.setenv("FLUX_MCP_URL", "http://127.0.0.1:9001/mcp")

    settings = resolve_bot_settings(get_settings())

    assert settings.mcp_url == "http://127.0.0.1:9001/mcp"
