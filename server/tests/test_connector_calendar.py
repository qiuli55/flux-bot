"""日历连接器（CalDAV / ICS）测试。

上游一律用 httpx.MockTransport 顶替，不访问真实网络。ICS 解析、窗口过滤、
CalDAV REPORT / PROPFIND / PUT、凭据加密与账号隔离都在这里覆盖。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from flux.config import Settings
from flux.core.key_vault.service import KeyVaultService
from flux.main import create_app
from tests.cloud_jwt import mint_cloud_token

PREFIX = "/api/v1"
CALENDAR = f"{PREFIX}/connectors/calendar"
ACCOUNT_A = "account-a"
ACCOUNT_B = "account-b"
ICS_URL = "https://calendar.example/feed.ics"
CALDAV_URL = "https://dav.example/calendars/alice/"

ICS_TEXT = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Flux Test//EN
BEGIN:VEVENT
UID:event-1@example.com
SUMMARY:Team sync
DTSTART:20261110T090000Z
DTEND:20261110T100000Z
LOCATION:Room 1
STATUS:CONFIRMED
DESCRIPTION:Weekly sync
END:VEVENT
BEGIN:VEVENT
UID:event-2@example.com
SUMMARY:Release
DTSTART:20261120T140000Z
DTEND:20261120T150000Z
STATUS:TENTATIVE
DESCRIPTION:Release window
END:VEVENT
END:VCALENDAR
"""

REPORT_XML = """<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:response>
    <d:href>/calendars/alice/remote.ics</d:href>
    <d:propstat>
      <d:prop>
        <d:getetag>"etag-1"</d:getetag>
        <c:calendar-data>BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:caldav-1@example.com
SUMMARY:CalDAV planning
DTSTART:20261201T100000Z
DTEND:20261201T110000Z
LOCATION:Room 7
STATUS:CONFIRMED
DESCRIPTION:Planning session
END:VEVENT
END:VCALENDAR</c:calendar-data>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>
"""

PROPFIND_XML = """<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:response>
    <d:href>/calendars/alice/</d:href>
    <d:propstat>
      <d:prop>
        <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
        <d:displayname>Alice</d:displayname>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>
"""


def _headers(account: str) -> dict[str, str]:
    """Phase 2：账号身份来自云 JWT（sub）。"""
    return {"Authorization": f"Bearer {mint_cloud_token(account)}"}


def _client(
    settings: Settings,
    db_schema: None,
    handler: Callable[[httpx.Request], httpx.Response],
) -> TestClient:
    app = create_app(settings)
    app.state.container.calendar._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TestClient(app)


def _install(
    client: TestClient,
    *,
    account: str = ACCOUNT_A,
    api_key: str | None = None,
    settings: dict[str, object] | None = None,
) -> httpx.Response:
    payload: dict[str, object] = {"auth_type": "api_key"}
    if api_key is not None:
        payload["api_key"] = api_key
    if settings is not None:
        payload["settings"] = settings
    return client.post(
        f"{CALENDAR}/install",
        json=payload,
        headers=_headers(account),
    )


def _install_ics(client: TestClient, *, account: str = ACCOUNT_A) -> None:
    response = _install(
        client,
        account=account,
        settings={"mode": "ics", "url": ICS_URL},
    )
    assert response.status_code == 200, response.text


def _ics_handler(request: httpx.Request) -> httpx.Response:
    if request.method == "GET" and request.url.path == "/feed.ics":
        return httpx.Response(
            200,
            text=ICS_TEXT,
            headers={"content-type": "text/calendar; charset=utf-8"},
        )
    return httpx.Response(404, text="unexpected")


