"""REST 面单用户令牌鉴权（`flux.api.auth`）。

三条规则各有一组用例：未配置 = 全放行；配置后 = 带对令牌放行 / 不带或带错 401；
回环 + 无转发头 = 免认证（桌面端与本地浏览器），但**带转发头就不免**
（这条正是 nginx 反代必须转发 XFF 的守护用例）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from flux.config import Settings
from flux.main import create_app

TOKEN = "test-token-3f9c1d7a"

#: TestClient 默认的 client.host 是 "testclient"（非回环），因此默认就落在"需要认证"的分支。
PUBLIC_CLIENT = {"client": ("203.0.113.9", 41234)}
LOOPBACK_CLIENT = {"client": ("127.0.0.1", 41234)}


@pytest.fixture()
def secured_app_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"auth_token": TOKEN})


@pytest.fixture()
def secured_apply_settings(apply_settings: Settings) -> Settings:
    """带工作区根的加令牌配置。

    Human PTY 会话创建要求已配置 workspace_root（未配置即 422，见
    doc/HUMAN_TERMINAL_PTY.md），所以 PTY 的 WebSocket 鉴权用例必须在
    `apply_settings` 基础上加令牌，不能用裸 `settings`。
    """
    return apply_settings.model_copy(update={"auth_token": TOKEN})


@pytest.fixture()
def secured_client(secured_app_settings: Settings, db_schema: None) -> Iterator[TestClient]:
    app = create_app(secured_app_settings)
    with TestClient(app, **PUBLIC_CLIENT) as test_client:
        yield test_client


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_without_token_configuration_everything_is_open(client: TestClient) -> None:
    """未配置 FLUX_AUTH_TOKEN：本地开发 / 桌面端零配置（回归保护）。"""
    assert client.get("/api/v1/health").status_code == 200


def test_health_requires_token_when_configured(secured_client: TestClient) -> None:
    response = secured_client.get("/api/v1/health")
    assert response.status_code == 401
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "unauthenticated"


def test_auth_cookie_passes(secured_client: TestClient) -> None:
    response = secured_client.get("/api/v1/health", cookies={"flux_auth_token": TOKEN})
    assert response.status_code == 200


def test_valid_token_passes(secured_client: TestClient) -> None:
    response = secured_client.get("/api/v1/health", headers=_auth(TOKEN))
    assert response.status_code == 200
    assert response.json()["success"] is True


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-header"),
        pytest.param(_auth("wrong-token"), id="wrong-token"),
        pytest.param(_auth(TOKEN + "x"), id="token-prefix-plus-junk"),
        pytest.param({"Authorization": TOKEN}, id="missing-bearer-prefix"),
        pytest.param({"Authorization": f"Basic {TOKEN}"}, id="wrong-scheme"),
        pytest.param({"Authorization": "Bearer "}, id="empty-token"),
    ],
)
def test_bad_credentials_are_rejected(secured_client: TestClient, headers: dict[str, str]) -> None:
    """拒绝原因不区分（fail-closed），统一 unauthenticated。"""
    response = secured_client.get("/api/v1/health", headers=headers)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


def test_write_endpoints_are_protected_too(secured_client: TestClient) -> None:
    """不只是探活接口：真正能执行命令 / 落盘的接口同样被拦住。"""
    for path, payload in (
        ("/api/v1/terminal/sessions", {"agent_id": "x", "cwd": "/tmp"}),
        ("/api/v1/workspace/apply", {"change_ids": ["x"]}),
        ("/api/v1/git/commit", {"message": "x"}),
    ):
        response = secured_client.post(path, json=payload)
        assert response.status_code == 401, path
        assert response.json()["code"] == "unauthenticated"


def test_loopback_without_forward_headers_is_trusted(
    secured_app_settings: Settings, db_schema: None
) -> None:
    """同机直连（本机浏览器 / 桌面端反代）：不带令牌也放行。"""
    app = create_app(secured_app_settings)
    with TestClient(app, **LOOPBACK_CLIENT) as test_client:
        assert test_client.get("/api/v1/health").status_code == 200


@pytest.mark.parametrize(
    "header",
    [
        "X-Forwarded-For",
        "X-Real-IP",
        "Forwarded",
    ],
)
def test_loopback_with_forward_headers_must_authenticate(
    secured_app_settings: Settings, db_schema: None, header: str
) -> None:
    """关键约束：经反代进来的请求必须带令牌。

    nginx 若忘记 `proxy_set_header X-Forwarded-For`，公网请求会伪装成"回环直连"
    从而绕过鉴权——本用例把这个配置约束钉死在代码层面。
    """
    app = create_app(secured_app_settings)
    with TestClient(app, **LOOPBACK_CLIENT) as test_client:
        assert test_client.get("/api/v1/health", headers={header: "203.0.113.9"}).status_code == 401
        assert (
            test_client.get(
                "/api/v1/health", headers={header: "203.0.113.9", **_auth(TOKEN)}
            ).status_code
            == 200
        )


def test_docs_stay_public(secured_client: TestClient) -> None:
    """/docs 与 /openapi.json 在 /api/v1 之外：不泄密但也不该被令牌挡住（要留 Authorize 按钮）。"""
    assert secured_client.get("/openapi.json").status_code == 200
    assert secured_client.get("/docs").status_code == 200


def test_openapi_declares_bearer_scheme(secured_client: TestClient) -> None:
    """OpenAPI 里必须声明 bearerAuth，/docs 才会出现 Authorize 按钮。"""
    schema = secured_client.get("/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]
    assert schemes["HTTPBearer"]["scheme"] == "bearer"
    assert schema["paths"]["/api/v1/health"]["get"]["security"] == [{"HTTPBearer": []}]


@pytest.mark.skipif(os.name == "nt", reason="native PTY is not enabled on Windows")
def test_human_websocket_requires_auth_when_rest_is_secured(
    secured_apply_settings: Settings, db_schema: None
) -> None:
    app = create_app(secured_apply_settings)
    with TestClient(app, **PUBLIC_CLIENT) as test_client:
        created = test_client.post(
            "/api/v1/terminal/pty/sessions",
            headers=_auth(TOKEN),
        )
        assert created.status_code == 200
        session_id = created.json()["data"]["id"]

        ws_path = f"/api/v1/terminal/pty/sessions/{session_id}/ws"
        with pytest.raises(WebSocketDisconnect) as exc, test_client.websocket_connect(ws_path):
            pass
        assert exc.value.code == 1008


@pytest.mark.skipif(os.name == "nt", reason="native PTY is not enabled on Windows")
def test_human_websocket_accepts_auth_header_when_rest_is_secured(
    secured_apply_settings: Settings, db_schema: None
) -> None:
    app = create_app(secured_apply_settings)
    with TestClient(app, **PUBLIC_CLIENT) as test_client:
        created = test_client.post(
            "/api/v1/terminal/pty/sessions",
            headers=_auth(TOKEN),
        )
        assert created.status_code == 200
        session_id = created.json()["data"]["id"]

        with test_client.websocket_connect(
            f"/api/v1/terminal/pty/sessions/{session_id}/ws",
            headers=_auth(TOKEN),
        ) as websocket:
            websocket.send_json({"type": "stop", "force": True})
