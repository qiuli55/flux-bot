"""Email 连接器测试：安装 / 账号隔离 / IMAP 读信 / SMTP 发信 / 错误路径。

所有 IMAP / SMTP 交互都用测试内的假连接对象顶替，绝不访问真实网络。
凭据以加密 JSON 落库，测试同时验证密码不会出现在响应、日志与数据库明文列里。
"""

from __future__ import annotations

import asyncio
import imaplib
import logging
import smtplib
from email.message import EmailMessage
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from flux.config import Settings
from flux.container import Container
from flux.core.github.service import ACCOUNT_HEADER
from flux.main import create_app
from flux.models.connector_installation import ConnectorInstallation

PREFIX = "/api/v1"
ACCOUNT_A = "account-a"
ACCOUNT_B = "account-b"
USERNAME = "alice@example.test"
PASSWORD = "mail-app-password-super-secret"
BODY_MARKER = "unique-body-marker-42"

INSTALL_SETTINGS: dict[str, Any] = {
    "imap_host": "imap.example.test",
    "imap_port": 993,
    "imap_ssl": True,
    "smtp_host": "smtp.example.test",
    "smtp_port": 587,
    "smtp_starttls": True,
    "username": USERNAME,
}


class _CaptureHandler(logging.Handler):
    """最小日志捕获器：create_app 会重置根 logger，pytest 的 caplog 会失效。"""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _headers(account: str = ACCOUNT_A) -> dict[str, str]:
    return {ACCOUNT_HEADER: account}


def _client(settings: Settings, db_schema: None) -> TestClient:
    return TestClient(create_app(settings))


def _install(
    client: TestClient,
    account: str = ACCOUNT_A,
    *,
    api_key: str | None = PASSWORD,
    settings_overrides: dict[str, Any] | None = None,
    remove: tuple[str, ...] = (),
) -> Any:
    payload_settings = dict(INSTALL_SETTINGS)
    payload_settings.update(settings_overrides or {})
    for key in remove:
        payload_settings.pop(key, None)
    body: dict[str, Any] = {"auth_type": "api_key", "settings": payload_settings}
    if api_key is not None:
        body["api_key"] = api_key
    return client.post(
        f"{PREFIX}/connectors/email/install",
        json=body,
        headers=_headers(account),
    )


# --- 假 IMAP / SMTP ---------------------------------------------------------


def _message_bytes(
    *,
    subject: str = "Hello",
    sender: str = "bob@example.test",
    body: str = "plain body",
    date: str = "Mon, 10 Nov 2025 09:00:00 +0000",
    html_only: bool = False,
    html: str | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = USERNAME
    message["Subject"] = subject
    message["Date"] = date
    if html_only:
        message.set_content(body, subtype="html")
    else:
        message.set_content(body)
        if html is not None:
            message.add_alternative(html, subtype="html")
    return message.as_bytes()


def _header_part(raw: bytes) -> bytes:
    for separator in (b"\r\n\r\n", b"\n\n"):
        index = raw.find(separator)
        if index >= 0:
            return raw[: index + len(separator)]
    return raw


class FakeIMAP:
    def __init__(
        self,
        messages: dict[bytes, tuple[bytes, bool]] | None = None,
        *,
        login_error: Exception | None = None,
        fetch_error: Exception | None = None,
        noop_error: Exception | None = None,
        select_status: str = "OK",
    ) -> None:
        self.messages = messages or {}
        self.login_error = login_error
        self.fetch_error = fetch_error
        self.noop_error = noop_error
        self.select_status = select_status
        self.credentials: tuple[str, str] | None = None
        self.mailbox = ""
        self.readonly: bool | None = None
        self.logged_out = False

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        self.credentials = (username, password)
        if self.login_error is not None:
            raise self.login_error
        return "OK", [b"LOGIN completed"]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.mailbox = mailbox
        self.readonly = readonly
        return self.select_status, [b"1"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        if command == "search":
            return "OK", [b" ".join(self.messages)]
        if command == "fetch":
            uid, query = args
            if self.fetch_error is not None:
                raise self.fetch_error
            raw, seen = self.messages[uid]
            payload = _header_part(raw) if b"HEADER" in query else raw
            flags = b"\\Seen" if seen else b""
            meta = (
                b"1 (UID "
                + uid
                + b" FLAGS ("
                + flags
                + b") BODY[] {"
                + str(len(payload)).encode()
                + b"}"
            )
            return "OK", [(meta, payload), b")"]
        return "BAD", [b"unsupported"]

    def noop(self) -> tuple[str, list[bytes]]:
        if self.noop_error is not None:
            raise self.noop_error
        return "OK", [b"NOOP"]

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "BYE", [b"logout"]

    def close(self) -> tuple[str, list[bytes]]:
        return "OK", [b"close"]


class FakeSMTP:
    def __init__(
        self,
        *,
        send_error: Exception | None = None,
        login_error: Exception | None = None,
        noop_error: Exception | None = None,
    ) -> None:
        self.send_error = send_error
        self.login_error = login_error
        self.noop_error = noop_error
        self.sent: list[EmailMessage] = []
        self.credentials: tuple[str, str] | None = None
        self.starttls_used = False
        self.noop_called = False
        self.quit_called = False

    def ehlo(self) -> tuple[int, bytes]:
        return 250, b"ok"

    def starttls(self, context: Any = None) -> tuple[int, bytes]:
        self.starttls_used = True
        return 220, b"ready"

    def login(self, username: str, password: str) -> tuple[int, bytes]:
        self.credentials = (username, password)
        if self.login_error is not None:
            raise self.login_error
        return 235, b"ok"

    def send_message(self, message: EmailMessage) -> dict[str, Any]:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(message)
        return {}

    def noop(self) -> tuple[int, bytes]:
        if self.noop_error is not None:
            raise self.noop_error
        self.noop_called = True
        return 250, b"ok"

    def quit(self) -> tuple[int, bytes]:
        self.quit_called = True
        return 221, b"bye"

    def close(self) -> None:
        return None


def _patch_imap(monkeypatch: pytest.MonkeyPatch, fake: FakeIMAP) -> None:
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda *args, **kwargs: fake)
    monkeypatch.setattr(imaplib, "IMAP4", lambda *args, **kwargs: fake)


def _patch_smtp(monkeypatch: pytest.MonkeyPatch, fake: FakeSMTP) -> None:
    monkeypatch.setattr(smtplib, "SMTP", lambda *args, **kwargs: fake)
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *args, **kwargs: fake)