def _caldav_handler(seen: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if request.method == "REPORT" and path == "/calendars/alice/":
            return httpx.Response(
                207,
                text=REPORT_XML,
                headers={"content-type": "application/xml; charset=utf-8"},
            )
        if request.method == "PROPFIND" and path == "/calendars/alice/":
            return httpx.Response(
                207,
                text=PROPFIND_XML,
                headers={"content-type": "application/xml; charset=utf-8"},
            )
        if request.method == "PUT" and path.startswith("/calendars/alice/"):
            return httpx.Response(201)
        return httpx.Response(404, text="unexpected")

    return handler


def _db_path(settings: Settings) -> str:
    return str(make_url(settings.database_url).database)


# --- 安装与凭据 ---


def test_install_seals_password_and_never_returns_it(settings: Settings, db_schema: None) -> None:
    password = "caldav-super-secret"
    with _client(settings, db_schema, _caldav_handler([])) as client:
        installed = _install(
            client,
            api_key=password,
            settings={
                "mode": "caldav",
                "url": CALDAV_URL,
                "username": "alice",
                "timeout": 12,
            },
        )
        assert installed.status_code == 200, installed.text
        state = installed.json()["data"]
        assert state["connected"] is True
        assert state["mode"] == "caldav"
        assert state["account"] == "alice"
        assert state["display_name"] == "alice"
        assert state["host"] == "dav.example"
        assert state["scopes"] == ["calendar.read", "calendar.write"]
        assert password not in installed.text
        assert CALDAV_URL not in installed.text

        # 密文落库且不是明文
        with sqlite3.connect(_db_path(settings)) as conn:
            row = conn.execute(
                "SELECT credential_ciphertext FROM connector_installations "
                "WHERE account_key = ? AND connector_id = ?",
                (ACCOUNT_A, "calendar"),
            ).fetchone()
        assert row is not None and row[0] is not None
        ciphertext = bytes(row[0])
        assert password.encode() not in ciphertext
        assert b"alice" not in ciphertext

        # 同一把密钥库主密钥可以解出完整凭据
        framework = client.app.state.container.connector_framework
        document = json.loads(asyncio.run(framework.credential_for(ACCOUNT_A, "calendar")))
        assert document["url"] == CALDAV_URL
        assert document["username"] == "alice"
        assert document["password"] == password
        assert document["timeout"] == 12

        # 重复安装冲突
        duplicate = _install(
            client,
            api_key=password,
            settings={"mode": "caldav", "url": CALDAV_URL, "username": "alice"},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "conflict"


def test_public_ics_url_still_validates_https(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema, _ics_handler) as client:
        rejected_http = _install(
            client,
            settings={"mode": "ics", "url": "http://calendar.example/feed.ics"},
        )
        rejected_junk = _install(
            client,
            settings={"mode": "ics", "url": "not a url"},
        )

    assert rejected_http.status_code == 400
    assert rejected_http.json()["code"] == "bad_request"
    assert "https" in rejected_http.json()["message"]
    assert rejected_junk.status_code == 400


def test_caldav_requires_username_and_password(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema, _caldav_handler([])) as client:
        missing_password = _install(
            client,
            settings={"mode": "caldav", "url": CALDAV_URL, "username": "alice"},
        )
        missing_username = _install(
            client,
            settings={"mode": "caldav", "url": CALDAV_URL, "password": "secret"},
        )

    assert missing_password.status_code == 400
    assert "密码" in missing_password.json()["message"]
    assert missing_username.status_code == 400


def test_account_isolation(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema, _ics_handler) as client:
        _install_ics(client)

        rows_b = client.get(f"{PREFIX}/connectors", headers=_headers(ACCOUNT_B)).json()["data"]
        calendar_b = next(row for row in rows_b if row["id"] == "calendar")
        assert calendar_b["connected"] is False
        assert calendar_b["account"] == ""
        assert calendar_b["mode"] == ""

        assert client.get(f"{CALENDAR}/events", headers=_headers(ACCOUNT_B)).status_code == 404

        # B 的卸载不能影响 A
        removed_b = client.delete(f"{CALENDAR}", headers=_headers(ACCOUNT_B))
        assert removed_b.status_code == 200
        assert removed_b.json()["data"]["connected"] is False

        rows_a = client.get(f"{PREFIX}/connectors", headers=_headers(ACCOUNT_A)).json()["data"]
        calendar_a = next(row for row in rows_a if row["id"] == "calendar")
        assert calendar_a["connected"] is True
        assert calendar_a["account"] == "calendar.example"

        listed = client.get(f"{CALENDAR}/events", headers=_headers(ACCOUNT_A))
        assert listed.status_code == 200, listed.text


# --- ICS 读取 ---


def test_ics_list_filters_window_and_omits_raw(settings: Settings, db_schema: None) -> None:
    window = {"start": "2026-11-09T00:00:00Z", "end": "2026-11-16T00:00:00Z"}
    with _client(settings, db_schema, _ics_handler) as client:
        _install_ics(client)
        response = client.get(f"{CALENDAR}/events", params=window, headers=_headers(ACCOUNT_A))
        assert response.status_code == 200, response.text
        events = response.json()["data"]
        assert [event["uid"] for event in events] == ["event-1@example.com"]
        event = events[0]
        assert event["title"] == "Team sync"
        assert event["summary"] == "Team sync"
        assert event["start"] == "2026-11-10T09:00:00+00:00"
        assert event["end"] == "2026-11-10T10:00:00+00:00"
        assert event["location"] == "Room 1"
        assert event["status"] == "CONFIRMED"
        # 列表不返回 description 与原始 ICS
        assert "description" not in event
        assert "raw" not in event
        assert "BEGIN:VCALENDAR" not in response.text
        assert "Weekly sync" not in response.text

        wide = client.get(
            f"{CALENDAR}/events",
            params={"start": "2026-11-01T00:00:00Z", "end": "2026-12-01T00:00:00Z"},
            headers=_headers(ACCOUNT_A),
        )
        assert wide.status_code == 200, wide.text
        assert [event["uid"] for event in wide.json()["data"]] == [
            "event-1@example.com",
            "event-2@example.com",
        ]

        limited = client.get(
            f"{CALENDAR}/events",
            params={
                "start": "2026-11-01T00:00:00Z",
                "end": "2026-12-01T00:00:00Z",
                "limit": 1,
            },
            headers=_headers(ACCOUNT_A),
        )
        assert [event["uid"] for event in limited.json()["data"]] == ["event-1@example.com"]


def test_ics_get_event_includes_description_and_raw_subset(
    settings: Settings, db_schema: None
) -> None:
    with _client(settings, db_schema, _ics_handler) as client:
        _install_ics(client)
        response = client.get(f"{CALENDAR}/events/event-1@example.com", headers=_headers(ACCOUNT_A))
        assert response.status_code == 200, response.text
        event = response.json()["data"]
        assert event["uid"] == "event-1@example.com"
        assert event["description"] == "Weekly sync"
        assert "UID:event-1@example.com" in event["raw"]
        assert "SUMMARY:Team sync" in event["raw"]

        missing = client.get(
            f"{CALENDAR}/events/does-not-exist@example.com", headers=_headers(ACCOUNT_A)
        )
        assert missing.status_code == 404
        assert missing.json()["code"] == "not_found"


def test_test_connection_ics_reports_ok(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema, _ics_handler) as client:
        _install_ics(client)
        response = client.post(f"{CALENDAR}/test", headers=_headers(ACCOUNT_A))

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["ok"] is True
    assert data["mode"] == "ics"
    assert data["events"] == 2


def test_ics_create_is_rejected(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema, _ics_handler) as client:
        _install_ics(client)
        response = client.post(
            f"{CALENDAR}/events",
            json={
                "title": "Should fail",
                "start": "2026-12-01T10:00:00Z",
                "end": "2026-12-01T11:00:00Z",
            },
            headers=_headers(ACCOUNT_A),
        )

    assert response.status_code == 400
    assert "只读" in response.json()["message"]


# --- CalDAV 读取与新建 ---


def test_caldav_list_and_create(settings: Settings, db_schema: None) -> None:
    seen: list[httpx.Request] = []
    with _client(settings, db_schema, _caldav_handler(seen)) as client:
        installed = _install(
            client,
            api_key="caldav-pass",
            settings={"mode": "caldav", "url": CALDAV_URL, "username": "alice"},
        )
        assert installed.status_code == 200, installed.text

        listed = client.get(
            f"{CALENDAR}/events",
            params={"start": "2026-12-01T00:00:00Z", "end": "2026-12-02T00:00:00Z"},
            headers=_headers(ACCOUNT_A),
        )
        assert listed.status_code == 200, listed.text
        events = listed.json()["data"]
        assert [event["uid"] for event in events] == ["caldav-1@example.com"]
        assert events[0]["title"] == "CalDAV planning"

        created = client.post(
            f"{CALENDAR}/events",
            json={
                "title": "New meeting",
                "start": "2026-12-03T10:00:00Z",
                "end": "2026-12-03T11:00:00Z",
                "description": "Agenda",
                "location": "Room 9",
            },
            headers=_headers(ACCOUNT_A),
        )
        assert created.status_code == 200, created.text
        payload = created.json()["data"]
        assert payload["uid"].endswith("@flux.local")
        assert payload["title"] == "New meeting"

        puts = [request for request in seen if request.method == "PUT"]
        assert len(puts) == 1
        put = puts[0]
        assert put.url.path.startswith("/calendars/alice/")
        assert put.url.path.endswith(".ics")
        body = put.content.decode("utf-8")
        assert "BEGIN:VEVENT" in body
        assert "SUMMARY:New meeting" in body
        assert "LOCATION:Room 9" in body
        assert payload["uid"] in body

        tested = client.post(f"{CALENDAR}/test", headers=_headers(ACCOUNT_A))
        assert tested.status_code == 200, tested.text
        assert tested.json()["data"] == {"ok": True, "mode": "caldav", "status": 207}


# --- 错误路径 ---


def test_not_installed_returns_404(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema, _ics_handler) as client:
        listed = client.get(f"{CALENDAR}/events", headers=_headers(ACCOUNT_A))
        detail = client.get(f"{CALENDAR}/events/event-1@example.com", headers=_headers(ACCOUNT_A))
        created = client.post(
            f"{CALENDAR}/events",
            json={
                "title": "No install",
                "start": "2026-12-01T10:00:00Z",
                "end": "2026-12-01T11:00:00Z",
            },
            headers=_headers(ACCOUNT_A),
        )
        tested = client.post(f"{CALENDAR}/test", headers=_headers(ACCOUNT_A))

    assert listed.status_code == 404
    assert detail.status_code == 404
    assert created.status_code == 404
    assert tested.status_code == 404
    assert listed.json()["code"] == "not_found"


def test_key_vault_mismatch_fails_closed(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema, _ics_handler) as client:
        _install_ics(client)
        container = client.app.state.container
        # 换成另一把主密钥：已存密文无法解密，必须 503 而不是静默返回空列表。
        container.calendar._key_vault = KeyVaultService(
            container.session_factory,
            settings.model_copy(update={"key_vault_secret": "another-key-vault-secret"}),
        )
        response = client.get(f"{CALENDAR}/events", headers=_headers(ACCOUNT_A))

    assert response.status_code == 503
    assert response.json()["code"] == "configuration_error"
    assert "another-key-vault-secret" not in response.text


def test_upstream_5xx_returns_safe_502(settings: Settings, db_schema: None) -> None:
    def failing(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream secret detail")

    with _client(settings, db_schema, failing) as client:
        _install_ics(client)
        listed = client.get(f"{CALENDAR}/events", headers=_headers(ACCOUNT_A))
        tested = client.post(f"{CALENDAR}/test", headers=_headers(ACCOUNT_A))

    assert listed.status_code == 502
    assert listed.json()["code"] == "provider_error"
    assert "upstream secret detail" not in listed.text
    assert tested.status_code == 502


def test_uninstall_then_actions_return_404(settings: Settings, db_schema: None) -> None:
    with _client(settings, db_schema, _ics_handler) as client:
        _install_ics(client)
        removed = client.delete(f"{CALENDAR}", headers=_headers(ACCOUNT_A))
        assert removed.status_code == 200
        assert removed.json()["data"]["connected"] is False

        listed = client.get(f"{CALENDAR}/events", headers=_headers(ACCOUNT_A))
        tested = client.post(f"{CALENDAR}/test", headers=_headers(ACCOUNT_A))

    assert listed.status_code == 404
    assert tested.status_code == 404
