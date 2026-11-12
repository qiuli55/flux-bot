"""按 Bot 隔离的云桌面会话与票据。

覆盖：未配置时 503；配置后 profile 由服务端按 Bot UUID 派生；票据签发/验签/
过期/篡改；不同 Bot 的 profile 互相隔离；关闭长期密钥下发时响应不含 gateway_token。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from flux.config import Settings
from flux.core.desktop.tickets import (
    desktop_profile,
    issue_desktop_ticket,
    verify_desktop_ticket,
)
from flux.main import create_app

PREFIX = "/api/v1"
HOST = "https://desktop.example.test"
GATEWAY_TOKEN = "desktop-gateway-token-for-tests"
BRIDGE = "https://bridge.example.test"
BRIDGE_KEY = "bridge-key-for-tests"
TICKET_SECRET = "desktop-ticket-secret-for-tests"


def _client(settings: Settings, **updates: object) -> TestClient:
    return TestClient(create_app(settings.model_copy(update=updates)))


def _create_bot(client: TestClient, name: str = "桌面 Bot") -> str:
    response = client.post(f"{PREFIX}/bots", json={"name": name})
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["id"])


def _configured(settings: Settings, **updates: object) -> Settings:
    return settings.model_copy(
        update={
            "desktop_host": HOST,
            "desktop_gateway_token": GATEWAY_TOKEN,
            "desktop_bridge": BRIDGE,
            "desktop_bridge_key": BRIDGE_KEY,
            "desktop_ticket_secret": TICKET_SECRET,
            **updates,
        }
    )


def _expected_profile(bot_id: str) -> str:
    return f"bot{uuid.UUID(bot_id).hex[:8]}"


@pytest.mark.parametrize(
    "updates",
    [
        pytest.param({"desktop_host": None, "desktop_gateway_token": None}, id="both-missing"),
        pytest.param({"desktop_host": None, "desktop_gateway_token": GATEWAY_TOKEN}, id="no-host"),
        pytest.param({"desktop_host": HOST, "desktop_gateway_token": None}, id="no-gateway"),
    ],
)
def test_unconfigured_desktop_returns_503(
    settings: Settings, db_schema: None, updates: dict[str, object]
) -> None:
    configured = settings.model_copy(update=updates)
    with _client(configured) as client:
        bot_id = _create_bot(client)
        response = client.post(f"{PREFIX}/bots/{bot_id}/desktop/session")

    assert response.status_code == 503
    assert response.json()["code"] == "desktop_not_configured"


def test_configured_desktop_session_issues_bot_profile_and_ticket(
    settings: Settings, db_schema: None
) -> None:
    with _client(_configured(settings)) as client:
        bot_id = _create_bot(client)
        response = client.post(f"{PREFIX}/bots/{bot_id}/desktop/session")

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["profile"] == _expected_profile(bot_id)
        assert data["host"] == HOST
        assert data["bridge"] == BRIDGE
        assert data["gateway_token"] == GATEWAY_TOKEN
        assert data["bridge_key"] == BRIDGE_KEY
        assert data["ticket"]

        payload = verify_desktop_ticket(data["ticket"], TICKET_SECRET)
        assert payload is not None
        assert payload["bot_id"] == bot_id
        assert payload["profile"] == data["profile"]
        assert isinstance(payload["exp"], int)


def test_ticket_expiry_and_tampering_are_rejected(settings: Settings, db_schema: None) -> None:
    with _client(_configured(settings)) as client:
        bot_id = _create_bot(client)
        data = client.post(f"{PREFIX}/bots/{bot_id}/desktop/session").json()["data"]
        ticket = data["ticket"]

    assert verify_desktop_ticket(ticket, TICKET_SECRET) is not None
    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert verify_desktop_ticket(ticket, TICKET_SECRET, now=future) is None
    assert verify_desktop_ticket(ticket, "wrong-secret") is None

    replacement = "A" if ticket[-1] != "A" else "B"
    tampered = f"{ticket[:-1]}{replacement}"
    assert verify_desktop_ticket(tampered, TICKET_SECRET) is None


def test_two_bots_get_distinct_profiles(settings: Settings, db_schema: None) -> None:
    with _client(_configured(settings)) as client:
        first_id = _create_bot(client, "Bot A")
        second_id = _create_bot(client, "Bot B")
        first = client.post(f"{PREFIX}/bots/{first_id}/desktop/session").json()["data"]
        second = client.post(f"{PREFIX}/bots/{second_id}/desktop/session").json()["data"]

    assert first["profile"] == _expected_profile(first_id)
    assert second["profile"] == _expected_profile(second_id)
    assert first["profile"] != second["profile"]


def test_gateway_token_can_be_withheld(settings: Settings, db_schema: None) -> None:
    with _client(_configured(settings, desktop_send_gateway_token=False)) as client:
        bot_id = _create_bot(client)
        data = client.post(f"{PREFIX}/bots/{bot_id}/desktop/session").json()["data"]

    assert data["ticket"]
    assert "gateway_token" not in data
    assert "bridge_key" not in data


def test_ticket_must_match_requested_bot(settings: Settings) -> None:
    """票据只能用于签发它的那台 Bot：换 Bot / 换 profile 一律拒绝。"""
    bot_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    profile = desktop_profile(bot_id)
    ticket = issue_desktop_ticket(
        bot_id=bot_id,
        profile=profile,
        principal="device",
        secret=TICKET_SECRET,
        ttl_seconds=60,
    )
    assert ticket is not None

    assert (
        verify_desktop_ticket(
            ticket, TICKET_SECRET, expected_bot_id=bot_id, expected_profile=profile
        )
        is not None
    )
    assert verify_desktop_ticket(ticket, TICKET_SECRET, expected_bot_id=other_id) is None
    assert verify_desktop_ticket(ticket, TICKET_SECRET, expected_profile="botdeadbeef") is None


def test_ticket_helpers_are_self_contained(settings: Settings) -> None:
    bot_id = str(uuid.uuid4())
    profile = desktop_profile(bot_id)
    assert profile == _expected_profile(bot_id)

    assert (
        issue_desktop_ticket(
            bot_id=bot_id,
            profile=profile,
            principal="local",
            secret=None,
            ttl_seconds=60,
        )
        is None
    )

    ticket = issue_desktop_ticket(
        bot_id=bot_id,
        profile=profile,
        principal="device",
        secret=TICKET_SECRET,
        ttl_seconds=60,
        device_id="device-123",
    )
    payload = verify_desktop_ticket(ticket, TICKET_SECRET)
    assert payload is not None
    assert payload["principal"] == "device"
    assert payload["device_id"] == "device-123"
    assert payload["bot_id"] == bot_id
    assert payload["profile"] == profile