# --- 安装与凭据 -------------------------------------------------------------


def test_install_seals_credentials_without_exposing_password(
    settings: Settings, db_schema: None
) -> None:
    with _client(settings, db_schema) as client:
        installed = _install(client)

        assert installed.status_code == 200, installed.text
        state = installed.json()["data"]
        assert state["connected"] is True
        assert state["account"] == USERNAME
        assert state["display_name"] == USERNAME
        assert PASSWORD not in installed.text

        listed = client.get(f"{PREFIX}/connectors", headers=_headers())
        assert listed.status_code == 200, listed.text
        email_row = next(row for row in listed.json()["data"] if row["id"] == "email")
        assert email_row["account"] == USERNAME
        assert PASSWORD not in listed.text

        async def _ciphertext() -> bytes:
            async with client.app.state.container.session_factory() as session:
                row = (
                    await session.scalars(
                        select(ConnectorInstallation).where(
                            ConnectorInstallation.account_key == ACCOUNT_A,
                            ConnectorInstallation.connector_id == "email",
                        )
                    )
                ).first()
                assert row is not None
                assert row.credential_ciphertext is not None
                return row.credential_ciphertext

        ciphertext = asyncio.run(_ciphertext())
        assert PASSWORD.encode() not in ciphertext

    # 新进程 / 新容器只要拿到同一把密钥库主密钥就能解密，且能看到用户名
    restarted = Container(settings)
    try:
        credentials = asyncio.run(restarted.email.credentials(ACCOUNT_A))
    finally:
        asyncio.run(restarted.engine.dispose())
    assert credentials["username"] == USERNAME
    assert credentials["password"] == PASSWORD
    assert credentials["imap_host"] == "imap.example.test"
    assert credentials["smtp_host"] == "smtp.example.test"


def test_install_accepts_password_only_in_settings(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema) as client:
        response = _install(
            client,
            api_key=None,
            settings_overrides={"password": PASSWORD},
        )
        assert response.status_code == 200, response.text
        assert PASSWORD not in response.text


