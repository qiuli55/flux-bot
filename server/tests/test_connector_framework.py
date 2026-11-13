"""账号侧连接器框架测试：catalog / 安装 / 卸载 / 账号隔离。

GitHub 的上游一律用 httpx.MockTransport 顶替，不访问真实网络；api_key 路径用
测试内注册的假连接器定义覆盖，不引入任何真实的第三方服务。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
from fastapi.testclient import TestClient

from flux.config import Settings
from flux.main import create_app
from tests.cloud_jwt import mint_cloud_token

PREFIX = "/api/v1"
ACCOUNT_A = "account-a"
ACCOUNT_B = "account-b"
API_KEY = "sk-test-secret-value"

GITHUB_ROW_KEYS = {
    "id",
    "name",
    "description",
    "connected",
    "account",
    "display_name",
    "avatar_url",
    "scopes",
    "connected_at",
    "updated_at",
}


def _github_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/login/device/code":
        return httpx.Response(
            200,
            json={
                "device_code": "device-code-1",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device",
                "verification_uri_complete": "https://github.com/login/device?user_code=ABCD-EFGH",
                "expires_in": 900,
                "interval": 5,
            },
        )
    if path == "/login/oauth/access_token":
        return httpx.Response(
            200,
            json={
                "access_token": "gho_super_secret_token",
                "scope": "repo read:user",
                "token_type": "bearer",
            },
        )
    if path == "/user":
        return httpx.Response(
            200,
            json={
                "login": "octocat",
                "name": "The Octocat",
                "avatar_url": "https://avatars.example/octocat.png",
            },
        )
    return httpx.Response(404, json={"message": "unexpected upstream path"})


def _client(
    settings: Settings,
    db_schema: None,
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> TestClient:
    app = create_app(settings)
    if handler is not None:
        app.state.container.github._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
    return TestClient(app)


def _headers(account: str) -> dict[str, str]:
    """Phase 2：账号身份来自云 JWT（sub）。"""
    return {"Authorization": f"Bearer {mint_cloud_token(account)}"}


def _row(client: TestClient, account: str, connector_id: str = "github") -> dict:
    rows = client.get(f"{PREFIX}/connectors", headers=_headers(account)).json()["data"]
    return next(row for row in rows if row["id"] == connector_id)


def _register_fake_api_key_connector(client: TestClient, connector_id: str = "acme") -> None:
    """测试内注册一个假的 api_key 类定义，避免依赖生产目录里的具体连接器。"""
    from flux.core.connectors.framework import ConnectorDefinition

    client.app.state.container.connector_framework.register(
        ConnectorDefinition(
            id=connector_id,
            name="Acme",
            description="测试用 API Key 连接器",
            auth_types=("api_key",),
            scopes=("read",),
        )
    )


# --- catalog ---


def test_catalog_lists_github_email_and_calendar_by_default(
    settings: Settings, db_schema: None
) -> None:
    with _client(settings, db_schema) as client:
        response = client.get(f"{PREFIX}/connectors/catalog")

    assert response.status_code == 200, response.text
    entries = response.json()["data"]
    by_id = {entry["id"]: entry for entry in entries}
    assert set(by_id) == {"github", "email", "calendar"}
    assert by_id["github"] == {
        "id": "github",
        "name": "GitHub",
        "description": "代码仓库、Issue、PR 与 Actions",
        "auth_types": ["oauth"],
        "scopes": ["repo", "read:user"],
    }
    email_entry = by_id["email"]
    assert email_entry["name"] == "Email (IMAP/SMTP)"
    assert email_entry["auth_types"] == ["api_key"]
    assert email_entry["scopes"] == ["mail.read", "mail.send"]
    assert "IMAP" in email_entry["description"]
    assert "SMTP" in email_entry["description"]
    assert by_id["calendar"] == {
        "id": "calendar",
        "name": "Calendar (CalDAV / ICS)",
        "description": "读取已发布的 ICS 订阅或 CalDAV 日历，查看事件并在 CalDAV 上新建事件",
        "auth_types": ["api_key"],
        "scopes": ["calendar.read", "calendar.write"],
    }
    assert len(entries) == 3


def test_catalog_does_not_include_account_data(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema) as client:
        client.get(f"{PREFIX}/connectors/catalog", headers=_headers(ACCOUNT_A))
        entries = client.get(f"{PREFIX}/connectors/catalog", headers=_headers(ACCOUNT_B)).json()[
            "data"
        ]

    by_id = {entry["id"]: entry for entry in entries}
    assert set(by_id) == {"github", "email", "calendar"}
    for entry in entries:
        assert "account" not in entry
        assert "connected" not in entry


# --- GitHub 既有流程 ---


def test_github_login_flow_and_list_shape(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema, _github_handler) as client:
        started = client.post(
            f"{PREFIX}/connectors/github/login/start", headers=_headers(ACCOUNT_A)
        )
        assert started.status_code == 200, started.text
        flow = started.json()["data"]
        assert flow["user_code"] == "ABCD-EFGH"
        assert flow["verification_uri"].startswith("https://github.com/")

        polled = client.get(
            f"{PREFIX}/connectors/github/login/{flow['flow_id']}",
            headers=_headers(ACCOUNT_A),
        )
        assert polled.status_code == 200, polled.text
        assert polled.json()["data"]["status"] == "success"

        listed = client.get(f"{PREFIX}/connectors", headers=_headers(ACCOUNT_A))
        assert listed.status_code == 200, listed.text
        rows = listed.json()["data"]
        assert {row["id"] for row in rows} == {"github", "email", "calendar"}
        row = next(item for item in rows if item["id"] == "github")
        assert set(row) >= GITHUB_ROW_KEYS
        assert row["connected"] is True
        assert row["account"] == "octocat"
        assert row["display_name"] == "The Octocat"
        assert row["scopes"] == ["repo", "read:user"]
        email_row = next(item for item in rows if item["id"] == "email")
        assert email_row["connected"] is False
        assert email_row["account"] == ""
        calendar_row = next(item for item in rows if item["id"] == "calendar")
        assert calendar_row["connected"] is False
        assert calendar_row["account"] == ""
        # 令牌绝不出现在响应里
        assert "gho_super_secret_token" not in listed.text

        catalog = client.get(f"{PREFIX}/connectors/catalog").json()["data"]
        github_entry = next(item for item in catalog if item["id"] == "github")
        assert github_entry["scopes"] == ["repo", "read:user"]


def test_github_state_is_isolated_between_accounts(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema, _github_handler) as client:
        flow_id = client.post(
            f"{PREFIX}/connectors/github/login/start", headers=_headers(ACCOUNT_A)
        ).json()["data"]["flow_id"]
        client.get(f"{PREFIX}/connectors/github/login/{flow_id}", headers=_headers(ACCOUNT_A))

        connected_a = _row(client, ACCOUNT_A)
        connected_b = _row(client, ACCOUNT_B)
        assert connected_a["connected"] is True
        assert connected_b["connected"] is False
        assert connected_b["account"] == ""

        # B 的卸载不能删掉 A 的连接（DELETE /github 是兼容别名）
        removed_b = client.delete(f"{PREFIX}/connectors/github", headers=_headers(ACCOUNT_B))
        assert removed_b.status_code == 200
        assert removed_b.json()["data"]["connected"] is False
        still_a = _row(client, ACCOUNT_A)
        assert still_a["connected"] is True
        assert still_a["account"] == "octocat"


def test_github_uninstall_via_generic_route_is_isolated(
    settings: Settings, db_schema: None
) -> None:
    with _client(settings, db_schema, _github_handler) as client:
        flow_id = client.post(
            f"{PREFIX}/connectors/github/login/start", headers=_headers(ACCOUNT_A)
        ).json()["data"]["flow_id"]
        client.get(f"{PREFIX}/connectors/github/login/{flow_id}", headers=_headers(ACCOUNT_A))

        removed = client.delete(f"{PREFIX}/connectors/github", headers=_headers(ACCOUNT_A))
        assert removed.status_code == 200
        assert removed.json()["data"]["connected"] is False
        assert _row(client, ACCOUNT_A)["connected"] is False
        # 卸载是幂等的
        again = client.delete(f"{PREFIX}/connectors/github", headers=_headers(ACCOUNT_A))
        assert again.status_code == 200
        assert again.json()["data"]["connected"] is False


def test_github_install_route_points_to_login(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema) as client:
        response = client.post(
            f"{PREFIX}/connectors/github/install",
            json={"auth_type": "oauth"},
            headers=_headers(ACCOUNT_A),
        )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "bad_request"
    assert body["data"]["login_path"].endswith("/connectors/github/login/start")


def test_github_does_not_accept_api_key_install(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema) as client:
        response = client.post(
            f"{PREFIX}/connectors/github/install",
            json={"auth_type": "api_key", "api_key": API_KEY},
            headers=_headers(ACCOUNT_A),
        )

    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"


# --- api_key 安装 / 卸载 ---


def test_api_key_install_and_uninstall(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema) as client:
        _register_fake_api_key_connector(client)

        installed = client.post(
            f"{PREFIX}/connectors/acme/install",
            json={"auth_type": "api_key", "api_key": API_KEY, "scopes": ["read", "write"]},
            headers=_headers(ACCOUNT_A),
        )
        assert installed.status_code == 200, installed.text
        state = installed.json()["data"]
        assert state["connected"] is True
        assert state["id"] == "acme"
        assert state["name"] == "Acme"
        assert state["scopes"] == ["read", "write"]
        assert API_KEY not in installed.text

        # 凭据确实以可解密形式落库，但接口不会返回
        account_a = client.get(f"{PREFIX}/connectors", headers=_headers(ACCOUNT_A)).json()["data"]
        acme_a = next(row for row in account_a if row["id"] == "acme")
        assert acme_a["connected"] is True
        assert acme_a["scopes"] == ["read", "write"]
        assert "credential" not in json.dumps(acme_a)

        # 重复安装给出明确冲突
        duplicate = client.post(
            f"{PREFIX}/connectors/acme/install",
            json={"auth_type": "api_key", "api_key": API_KEY},
            headers=_headers(ACCOUNT_A),
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "conflict"

        removed = client.delete(f"{PREFIX}/connectors/acme", headers=_headers(ACCOUNT_A))
        assert removed.status_code == 200
        assert removed.json()["data"]["connected"] is False
        assert removed.json()["data"]["scopes"] == []


def test_api_key_default_scopes_come_from_definition(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema) as client:
        _register_fake_api_key_connector(client)
        state = client.post(
            f"{PREFIX}/connectors/acme/install",
            json={"api_key": API_KEY},
            headers=_headers(ACCOUNT_A),
        ).json()["data"]

    assert state["connected"] is True
    assert state["scopes"] == ["read"]


def test_api_key_install_is_isolated_between_accounts(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema) as client:
        _register_fake_api_key_connector(client)
        client.post(
            f"{PREFIX}/connectors/acme/install",
            json={"api_key": API_KEY},
            headers=_headers(ACCOUNT_A),
        )

        rows_b = client.get(f"{PREFIX}/connectors", headers=_headers(ACCOUNT_B)).json()["data"]
        acme_b = next(row for row in rows_b if row["id"] == "acme")
        assert acme_b["connected"] is False
        assert acme_b["scopes"] == []

        # B 卸载不能影响 A
        removed_b = client.delete(f"{PREFIX}/connectors/acme", headers=_headers(ACCOUNT_B))
        assert removed_b.status_code == 200
        assert removed_b.json()["data"]["connected"] is False

        rows_a = client.get(f"{PREFIX}/connectors", headers=_headers(ACCOUNT_A)).json()["data"]
        acme_a = next(row for row in rows_a if row["id"] == "acme")
        assert acme_a["connected"] is True
        assert acme_a["scopes"] == ["read"]

        # 凭据读取同样按账号隔离
        framework = client.app.state.container.connector_framework
        assert asyncio.run(framework.credential_for(ACCOUNT_A, "acme")) == API_KEY
        assert asyncio.run(framework.credential_for(ACCOUNT_B, "acme")) is None

        client.delete(f"{PREFIX}/connectors/acme", headers=_headers(ACCOUNT_A))
        assert asyncio.run(framework.credential_for(ACCOUNT_A, "acme")) is None


def test_api_key_install_requires_key(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema) as client:
        _register_fake_api_key_connector(client)
        response = client.post(
            f"{PREFIX}/connectors/acme/install",
            json={"auth_type": "api_key"},
            headers=_headers(ACCOUNT_A),
        )

    assert response.status_code == 400
    assert "api_key" in response.json()["message"]


# --- 未知连接器与错误路径 ---


def test_unknown_connector_returns_404(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema) as client:
        install = client.post(
            f"{PREFIX}/connectors/not-a-connector/install",
            json={"auth_type": "api_key", "api_key": API_KEY},
            headers=_headers(ACCOUNT_A),
        )
        uninstall = client.delete(
            f"{PREFIX}/connectors/not-a-connector", headers=_headers(ACCOUNT_A)
        )
        catalog = client.get(f"{PREFIX}/connectors/catalog")

    assert install.status_code == 404
    assert uninstall.status_code == 404
    assert "not-a-connector" not in json.dumps(catalog.json()["data"])


def test_client_account_header_is_ignored_for_identity(settings: Settings, db_schema: None) -> None:
    """Phase 2：客户端自报的 X-Flux-Account 不再决定身份。

    非法值也不会 400；身份来自登录态（这里回退到主用户 1）。
    """
    with _client(settings, db_schema) as client:
        response = client.get(f"{PREFIX}/connectors", headers={"X-Flux-Account": "bad account!"})

    assert response.status_code == 200, response.text
    # 列表按主用户（而非请求头里的非法值）读取。
    assert all(isinstance(row, dict) for row in response.json()["data"])
