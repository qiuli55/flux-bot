"""用户保险库扩展测试。

覆盖三件事：

1. 登录 / 支付卡 / 联系人的 CRUD 与掩码响应（密码、PAN、CVV 永不回传）；
2. 密文落库确实不是明文，且复用密钥库同一把主密钥（换 Container 仍可解密，
   换错 / 丢失主密钥则 fail-closed）；
3. 日志里不出现密码、PAN、CVV 或联系人 PII。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from flux.config import Settings
from flux.container import Container
from flux.core.key_vault.service import KeyVaultService
from flux.errors import ConfigurationError

PREFIX = "/api/v1/vault"
LOGIN_PASSWORD = "s3cret-password-登录"
NEW_LOGIN_PASSWORD = "rotated-password-456"
CARD_PAN = "4242424242424242"
CARD_CVV = "987"
CONTACT_PHONE = "+8613800138000"
CONTACT_EMAIL = "alice@example.com"


def _db_path(settings: Settings) -> str:
    return settings.database_url.split("///", 1)[-1]


def _create_login(client: TestClient, **overrides: object) -> dict:
    body = {
        "site": "https://example.com/login",
        "username": "alice",
        "password": LOGIN_PASSWORD,
        "notes": "personal",
    }
    body.update(overrides)
    response = client.post(f"{PREFIX}/logins", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _create_card(client: TestClient, **overrides: object) -> dict:
    body = {
        "label": "日常卡",
        "brand": "",
        "pan": CARD_PAN,
        "cvv": CARD_CVV,
        "exp_month": 12,
        "exp_year": 2030,
        "notes": "",
    }
    body.update(overrides)
    response = client.post(f"{PREFIX}/cards", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _create_contact(client: TestClient, **overrides: object) -> dict:
    body = {
        "name": "Alice",
        "phones": [CONTACT_PHONE],
        "emails": [CONTACT_EMAIL],
        "notes": "家庭",
    }
    body.update(overrides)
    response = client.post(f"{PREFIX}/contacts", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


# --- 站点登录 ---


def test_login_create_list_get_mask_password(client: TestClient, settings: Settings) -> None:
    created = _create_login(client)

    assert created["site"] == "https://example.com/login"
    assert created["username"] == "alice"
    assert created["has_password"] is True
    assert "password" not in created
    assert "password_ciphertext" not in created

    listed = client.get(f"{PREFIX}/logins").json()["data"]
    assert len(listed) == 1
    assert listed[0]["username"] == "alice"
    assert "password" not in listed[0]

    got = client.get(f"{PREFIX}/logins/{created['id']}").json()["data"]
    assert "password" not in got
    assert "password_ciphertext" not in got

    # 直接查库：密文里不能出现密码明文
    with sqlite3.connect(_db_path(settings)) as conn:
        row = conn.execute("SELECT password_ciphertext FROM vault_logins").fetchone()
    assert row is not None
    assert LOGIN_PASSWORD.encode() not in bytes(row[0])


def test_login_update_delete_and_blank_password_keeps_secret(client: TestClient) -> None:
    created = _create_login(client)

    partial = client.patch(
        f"{PREFIX}/logins/{created['id']}",
        json={"username": "bob", "notes": "", "password": ""},
    )
    assert partial.status_code == 200, partial.text
    updated = partial.json()["data"]
    assert updated["username"] == "bob"
    assert updated["notes"] == ""
    revealed = client.post(f"{PREFIX}/logins/{created['id']}/reveal").json()["data"]
    assert revealed["password"] == LOGIN_PASSWORD

    rotated = client.patch(
        f"{PREFIX}/logins/{created['id']}", json={"password": NEW_LOGIN_PASSWORD}
    )
    assert rotated.status_code == 200, rotated.text
    revealed = client.post(f"{PREFIX}/logins/{created['id']}/reveal").json()["data"]
    assert revealed["password"] == NEW_LOGIN_PASSWORD

    deleted = client.delete(f"{PREFIX}/logins/{created['id']}")
    assert deleted.status_code == 200, deleted.text
    assert client.get(f"{PREFIX}/logins/{created['id']}").status_code == 404


def test_login_reveal_is_audited_without_the_secret(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    created = _create_login(client)

    with caplog.at_level(logging.INFO):
        response = client.post(f"{PREFIX}/logins/{created['id']}/reveal")

    assert response.status_code == 200, response.text
    assert response.json()["data"]["password"] == LOGIN_PASSWORD
    # 只看应用自身 logger：aiosqlite / SQLAlchemy 在 DEBUG 下会打印 SQL 参数，
    # 属框架行为（生产默认 INFO 不输出），不应混进"本服务有没有泄漏"的断言。
    messages = "\n".join(
        record.getMessage() for record in caplog.records if record.name.startswith("flux.")
    )
    assert created["id"] in messages
    assert LOGIN_PASSWORD not in messages


def test_login_unknown_and_invalid_ids_are_4xx(client: TestClient) -> None:
    assert client.get(f"{PREFIX}/logins/{uuid.uuid4()}").status_code == 404
    assert client.get(f"{PREFIX}/logins/not-a-uuid").status_code == 400
    assert client.delete(f"{PREFIX}/logins/not-a-uuid").status_code == 400


# --- 支付卡 ---


def test_card_create_list_get_never_expose_pan_or_cvv(
    client: TestClient, settings: Settings
) -> None:
    created = _create_card(client)

    for forbidden in ("pan", "cvv", "secret_ciphertext"):
        assert forbidden not in created
    assert created["last4"] == "4242"
    assert created["brand"] == "visa"
    assert created["exp_month"] == 12
    assert created["exp_year"] == 2030
    assert created["has_pan"] is True
    assert created["has_cvv"] is True

    listed = client.get(f"{PREFIX}/cards").json()["data"]
    assert len(listed) == 1
    for forbidden in ("pan", "cvv", "secret_ciphertext"):
        assert forbidden not in listed[0]
    assert listed[0]["last4"] == "4242"

    got = client.get(f"{PREFIX}/cards/{created['id']}").json()["data"]
    for forbidden in ("pan", "cvv", "secret_ciphertext"):
        assert forbidden not in got

    # 密文里不能出现完整卡号或 CVV 的 JSON 片段
    with sqlite3.connect(_db_path(settings)) as conn:
        row = conn.execute("SELECT secret_ciphertext FROM vault_cards").fetchone()
    assert row is not None
    blob = bytes(row[0])
    assert CARD_PAN.encode() not in blob
    assert b'"cvv":"987"' not in blob


def test_card_has_no_reveal_endpoint(client: TestClient) -> None:
    card = _create_card(client)
    assert client.post(f"{PREFIX}/cards/{card['id']}/reveal").status_code == 404
    assert client.get(f"{PREFIX}/cards/{card['id']}/reveal").status_code == 404


def test_card_last4_only_is_supported(client: TestClient) -> None:
    created = _create_card(
        client,
        pan=None,
        cvv=None,
        last4="9876",
        brand="mastercard",
        exp_month=1,
        exp_year=2031,
    )
    assert created["last4"] == "9876"
    assert created["brand"] == "mastercard"
    assert created["has_pan"] is False
    assert created["has_cvv"] is False


def test_card_update_reencrypts_with_same_master_key(
    client: TestClient, settings: Settings
) -> None:
    created = _create_card(client)

    response = client.patch(
        f"{PREFIX}/cards/{created['id']}",
        json={
            "label": "旅行卡",
            "pan": "5555555555554444",
            "cvv": "456",
            "exp_month": 1,
            "exp_year": 2031,
        },
    )
    assert response.status_code == 200, response.text
    updated = response.json()["data"]
    assert updated["label"] == "旅行卡"
    assert updated["last4"] == "4444"
    assert updated["brand"] == "mastercard"
    for forbidden in ("pan", "cvv", "secret_ciphertext"):
        assert forbidden not in updated

    with sqlite3.connect(_db_path(settings)) as conn:
        blob = conn.execute("SELECT secret_ciphertext FROM vault_cards").fetchone()[0]
    # 用同一把主密钥可以解开更新后的密文（测试侧直接验证信封）
    vault = KeyVaultService(None, settings)  # type: ignore[arg-type]
    payload = json.loads(vault.open_text(bytes(blob)))
    assert payload == {"pan": "5555555555554444", "cvv": "456"}

    # 已存完整卡号的卡片不能单独改 last4（否则展示值与密文脱节）
    blocked = client.patch(f"{PREFIX}/cards/{created['id']}", json={"last4": "1111"})
    assert blocked.status_code == 400, blocked.text


def test_card_validation_rejects_bad_input(client: TestClient) -> None:
    missing_identity = client.post(f"{PREFIX}/cards", json={"label": "x"})
    assert missing_identity.status_code == 400, missing_identity.text

    short_pan = client.post(f"{PREFIX}/cards", json={"pan": "1234"})
    assert short_pan.status_code == 400, short_pan.text

    non_digit_pan = client.post(f"{PREFIX}/cards", json={"pan": "4242abcd42424242"})
    assert non_digit_pan.status_code == 400, non_digit_pan.text

    bad_cvv = client.post(f"{PREFIX}/cards", json={"last4": "4242", "cvv": "12"})
    assert bad_cvv.status_code == 400, bad_cvv.text

    mismatch = client.post(f"{PREFIX}/cards", json={"pan": CARD_PAN, "last4": "0000"})
    assert mismatch.status_code == 400, mismatch.text

    bad_exp = client.post(
        f"{PREFIX}/cards", json={"last4": "4242", "exp_month": 13, "exp_year": 2030}
    )
    assert bad_exp.status_code == 422, bad_exp.text

    half_exp = client.post(f"{PREFIX}/cards", json={"last4": "4242", "exp_month": 12})
    assert half_exp.status_code == 400, half_exp.text


# --- 联系人 ---


def test_contact_crud_and_field_caps(client: TestClient) -> None:
    created = _create_contact(client)
    assert created["name"] == "Alice"
    assert created["phones"] == [CONTACT_PHONE]
    assert created["emails"] == [CONTACT_EMAIL]

    listed = client.get(f"{PREFIX}/contacts").json()["data"]
    assert len(listed) == 1

    updated = client.patch(
        f"{PREFIX}/contacts/{created['id']}",
        json={"name": "Alice Chen", "phones": ["+8613900139000"], "emails": []},
    )
    assert updated.status_code == 200, updated.text
    data = updated.json()["data"]
    assert data["name"] == "Alice Chen"
    assert data["phones"] == ["+8613900139000"]
    assert data["emails"] == []

    assert client.delete(f"{PREFIX}/contacts/{created['id']}").status_code == 200
    assert client.get(f"{PREFIX}/contacts/{created['id']}").status_code == 404

    too_long_name = client.post(f"{PREFIX}/contacts", json={"name": "x" * 129})
    assert too_long_name.status_code == 422, too_long_name.text

    too_many = client.post(f"{PREFIX}/contacts", json={"name": "x", "phones": ["1"] * 21})
    assert too_many.status_code == 422, too_many.text

    long_item = client.post(f"{PREFIX}/contacts", json={"name": "x", "phones": ["1" * 41]})
    assert long_item.status_code == 400, long_item.text


# --- 主密钥复用 / fail-closed ---


def test_same_master_key_can_decrypt_after_new_container(
    client: TestClient, settings: Settings
) -> None:
    created = _create_login(client)

    restarted = Container(settings)
    try:
        password = asyncio.run(restarted.vault.reveal_login(created["id"]))
    finally:
        asyncio.run(restarted.dispose())
    assert password == LOGIN_PASSWORD


def test_wrong_master_key_fails_closed(client: TestClient, settings: Settings) -> None:
    created = _create_login(client)

    wrong = Container(
        settings.model_copy(update={"key_vault_secret": "definitely-wrong-master-key"})
    )
    try:
        with pytest.raises(ConfigurationError):
            asyncio.run(wrong.vault.reveal_login(created["id"]))
    finally:
        asyncio.run(wrong.dispose())


def test_missing_master_key_file_fails_closed(
    client: TestClient, settings: Settings, tmp_path
) -> None:
    """没有显式主密钥、密钥文件也不存在时：自动生成的新密钥解不开旧密文，必须报错。"""
    created = _create_login(client)

    missing = Container(
        settings.model_copy(
            update={
                "key_vault_secret": None,
                "key_vault_key_file": str(tmp_path / "missing" / "key-vault.key"),
            }
        )
    )
    try:
        with pytest.raises(ConfigurationError):
            asyncio.run(missing.vault.reveal_login(created["id"]))
    finally:
        asyncio.run(missing.dispose())


# --- 日志 ---


def test_logs_never_contain_secrets_or_contact_pii(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        login = _create_login(client)
        client.patch(f"{PREFIX}/logins/{login['id']}", json={"password": NEW_LOGIN_PASSWORD})
        client.post(f"{PREFIX}/logins/{login['id']}/reveal")
        card = _create_card(client)
        client.patch(f"{PREFIX}/cards/{card['id']}", json={"cvv": "654"})
        _create_contact(client)

    # 只看应用自身 logger：aiosqlite / SQLAlchemy 在 DEBUG 下会打印 SQL 参数，
    # 属框架行为（生产默认 INFO 不输出），不应混进"本服务有没有泄漏"的断言。
    messages = "\n".join(
        record.getMessage() for record in caplog.records if record.name.startswith("flux.")
    )
    assert LOGIN_PASSWORD not in messages
    assert NEW_LOGIN_PASSWORD not in messages
    assert CARD_PAN not in messages
    assert '"cvv"' not in messages
    assert "cvv=" not in messages
    assert CONTACT_PHONE not in messages
    assert CONTACT_EMAIL not in messages