@pytest.mark.parametrize(
    ("overrides", "remove", "api_key", "field"),
    [
        ({"imap_host": "", "smtp_host": ""}, (), PASSWORD, "imap_host"),
        ({"imap_host": "imap example test"}, (), PASSWORD, "imap_host"),
        ({"imap_port": 70000}, (), PASSWORD, "imap_port"),
        ({"smtp_ssl": True, "smtp_starttls": True}, (), PASSWORD, "smtp_starttls"),
        ({}, ("username",), PASSWORD, "username"),
        ({}, (), None, "password"),
        ({"password": PASSWORD}, (), "different-password", "password"),
    ],
)
def test_install_rejects_invalid_credentials(
    settings: Settings,
    db_schema: None,
    overrides: dict[str, Any],
    remove: tuple[str, ...],
    api_key: str | None,
    field: str,
) -> None:
    with _client(settings, db_schema) as client:
        response = _install(
            client,
            api_key=api_key,
            settings_overrides=overrides,
            remove=remove,
        )

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["data"]["field"] == field
    assert PASSWORD not in response.text


def test_install_twice_returns_conflict(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema) as client:
        assert _install(client).status_code == 200
        duplicate = _install(client)

    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "conflict"


# --- 账号隔离 ---------------------------------------------------------------


