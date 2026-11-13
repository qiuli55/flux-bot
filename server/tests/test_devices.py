"""设备配对与长期设备令牌。

覆盖：管理员签发配对码、公开兑换、令牌鉴权、一次性/过期/未知码统一错误、
按来源 IP 的失败限流、管理员撤销与设备自助登出、令牌过期与 last_seen 刷新。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from flux.bot_node.main import create_app
from flux.cli.main import main
from flux.config import Settings
from flux.core.devices.service import hash_device_token
from flux.db.session import create_engine, create_session_factory
from flux.models.device import Device, PairingCode

PREFIX = "/api/v1"
ADMIN_TOKEN = "test-admin-token"
#: 设备管理需要"这是谁"：测试默认把本机管理员当作主用户（Phase 1 起）。
OWNER_ID = "1"
#: 固定公网来源：限流按 client.host 分组，测试必须稳定命中同一个桶。
PUBLIC_CLIENT = {"client": ("198.51.100.7", 41000)}
ALPHABET = frozenset("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@contextmanager
def _client_for(settings: Settings, **updates: object) -> Iterator[TestClient]:
    # Phase 1 起签发配对码必须能解析"这是谁"：默认把管理员当作主用户，
    # 需要验证拒绝路径的用例可显式传 owner_cloud_user_id=None 覆盖。
    secured = settings.model_copy(
        update={"auth_token": ADMIN_TOKEN, "owner_cloud_user_id": OWNER_ID, **updates}
    )
    app = create_app(secured)
    with TestClient(app, **PUBLIC_CLIENT) as client:
        yield client


def _issue_code(client: TestClient) -> str:
    response = client.post(f"{PREFIX}/devices/pairing-codes", headers=_auth(ADMIN_TOKEN))
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["code"])


def _pair(client: TestClient, code: str, name: str = "phone") -> dict:
    response = client.post(
        f"{PREFIX}/devices/pair",
        json={"code": code, "device_name": name},
    )
    return response


def _past() -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=1)


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


def _expire_pairing_code(settings: Settings, code: str) -> None:
    _run_update(
        settings,
        update(PairingCode)
        .where(PairingCode.code_hash == hash_device_token(code))
        .values(expires_at=_past()),
    )


def _expire_device(settings: Settings, device_id: str) -> None:
    _run_update(
        settings,
        update(Device).where(Device.id == uuid.UUID(device_id)).values(expires_at=_past()),
    )


def test_pairing_code_requires_admin_not_device(settings: Settings, db_schema: None) -> None:
    with _client_for(settings) as client:
        public = client.post(f"{PREFIX}/devices/pairing-codes")
        assert public.status_code == 401
        assert public.json()["code"] == "unauthenticated"

        code = _issue_code(client)
        paired = _pair(client, code)
        assert paired.status_code == 201, paired.text
        token = paired.json()["data"]["token"]

        as_device = client.post(f"{PREFIX}/devices/pairing-codes", headers=_auth(token))
        assert as_device.status_code == 403
        assert as_device.json()["code"] == "permission_denied"

        admin = client.post(f"{PREFIX}/devices/pairing-codes", headers=_auth(ADMIN_TOKEN))
        assert admin.status_code == 200
        data = admin.json()["data"]
        assert len(data["code"]) == 8
        assert set(data["code"]) <= ALPHABET
        assert data["expires_at"] is not None


def test_pair_returns_token_once_and_token_authenticates(
    settings: Settings, db_schema: None
) -> None:
    with _client_for(settings) as client:
        code = _issue_code(client)
        response = _pair(client, code, name="Pixel")

        assert response.status_code == 201, response.text
        data = response.json()["data"]
        device = data["device"]
        token = data["token"]

        assert set({"id", "name", "token_prefix", "created_at", "expires_at"}) <= set(device)
        assert device["name"] == "Pixel"
        assert token.startswith("fbd_")
        assert device["token_prefix"] == token[:8]
        assert "token" not in device
        assert "token_hash" not in device
        uuid.UUID(device["id"])

        bots = client.get(f"{PREFIX}/bots", headers=_auth(token))
        assert bots.status_code == 200


def test_pairing_code_is_single_use(settings: Settings, db_schema: None) -> None:
    with _client_for(settings) as client:
        code = _issue_code(client)
        assert _pair(client, code).status_code == 201

        replay = _pair(client, code)
        assert replay.status_code == 400
        assert replay.json()["code"] == "invalid_pairing_code"


def test_expired_unknown_and_replayed_codes_share_error(
    settings: Settings, db_schema: None
) -> None:
    with _client_for(settings) as client:
        used_code = _issue_code(client)
        assert _pair(client, used_code).status_code == 201
        reused = _pair(client, used_code)

        expired_code = _issue_code(client)
        _expire_pairing_code(settings, expired_code)
        expired = _pair(client, expired_code)

        unknown = _pair(client, "ZZZZZZZZ")

        for response in (reused, expired, unknown):
            assert response.status_code == 400, response.text
            assert response.json()["code"] == "invalid_pairing_code"
        assert len({response.json()["message"] for response in (reused, expired, unknown)}) == 1


def test_failed_pair_attempts_are_rate_limited(settings: Settings, db_schema: None) -> None:
    with _client_for(settings) as client:
        for _ in range(10):
            response = _pair(client, "ZZZZZZZZ")
            assert response.status_code == 400, response.text

        limited = _pair(client, "ZZZZZZZZ")
        assert limited.status_code == 429
        assert limited.json()["code"] == "rate_limited"


def test_admin_revoke_invalidates_token_immediately(settings: Settings, db_schema: None) -> None:
    with _client_for(settings) as client:
        code = _issue_code(client)
        paired = _pair(client, code)
        device = paired.json()["data"]["device"]
        token = paired.json()["data"]["token"]

        # 先请求一次，把设备令牌放进进程内鉴权缓存。
        assert client.get(f"{PREFIX}/bots", headers=_auth(token)).status_code == 200

        revoked = client.delete(f"{PREFIX}/devices/{device['id']}", headers=_auth(ADMIN_TOKEN))
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["data"]["revoked"] is True

        assert client.get(f"{PREFIX}/bots", headers=_auth(token)).status_code == 401


def test_device_can_revoke_itself(settings: Settings, db_schema: None) -> None:
    with _client_for(settings) as client:
        code = _issue_code(client)
        paired = _pair(client, code)
        token = paired.json()["data"]["token"]

        revoked = client.post(f"{PREFIX}/devices/me/revoke", headers=_auth(token))
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["data"]["revoked"] is True

        assert client.get(f"{PREFIX}/bots", headers=_auth(token)).status_code == 401


def test_expired_device_token_is_rejected(settings: Settings, db_schema: None) -> None:
    with _client_for(settings) as client:
        code = _issue_code(client)
        paired = _pair(client, code)
        device = paired.json()["data"]["device"]
        token = paired.json()["data"]["token"]

        _expire_device(settings, device["id"])

        assert client.get(f"{PREFIX}/bots", headers=_auth(token)).status_code == 401


def test_last_seen_is_recorded_after_authenticated_request(
    settings: Settings, db_schema: None
) -> None:
    with _client_for(settings) as client:
        code = _issue_code(client)
        paired = _pair(client, code)
        token = paired.json()["data"]["token"]

        assert client.get(f"{PREFIX}/bots", headers=_auth(token)).status_code == 200

        listing = client.get(f"{PREFIX}/devices", headers=_auth(ADMIN_TOKEN))
        assert listing.status_code == 200, listing.text
        devices = listing.json()["data"]
        assert len(devices) == 1
        assert devices[0]["last_seen_at"] is not None


def test_cli_devices_pair_returns_code(
    settings: Settings, db_schema: None, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_settings = settings.model_copy(update={"owner_cloud_user_id": OWNER_ID})
    code = main(["devices", "pair"], settings=cli_settings)
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert len(payload["data"]["code"]) == 8
    assert set(payload["data"]["code"]) <= ALPHABET
    assert payload["data"]["cloud_user_id"] == OWNER_ID
