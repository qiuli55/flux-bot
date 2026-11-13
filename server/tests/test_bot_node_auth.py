"""Bot 节点的 REST 鉴权默认值：未配置令牌时 fail-closed，只放行同机回环直连。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from flux.bot_node.main import create_app, resolve_bot_settings
from flux.config import Settings

TOKEN = "test-bot-node-token-4a7e2c"

PUBLIC_CLIENT = {"client": ("203.0.113.9", 41234)}
LOOPBACK_CLIENT = {"client": ("127.0.0.1", 41234)}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_no_token_rejects_forwarded_request(settings: Settings, db_schema: None) -> None:
    """回环 + 转发头不再算本机直连：Bot 节点默认没有令牌就 401。"""
    app = create_app(settings)
    with TestClient(app, **LOOPBACK_CLIENT) as client:
        response = client.get("/api/v1/bots", headers={"X-Forwarded-For": "1.2.3.4"})

    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "unauthenticated"
    assert "FLUX_AUTH_TOKEN" in body["message"]


def test_explicit_allow_no_auth_opens_bot_node(settings: Settings, db_schema: None) -> None:
    """FLUX_ALLOW_NO_AUTH=true 时保留旧的无鉴权行为。"""
    open_settings = settings.model_copy(update={"allow_no_auth": True})
    app = create_app(open_settings)
    with TestClient(app, **PUBLIC_CLIENT) as client:
        assert client.get("/api/v1/bots").status_code == 200


def test_configured_token_requires_correct_bearer(settings: Settings, db_schema: None) -> None:
    secured = settings.model_copy(update={"auth_token": TOKEN})
    app = create_app(secured)
    with TestClient(app, **PUBLIC_CLIENT) as client:
        assert client.get("/api/v1/bots", headers=_auth(TOKEN)).status_code == 200
        assert client.get("/api/v1/bots", headers=_auth("wrong-token")).status_code == 401


def test_health_is_public_for_docker_healthcheck(settings: Settings, db_schema: None) -> None:
    """Dockerfile 的 HEALTHCHECK 不带令牌，健康探针必须免鉴权。"""
    app = create_app(settings)
    with TestClient(app, **PUBLIC_CLIENT) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["success"] is True


def test_resolve_bot_settings_defaults_to_fail_closed(settings: Settings) -> None:
    assert resolve_bot_settings(settings).allow_no_auth is False
    explicit = settings.model_copy(update={"allow_no_auth": True})
    assert resolve_bot_settings(explicit).allow_no_auth is True