def test_email_is_isolated_between_accounts(
    settings: Settings,
    db_schema: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeIMAP({b"1": (_message_bytes(subject="A only"), False)})
    _patch_imap(monkeypatch, fake)

    with _client(settings, db_schema) as client:
        assert _install(client, ACCOUNT_A).status_code == 200

        listed_a = client.get(f"{PREFIX}/connectors/email/messages", headers=_headers(ACCOUNT_A))
        assert listed_a.status_code == 200, listed_a.text
        assert listed_a.json()["data"]["count"] == 1

        listed_b = client.get(f"{PREFIX}/connectors/email/messages", headers=_headers(ACCOUNT_B))
        assert listed_b.status_code == 404
        assert listed_b.json()["code"] == "not_found"

        send_b = client.post(
            f"{PREFIX}/connectors/email/send",
            json={"to": "bob@example.test", "subject": "hi", "body": "x"},
            headers=_headers(ACCOUNT_B),
        )
        assert send_b.status_code == 404

        get_b = client.get(f"{PREFIX}/connectors/email/messages/1", headers=_headers(ACCOUNT_B))
        assert get_b.status_code == 404

        test_b = client.post(f"{PREFIX}/connectors/email/test", headers=_headers(ACCOUNT_B))
        assert test_b.status_code == 404


# --- 列表 -------------------------------------------------------------------


def test_list_returns_headers_only_newest_first(
    settings: Settings,
    db_schema: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = {
        b"1": (_message_bytes(subject="Older", body="older body", html=None), True),
        b"2": (_message_bytes(subject="Newer", body=BODY_MARKER), False),
    }
    fake = FakeIMAP(messages)
    _patch_imap(monkeypatch, fake)

    with _client(settings, db_schema) as client:
        assert _install(client).status_code == 200
        response = client.get(
            f"{PREFIX}/connectors/email/messages?mailbox=Archive&limit=1",
            headers=_headers(),
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["mailbox"] == "Archive"
    assert data["count"] == 1
    assert len(data["messages"]) == 1
    message = data["messages"][0]
    assert message["id"] == "2"
    assert message["uid"] == 2
    assert message["subject"] == "Newer"
    assert message["from"] == "bob@example.test"
    assert message["to"] == USERNAME
    assert message["seen"] is False
    assert "body" not in message
    assert BODY_MARKER not in response.text
    assert fake.mailbox == "Archive"
    assert fake.readonly is True
    assert fake.logged_out is True


def test_list_since_uid_returns_oldest_window(
    settings: Settings,
    db_schema: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """传 since_uid 时取水位线之后最老的 N 封；默认仍是最新的 N 封。"""
    messages = {
        str(uid).encode(): (_message_bytes(subject=f"message {uid}"), False) for uid in range(1, 21)
    }
    _patch_imap(monkeypatch, FakeIMAP(messages))

    async def _scenario() -> None:
        container = Container(settings)
        try:
            await container.connector_framework.install(
                ACCOUNT_A,
                "email",
                auth_type="api_key",
                api_key=PASSWORD,
                settings=dict(INSTALL_SETTINGS),
            )

            default = await container.email.list_messages(ACCOUNT_A, limit=3)
            assert [message["uid"] for message in default] == [20, 19, 18]

            window = await container.email.list_messages(ACCOUNT_A, limit=3, since_uid=5)
            assert [message["uid"] for message in window] == [6, 7, 8]
            # 严格大于水位线：已处理过的那封不会被再次返回。
            assert all(message["uid"] > 5 for message in window)
        finally:
            await container.dispose()

    asyncio.run(_scenario())


# --- 单封邮件 ---------------------------------------------------------------


def test_get_message_returns_plain_text_body(
    settings: Settings,
    db_schema: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _message_bytes(subject="Report", body=BODY_MARKER, html="<p>html version</p>")
    _patch_imap(monkeypatch, FakeIMAP({b"7": (raw, True)}))

    with _client(settings, db_schema) as client:
        assert _install(client).status_code == 200
        response = client.get(f"{PREFIX}/connectors/email/messages/7", headers=_headers())

    assert response.status_code == 200, response.text
    message = response.json()["data"]
    assert message["id"] == "7"
    assert message["subject"] == "Report"
    assert message["body"].strip() == BODY_MARKER
    assert message["content_type"] == "text/plain"
    assert message["body_truncated"] is False
    assert message["seen"] is True


def test_get_message_returns_html_when_no_plain_text(
    settings: Settings,
    db_schema: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _message_bytes(subject="HTML only", body="<p>only html</p>", html_only=True)
    _patch_imap(monkeypatch, FakeIMAP({b"3": (raw, False)}))

    with _client(settings, db_schema) as client:
        assert _install(client).status_code == 200
        response = client.get(f"{PREFIX}/connectors/email/messages/3", headers=_headers())

    assert response.status_code == 200, response.text
    message = response.json()["data"]
    assert message["content_type"] == "text/html"
    assert "<p>only html</p>" in message["body"]


def test_get_message_rejects_invalid_id(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema) as client:
        assert _install(client).status_code == 200
        response = client.get(f"{PREFIX}/connectors/email/messages/not-a-uid", headers=_headers())

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


# --- 发信 -------------------------------------------------------------------


def test_send_message_success_and_logs_do_not_leak(
    settings: Settings,
    db_schema: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSMTP()
    _patch_smtp(monkeypatch, fake)
    capture = _CaptureHandler()
    logger = logging.getLogger("flux.core.connectors.email")
    previous_level = logger.level

    with _client(settings, db_schema) as client:
        # create_app 会重置根 logger 的 handler，所以这里直接把捕获器挂在模块 logger 上
        logger.addHandler(capture)
        logger.setLevel(logging.INFO)
        try:
            assert _install(client).status_code == 200
            response = client.post(
                f"{PREFIX}/connectors/email/send",
                json={
                    "to": ["bob@example.test"],
                    "cc": ["carol@example.test"],
                    "subject": "Weekly report",
                    "body": BODY_MARKER,
                },
                headers=_headers(),
            )
        finally:
            logger.removeHandler(capture)
            logger.setLevel(previous_level)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["sent"] is True
    assert data["recipients"] == 2
    assert data["to"] == ["bob@example.test"]
    assert data["cc"] == ["carol@example.test"]
    assert data["message_id"].startswith("<")
    assert PASSWORD not in response.text
    assert BODY_MARKER not in response.text

    assert len(fake.sent) == 1
    sent = fake.sent[0]
    assert sent["To"] == "bob@example.test"
    assert sent["Cc"] == "carol@example.test"
    assert sent["Subject"] == "Weekly report"
    assert sent.get_content().strip() == BODY_MARKER
    assert fake.credentials == (USERNAME, PASSWORD)
    assert fake.starttls_used is True

    # 日志只允许出现 message-id / 收件人数 / 主题长度
    log_text = "\n".join(capture.messages)
    assert PASSWORD not in log_text
    assert BODY_MARKER not in log_text
    assert "Weekly report" not in log_text
    assert "email.send" in log_text


def test_send_rejects_bad_recipients(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema) as client:
        assert _install(client).status_code == 200
        response = client.post(
            f"{PREFIX}/connectors/email/send",
            json={"to": ["not-an-address"], "subject": "hi", "body": "x"},
            headers=_headers(),
        )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


# --- 连通性自检 -------------------------------------------------------------


def test_connection_reports_both_sides(
    settings: Settings,
    db_schema: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imap = FakeIMAP()
    smtp = FakeSMTP()
    _patch_imap(monkeypatch, imap)
    _patch_smtp(monkeypatch, smtp)

    with _client(settings, db_schema) as client:
        assert _install(client).status_code == 200
        response = client.post(f"{PREFIX}/connectors/email/test", headers=_headers())

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["ok"] is True
    assert data["imap"] == {"configured": True, "ok": True, "error": ""}
    assert data["smtp"] == {"configured": True, "ok": True, "error": ""}
    assert smtp.sent == []
    assert smtp.noop_called is True
    assert imap.logged_out is True


def test_connection_reports_failed_side(
    settings: Settings,
    db_schema: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_imap(monkeypatch, FakeIMAP(noop_error=RuntimeError("imap down")))
    _patch_smtp(monkeypatch, FakeSMTP())

    with _client(settings, db_schema) as client:
        assert _install(client).status_code == 200
        response = client.post(f"{PREFIX}/connectors/email/test", headers=_headers())

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["ok"] is False
    assert data["imap"]["ok"] is False
    assert data["imap"]["error"]
    assert data["smtp"]["ok"] is True
    assert PASSWORD not in response.text


# --- 未安装 / 卸载 / 解密失败 ----------------------------------------------


def test_actions_require_installation(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema) as client:
        list_response = client.get(f"{PREFIX}/connectors/email/messages", headers=_headers())
        get_response = client.get(f"{PREFIX}/connectors/email/messages/1", headers=_headers())
        send_response = client.post(
            f"{PREFIX}/connectors/email/send",
            json={"to": "bob@example.test", "subject": "hi", "body": "x"},
            headers=_headers(),
        )
        test_response = client.post(f"{PREFIX}/connectors/email/test", headers=_headers())

    for response in (list_response, get_response, send_response, test_response):
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"


def test_uninstall_removes_access(
    settings: Settings,
    db_schema: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_imap(monkeypatch, FakeIMAP({b"1": (_message_bytes(), False)}))

    with _client(settings, db_schema) as client:
        assert _install(client).status_code == 200
        removed = client.delete(f"{PREFIX}/connectors/email", headers=_headers())
        assert removed.status_code == 200
        assert removed.json()["data"]["connected"] is False
        assert removed.json()["data"]["account"] == ""

        after = client.get(f"{PREFIX}/connectors/email/messages", headers=_headers())

    assert after.status_code == 404
    assert after.json()["code"] == "not_found"


def test_decrypt_failure_is_fail_closed(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema) as client:
        assert _install(client).status_code == 200

        async def _corrupt() -> None:
            async with client.app.state.container.session_factory() as session:
                row = (
                    await session.scalars(
                        select(ConnectorInstallation).where(
                            ConnectorInstallation.account_key == ACCOUNT_A,
                            ConnectorInstallation.connector_id == "email",
                        )
                    )
                ).first()
                assert row is not None
                row.credential_ciphertext = b"not-a-valid-sealed-document"
                await session.commit()

        asyncio.run(_corrupt())

        # 状态列表仍然可用（账号名留空），动作面 fail-closed 成 503
        listed = client.get(f"{PREFIX}/connectors", headers=_headers())
        assert listed.status_code == 200, listed.text
        email_row = next(row for row in listed.json()["data"] if row["id"] == "email")
        assert email_row["connected"] is True
        assert email_row["account"] == ""

        response = client.get(f"{PREFIX}/connectors/email/messages", headers=_headers())

    assert response.status_code == 503
    assert response.json()["code"] == "configuration_error"
    assert PASSWORD not in response.text


# --- IMAP / SMTP 错误 -------------------------------------------------------


def test_imap_error_returns_502_without_leaking(
    settings: Settings,
    db_schema: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeIMAP(login_error=imaplib.IMAP4.error("bad credentials: " + PASSWORD))
    _patch_imap(monkeypatch, fake)

    with _client(settings, db_schema) as client:
        assert _install(client).status_code == 200
        response = client.get(f"{PREFIX}/connectors/email/messages", headers=_headers())

    assert response.status_code == 502
    assert response.json()["code"] == "provider_error"
    assert PASSWORD not in response.text
    assert "bad credentials" not in response.text


def test_smtp_error_returns_502_without_leaking(
    settings: Settings,
    db_schema: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSMTP(send_error=smtplib.SMTPException("relay denied: " + PASSWORD))
    _patch_smtp(monkeypatch, fake)

    with _client(settings, db_schema) as client:
        assert _install(client).status_code == 200
        response = client.post(
            f"{PREFIX}/connectors/email/send",
            json={"to": "bob@example.test", "subject": "hi", "body": BODY_MARKER},
            headers=_headers(),
        )

    assert response.status_code == 502
    assert response.json()["code"] == "provider_error"
    assert PASSWORD not in response.text
    assert "relay denied" not in response.text
    assert BODY_MARKER not in response.text
