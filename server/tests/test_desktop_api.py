"""GET /api/v1/desktop 入口参数与地址别名解析。

覆盖：configured / unconfigured（不返回半个参数集）、desktop.json 缓存与失败回退、
票据模式下长期凭据脱敏、desktop.json 优先于 env 别名，以及两套 env 别名的优先级。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from flux.config import Settings
from flux.core.desktop.resolve import resolve_desktop_endpoints
from flux.core.desktop.service import DesktopService
from flux.main import create_app

PREFIX = "/api/v1"
HOST = "https://desktop.example.test"
BRIDGE = "https://bridge.example.test"
TOKEN = "desktop-gateway-token-for-tests"
BRIDGE_KEY = "bridge-key-for-tests"
TICKET_SECRET = "desktop-ticket-secret-for-tests"
CONFIG_URL = "https://config.example.test/desktop.json"


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _client(settings: Settings, desktop: DesktopService | None = None) -> TestClient:
    app = create_app(settings)
    if desktop is not None:
        app.state.container.desktop = desktop
    return TestClient(app)


def _configured(settings: Settings, **updates: object) -> Settings:
    return settings.model_copy(
        update={
            "desktop_config_url": "",
            "desktop_host": HOST,
            "desktop_bridge": BRIDGE,
            "desktop_gateway_token": TOKEN,
            "desktop_bridge_key": BRIDGE_KEY,
            **updates,
        }
    )


def test_get_desktop_ok(settings: Settings, db_schema: None) -> None:
    with _client(_configured(settings)) as client:
        response = client.get(f"{PREFIX}/desktop")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["configured"] is True
    assert data["gateway"] == HOST
    assert data["bridge"] == BRIDGE
    assert data["token"] == TOKEN
    assert data["bridge_key"] == BRIDGE_KEY
    assert data["config_url"] == ""


def test_get_desktop_unconfigured_returns_no_half_params(
    settings: Settings, db_schema: None
) -> None:
    with _client(settings) as client:  # 夹具里桌面地址 / 凭据全为空
        response = client.get(f"{PREFIX}/desktop")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["configured"] is False
    assert data["gateway"] == ""
    assert data["bridge"] == ""
    assert data["token"] is None
    assert data["bridge_key"] is None


def test_get_desktop_redacts_long_lived_credentials_in_ticket_mode(
    settings: Settings, db_schema: None
) -> None:
    configured = _configured(
        settings,
        desktop_ticket_secret=TICKET_SECRET,
        desktop_send_gateway_token=False,
    )
    with _client(configured) as client:
        data = client.get(f"{PREFIX}/desktop").json()["data"]

    assert data["configured"] is True
    assert data["gateway"] == HOST
    assert data["bridge"] == BRIDGE
    assert data["token"] is None
    assert data["bridge_key"] is None


def test_get_desktop_configured_by_ticket_secret_only(settings: Settings, db_schema: None) -> None:
    """没有长期凭据但配置了票据密钥：客户端仍可走会话接口。"""
    configured = settings.model_copy(
        update={
            "desktop_config_url": "",
            "desktop_host": HOST,
            "desktop_bridge": BRIDGE,
            "desktop_gateway_token": None,
            "desktop_bridge_key": None,
            "desktop_ticket_secret": TICKET_SECRET,
        }
    )
    with _client(configured) as client:
        data = client.get(f"{PREFIX}/desktop").json()["data"]

    assert data["configured"] is True
    assert data["token"] is None
    assert data["bridge_key"] is None


def test_desktop_json_is_cached(settings: Settings) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"gateway": HOST, "bridge": BRIDGE, "updated": "2026-11-12T00:00:00Z"},
        )

    configured = _configured(settings, desktop_config_url=CONFIG_URL)

    async def run() -> list[dict[str, object]]:
        async with _mock_client(handler) as client:
            service = DesktopService(configured, client=client)
            first = await service.config()
            second = await service.config()
            return [first, second]

    first, second = asyncio.run(run())
    assert calls == 1
    assert first["configured"] is True
    assert first["gateway"] == HOST
    assert second["tunnel_updated_at"] == "2026-11-12T00:00:00Z"


def test_desktop_json_failure_falls_back_to_env(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("tunnel down", request=request)

    configured = _configured(
        settings,
        desktop_config_url=CONFIG_URL,
        desktop_host=None,
        desktop_gateway="https://fallback-gateway.example.test",
        desktop_bridge=None,
        desktop_bridge_url=f"{BRIDGE}/",
    )

    async def run() -> dict[str, object]:
        async with _mock_client(handler) as client:
            return await DesktopService(configured, client=client).config()

    data = asyncio.run(run())
    assert data["configured"] is True
    assert data["gateway"] == "https://fallback-gateway.example.test"
    assert data["bridge"] == BRIDGE


def test_desktop_json_wins_over_env_aliases(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"gateway": "https://rotated.example.test", "bridge": BRIDGE}
        )

    configured = _configured(settings, desktop_config_url=CONFIG_URL)

    async def run() -> dict[str, object]:
        async with _mock_client(handler) as client:
            return await DesktopService(configured, client=client).config()

    data = asyncio.run(run())
    assert data["gateway"] == "https://rotated.example.test"


def test_env_aliases_prefer_main_fields_and_ignore_blanks(settings: Settings) -> None:
    both = settings.model_copy(
        update={
            "desktop_host": HOST,
            "desktop_gateway": "https://alias.example.test",
            "desktop_bridge": BRIDGE,
            "desktop_bridge_url": "https://alias-bridge.example.test",
            "desktop_gateway_token": TOKEN,
            "desktop_token": "alias-token",
            "desktop_bridge_key": BRIDGE_KEY,
        }
    )
    resolved = resolve_desktop_endpoints(both)
    assert resolved.gateway == HOST
    assert resolved.bridge == BRIDGE
    assert resolved.gateway_token == TOKEN

    blanks = settings.model_copy(
        update={
            "desktop_host": "   ",
            "desktop_gateway": HOST,
            "desktop_bridge": "",
            "desktop_bridge_url": BRIDGE,
            "desktop_gateway_token": " ",
            "desktop_token": TOKEN,
        }
    )
    resolved = resolve_desktop_endpoints(blanks)
    assert resolved.gateway == HOST
    assert resolved.bridge == BRIDGE
    assert resolved.gateway_token == TOKEN


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
