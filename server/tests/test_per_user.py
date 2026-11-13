"""Phase 1 按用户隔离：账号云身份 → principal → 设备归属。

覆盖：
- 云 access token 的解析（typ / sub / 签名 / 过期）与 principal 盖章；
- `current_cloud_user_id` 的三级回退（principal → FLUX_OWNER_CLOUD_USER_ID → None）；
- 配对码 / 设备按人盖章，跨用户列表与撤销互相不可见；
- 历史 NULL 归属设备只对配置的主用户可见，设备令牌鉴权回退到主用户；
- 没有云登录态也没有主用户时，签发配对码 / 列表 / 撤销明确拒绝。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi import Depends, Request
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from flux.api.auth import (
    current_cloud_user_id,
    is_cloud_access_token,
    parse_cloud_access_token,
    require_rest_auth,
)
from flux.bot_node.main import create_app
from flux.cli.main import main
from flux.config import Settings
from flux.core.devices.service import hash_device_token
from flux.db.session import create_engine, create_session_factory
from flux.models.device import Device, PairingCode
from tests.cloud_jwt import CLOUD_TEST_SECRET, mint_cloud_token

PREFIX = "/api/v1"
ADMIN_TOKEN = "test-admin-token"
OWNER_ID = "1"
#: 固定公网来源：限流按 client.host 分组，测试必须稳定命中同一个桶。
PUBLIC_CLIENT = {"client": ("198.51.100.7", 41000)}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@contextmanager
def _client_for(settings: Settings, **updates: object) -> Iterator[TestClient]:
    secured = settings.model_copy(update={"auth_token": ADMIN_TOKEN, **updates})
    app = create_app(secured)
    with TestClient(app, **PUBLIC_CLIENT) as client:
        yield client


@contextmanager
def _whoami_client(settings: Settings) -> Iterator[TestClient]:
    """挂一条只读探针，把 `require_rest_auth` 盖出来的 principal 原样返回。

    真实业务接口不暴露 principal，但 Phase 1 的验收点之一是"设备令牌鉴权时
    cloud_user_id 是否回退到主用户"，所以单独接一条测试路由来观察。
    """
    app = create_app(settings)

    @app.get(f"{PREFIX}/_test/principal")
    async def whoami(
        request: Request,
        _: None = Depends(require_rest_auth),
    ) -> dict[str, object]:
        return {"principal": getattr(request.state, "principal", None)}

    with TestClient(app, **PUBLIC_CLIENT) as client:
        yield client


def _issue_code(client: TestClient, token: str) -> str:
    response = client.post(f"{PREFIX}/devices/pairing-codes", headers=_auth(token))
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["code"])


def _pair(client: TestClient, code: str, name: str = "phone") -> dict:
    response = client.post(
        f"{PREFIX}/devices/pair",
        json={"code": code, "device_name": name},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _list_ids(client: TestClient, token: str) -> set[str]:
    response = client.get(f"{PREFIX}/devices", headers=_auth(token))
    assert response.status_code == 200, response.text
    return {str(device["id"]) for device in response.json()["data"]}


def _device_ids(client: TestClient, token: str) -> list[str]:
    response = client.get(f"{PREFIX}/devices", headers=_auth(token))
    assert response.status_code == 200, response.text
    return [str(device["id"]) for device in response.json()["data"]]


def _run_update(settings: Settings, statement: object) -> None:
    async def _update() -> None:
        engine = create_engine(settings.database_url)
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                await session.execute(statement)
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_update())


def _pairing_code_owner(settings: Settings, code: str) -> str | None:
    async def _read() -> str | None:
        engine = create_engine(settings.database_url)
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                row = await session.scalar(
                    select(PairingCode).where(PairingCode.code_hash == hash_device_token(code))
                )
                return row.cloud_user_id if row is not None else None
        finally:
            await engine.dispose()

    return asyncio.run(_read())


def _set_device_owner(settings: Settings, device_id: str, owner: str | None) -> None:
    _run_update(
        settings,
        update(Device).where(Device.id == uuid.UUID(device_id)).values(cloud_user_id=owner),
    )


# --- A. 解析 helper / principal ---


def test_parse_cloud_access_token_extracts_string_sub(settings: Settings) -> None:
    cfg = settings.model_copy(update={"cloud_secret_key": CLOUD_TEST_SECRET})

    payload = parse_cloud_access_token(cfg, mint_cloud_token("2"))
    assert payload is not None
    assert payload["sub"] == "2"

    # 云端的 sub 是数字：本仓库内部一律转成字符串。
    numeric = parse_cloud_access_token(cfg, mint_cloud_token(2))
    assert numeric is not None
    assert numeric["sub"] == "2"
    assert is_cloud_access_token(cfg, mint_cloud_token("2")) is True


def test_cloud_jwt_sets_principal_cloud_user_id(settings: Settings, db_schema: None) -> None:
    cfg = settings.model_copy(
        update={"cloud_secret_key": CLOUD_TEST_SECRET, "owner_cloud_user_id": "1"}
    )
    with _whoami_client(cfg) as client:
        response = client.get(f"{PREFIX}/_test/principal", headers=_auth(mint_cloud_token("2")))
        assert response.status_code == 200, response.text
        # JWT 的 sub 优先于 FLUX_OWNER_CLOUD_USER_ID。
        assert response.json()["principal"] == {"kind": "cloud", "cloud_user_id": "2"}


def test_admin_principal_does_not_fake_cloud_user_id(settings: Settings, db_schema: None) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": "1", "auth_token": ADMIN_TOKEN})
    with _whoami_client(cfg) as client:
        response = client.get(f"{PREFIX}/_test/principal", headers=_auth(ADMIN_TOKEN))
        assert response.status_code == 200, response.text
        assert response.json()["principal"] == {"kind": "admin"}


# --- B. 无效云令牌 ---


@pytest.mark.parametrize(
    "token_factory",
    [
        pytest.param(lambda: mint_cloud_token("1", typ="refresh"), id="wrong-typ"),
        pytest.param(lambda: mint_cloud_token("1", include_typ=False), id="missing-typ"),
        pytest.param(lambda: mint_cloud_token("1", include_sub=False), id="missing-sub"),
        pytest.param(lambda: mint_cloud_token(sub="   "), id="blank-sub"),
        pytest.param(
            lambda: mint_cloud_token("1", secret="not-the-shared-secret"), id="wrong-secret"
        ),
        pytest.param(lambda: mint_cloud_token("1", expires_in=-10), id="expired"),
        pytest.param(lambda: "not-a-jwt", id="malformed"),
    ],
)
def test_invalid_cloud_tokens_are_not_accepted(
    settings: Settings, db_schema: None, token_factory: object
) -> None:
    cfg = settings.model_copy(update={"cloud_secret_key": CLOUD_TEST_SECRET})
    token = token_factory()  # type: ignore[operator]
    assert parse_cloud_access_token(cfg, token) is None
    assert is_cloud_access_token(cfg, token) is False

    with _whoami_client(cfg) as client:
        # 没有别的方式认证 → 云令牌不成立就是 401。
        response = client.get(f"{PREFIX}/_test/principal", headers=_auth(token))
        assert response.status_code == 401
        assert response.json()["code"] == "unauthenticated"


def test_cloud_token_needs_configured_secret(settings: Settings) -> None:
    """未配置共享密钥时，即使令牌格式正确也不认（登录闭环整体关闭）。"""
    no_secret = settings.model_copy(update={"cloud_secret_key": None})
    assert no_secret.cloud_secret_key is None
    assert parse_cloud_access_token(no_secret, mint_cloud_token("1")) is None


# --- I. current_cloud_user_id 的三级回退 ---


def test_current_cloud_user_id_resolution_order(settings: Settings) -> None:
    with_owner = settings.model_copy(update={"owner_cloud_user_id": "1"})
    no_owner = settings.model_copy(update={"owner_cloud_user_id": None})

    # 1. principal 自带优先
    assert current_cloud_user_id({"cloud_user_id": "2"}, with_owner) == "2"
    # 2. 回退到配置的主用户
    assert current_cloud_user_id({"kind": "admin"}, with_owner) == "1"
    # 3. 两边都没有 → None
    assert current_cloud_user_id({}, no_owner) is None
    # 空白字符串视为未提供
    assert current_cloud_user_id({"cloud_user_id": "  "}, no_owner) is None
    assert (
        current_cloud_user_id(
            {"kind": "device"}, settings.model_copy(update={"owner_cloud_user_id": "  "})
        )
        is None
    )


# --- C. 没有云登录态 / 主用户时拒绝 ---


def test_pairing_without_cloud_login_is_rejected(settings: Settings, db_schema: None) -> None:
    cfg = settings.model_copy(
        update={"owner_cloud_user_id": None, "cloud_secret_key": CLOUD_TEST_SECRET}
    )
    with _client_for(cfg) as client:
        issue = client.post(f"{PREFIX}/devices/pairing-codes", headers=_auth(ADMIN_TOKEN))
        assert issue.status_code == 403
        assert issue.json()["code"] == "cloud_login_required"

        listing = client.get(f"{PREFIX}/devices", headers=_auth(ADMIN_TOKEN))
        assert listing.status_code == 403
        assert listing.json()["code"] == "cloud_login_required"

        revoke = client.delete(f"{PREFIX}/devices/{uuid.uuid4()}", headers=_auth(ADMIN_TOKEN))
        assert revoke.status_code == 403
        assert revoke.json()["code"] == "cloud_login_required"


def test_cli_device_commands_require_owner_setting(
    settings: Settings, db_schema: None, capsys: pytest.CaptureFixture[str]
) -> None:
    for argv in (["devices", "pair"], ["devices", "list"]):
        code = main(argv, settings=settings.model_copy(update={"owner_cloud_user_id": None}))
        payload = json.loads(capsys.readouterr().out)
        assert code == 1, argv
        assert payload["error"]["code"] == "configuration_error", argv


# --- D. 主用户兜底签发并盖章 ---


def test_owner_setting_stamps_code_and_device(settings: Settings, db_schema: None) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": OWNER_ID})
    with _client_for(cfg) as client:
        code = _issue_code(client, ADMIN_TOKEN)
        assert _pairing_code_owner(cfg, code) == OWNER_ID

        data = _pair(client, code, name="owner-phone")
        assert data["device"]["cloud_user_id"] == OWNER_ID
        # 主用户（这里用管理员令牌代表）能看到自己的设备。
        assert _list_ids(client, ADMIN_TOKEN) == {data["device"]["id"]}


# --- E. 云 JWT 的 sub 优先于主用户配置 ---


def test_cloud_jwt_sub_overrides_owner_setting(settings: Settings, db_schema: None) -> None:
    cfg = settings.model_copy(
        update={"owner_cloud_user_id": OWNER_ID, "cloud_secret_key": CLOUD_TEST_SECRET}
    )
    user_two = mint_cloud_token("2")
    with _client_for(cfg) as client:
        code = _issue_code(client, user_two)
        assert _pairing_code_owner(cfg, code) == "2"

        data = _pair(client, code, name="user-two-phone")
        assert data["device"]["cloud_user_id"] == "2"

        # 不会被悄悄记到主用户 1 名下。
        assert _list_ids(client, ADMIN_TOKEN) == set()
        assert _list_ids(client, user_two) == {data["device"]["id"]}


# --- F. 跨用户隔离 ---


def test_cross_user_device_isolation(settings: Settings, db_schema: None) -> None:
    cfg = settings.model_copy(
        update={"owner_cloud_user_id": None, "cloud_secret_key": CLOUD_TEST_SECRET}
    )
    user_a = mint_cloud_token("1")
    user_b = mint_cloud_token("2")
    with _client_for(cfg) as client:
        device_a = _pair(client, _issue_code(client, user_a), name="a-phone")["device"]
        device_b = _pair(client, _issue_code(client, user_b), name="b-phone")["device"]

        assert _list_ids(client, user_a) == {device_a["id"]}
        assert _list_ids(client, user_b) == {device_b["id"]}

        # 撤销对方的设备：按不存在处理（404），不泄露 id 是否存在。
        cross_a = client.delete(f"{PREFIX}/devices/{device_b['id']}", headers=_auth(user_a))
        assert cross_a.status_code == 404
        assert cross_a.json()["code"] == "not_found"

        cross_b = client.delete(f"{PREFIX}/devices/{device_a['id']}", headers=_auth(user_b))
        assert cross_b.status_code == 404
        assert cross_b.json()["code"] == "not_found"

        # 各自的设备仍然有效、仍可见。
        assert client.get(f"{PREFIX}/bots", headers=_auth(user_a)).status_code == 200
        assert _list_ids(client, user_a) == {device_a["id"]}


# --- G. 历史 NULL 归属 ---


def test_legacy_null_owner_falls_back_to_configured_owner(
    settings: Settings, db_schema: None
) -> None:
    cfg = settings.model_copy(
        update={"owner_cloud_user_id": OWNER_ID, "cloud_secret_key": CLOUD_TEST_SECRET}
    )
    user_two = mint_cloud_token("2")
    with _client_for(cfg) as client:
        paired = _pair(client, _issue_code(client, user_two), name="legacy-phone")
        device = paired["device"]
        token = paired["token"]

        # 模拟迁移前的老数据：设备归属为 NULL。
        _set_device_owner(cfg, device["id"], None)

        # 设备令牌鉴权：principal 的 cloud_user_id 回退到主用户。
        with _whoami_client(cfg) as whoami:
            response = whoami.get(f"{PREFIX}/_test/principal", headers=_auth(token))
            assert response.status_code == 200, response.text
            principal = response.json()["principal"]
            assert principal["kind"] == "device"
            assert principal["device_id"] == device["id"]
            assert principal["cloud_user_id"] == OWNER_ID

        # 主用户（JWT sub=1）能看到并撤销这条历史设备。
        assert _list_ids(client, mint_cloud_token("1")) == {device["id"]}
        # 另一个云用户看不到，也撤销不了。
        assert _list_ids(client, user_two) == set()
        denied = client.delete(f"{PREFIX}/devices/{device['id']}", headers=_auth(user_two))
        assert denied.status_code == 404

        revoked = client.delete(
            f"{PREFIX}/devices/{device['id']}", headers=_auth(mint_cloud_token("1"))
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["data"]["revoked"] is True


def test_null_owner_without_owner_setting_stays_invisible(
    settings: Settings, db_schema: None
) -> None:
    """没配主用户时，NULL 归属设备不属于任何人（但配对本身不失败）。"""
    cfg = settings.model_copy(
        update={"owner_cloud_user_id": None, "cloud_secret_key": CLOUD_TEST_SECRET}
    )
    user_two = mint_cloud_token("2")
    with _client_for(cfg) as client:
        paired = _pair(client, _issue_code(client, user_two), name="legacy-phone")
        device = paired["device"]
        _set_device_owner(cfg, device["id"], None)

        assert _list_ids(client, user_two) == set()
        denied = client.delete(f"{PREFIX}/devices/{device['id']}", headers=_auth(user_two))
        assert denied.status_code == 404
