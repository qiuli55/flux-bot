"""审批规则 REST API：CRUD、参数复核、事件与跨用户隔离。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from flux.config import Settings
from flux.container import Container
from flux.core.event.bus import Events
from flux.main import create_app
from tests.cloud_jwt import mint_cloud_token

PREFIX = "/api/v1"
RULES = f"{PREFIX}/approval-rules"


def _auth(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_cloud_token(user)}"}


@contextmanager
def _client(settings: Settings) -> Iterator[tuple[TestClient, Container]]:
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app.state.container


def _payment_rule(client: TestClient, user: str = "2", **overrides: object) -> dict:
    body = {
        "type": "payment_amount_over",
        "params": {"amount": 500},
        "description": "超过 500 元要确认",
    }
    body.update(overrides)
    response = client.post(RULES, json=body, headers=_auth(user))
    assert response.status_code == 200, response.text
    return response.json()["data"]


# ---------------------------------------------------------------- CRUD


def test_create_list_get_patch_delete(settings: Settings, db_schema: None) -> None:
    with _client(settings) as (client, _container):
        created = _payment_rule(client)
        assert created["cloud_user_id"] == "2"
        assert created["enabled"] is True
        assert created["created_by_kind"] == "user"
        assert created["created_by_bot_id"] is None
        # 币种默认 CNY，参数已归一化
        assert created["params"] == {"amount": 500.0, "currency": "CNY"}

        listed = client.get(RULES, headers=_auth("2")).json()["data"]
        assert [row["id"] for row in listed] == [created["id"]]

        fetched = client.get(f"{RULES}/{created['id']}", headers=_auth("2")).json()["data"]
        assert fetched["description"] == "超过 500 元要确认"

        patched = client.patch(
            f"{RULES}/{created['id']}",
            headers=_auth("2"),
            json={"params": {"amount": 1000, "currency": "usd"}, "enabled": False},
        )
        assert patched.status_code == 200, patched.text
        data = patched.json()["data"]
        assert data["params"] == {"amount": 1000.0, "currency": "USD"}
        assert data["enabled"] is False
        # 未提供 description 时保留原文
        assert data["description"] == "超过 500 元要确认"

        # 列表包含停用规则（设置页需要看到全部）
        assert [row["enabled"] for row in client.get(RULES, headers=_auth("2")).json()["data"]] == [
            False
        ]

        deleted = client.delete(f"{RULES}/{created['id']}", headers=_auth("2"))
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["data"] == {"deleted": True, "rule_id": created["id"]}
        assert client.get(RULES, headers=_auth("2")).json()["data"] == []


def test_default_chinese_description_is_generated(settings: Settings, db_schema: None) -> None:
    with _client(settings) as (client, _container):
        created = _payment_rule(client, description=None)
        del created  # 只是为了覆盖 description 省略路径
        response = client.post(
            RULES,
            headers=_auth("2"),
            json={"type": "outbound_message", "params": {"channel": "feishu"}},
        )
        assert response.status_code == 200, response.text
        assert "飞书" in response.json()["data"]["description"]


@pytest.mark.parametrize(
    "body",
    [
        {"type": "command_pattern", "params": {"pattern": "(", "style": "regex"}},
        {"type": "command_pattern", "params": {"pattern": "(a+)+", "style": "regex"}},
        {"type": "payment_amount_over", "params": {}},
        {"type": "unknown", "params": {}},
        {"type": "outbound_message", "params": {"channel": "email"}},
    ],
)
def test_invalid_rules_are_400(settings: Settings, db_schema: None, body: dict) -> None:
    with _client(settings) as (client, _container):
        response = client.post(RULES, headers=_auth("2"), json=body)
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "bad_request"


def test_patch_revalidates_params(settings: Settings, db_schema: None) -> None:
    with _client(settings) as (client, _container):
        created = _payment_rule(client)
        response = client.patch(
            f"{RULES}/{created['id']}",
            headers=_auth("2"),
            json={"type": "command_pattern", "params": {"pattern": "*", "style": "glob"}},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["type"] == "command_pattern"

        # 改成非法参数必须 400，且原规则保持不变
        bad = client.patch(
            f"{RULES}/{created['id']}",
            headers=_auth("2"),
            json={"params": {"pattern": "(", "style": "regex"}},
        )
        assert bad.status_code == 400
        fresh = client.get(f"{RULES}/{created['id']}", headers=_auth("2")).json()["data"]
        assert fresh["params"] == {"pattern": "*", "style": "glob"}


# ---------------------------------------------------------------- 隔离与鉴权


def test_cross_user_isolated(settings: Settings, db_schema: None) -> None:
    with _client(settings) as (client, _container):
        mine = _payment_rule(client, user="2")

        assert client.get(RULES, headers=_auth("3")).json()["data"] == []

        for method, suffix in (
            ("get", ""),
            ("patch", ""),
            ("delete", ""),
        ):
            kwargs = {"json": {"enabled": False}} if method == "patch" else {}
            response = getattr(client, method)(
                f"{RULES}/{mine['id']}{suffix}", headers=_auth("3"), **kwargs
            )
            assert response.status_code == 404, (method, response.text)
            assert response.json()["code"] == "not_found"

        # 用户 2 的规则仍然在、且没被改
        still = client.get(f"{RULES}/{mine['id']}", headers=_auth("2")).json()["data"]
        assert still["enabled"] is True


def test_unauthenticated_is_cloud_login_required(settings: Settings, db_schema: None) -> None:
    # 没有配置主用户时，本机匿名请求也无法解析成任何云用户。
    cfg = settings.model_copy(update={"owner_cloud_user_id": None})
    with _client(cfg) as (client, _container):
        response = client.get(RULES)
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "cloud_login_required"


def test_list_shows_bot_created_rule(settings: Settings, db_schema: None) -> None:
    with _client(settings) as (client, container):
        import asyncio
        import uuid

        bot_id = uuid.uuid4()

        async def _create_bot_rule() -> None:
            await container.approval_rules.create(
                cloud_user_id="2",
                rule_type="tool_match",
                params={"tool_name": "terminal.execute"},
                created_by_kind="bot",
                created_by_bot_id=bot_id,
            )

        asyncio.run(_create_bot_rule())
        rows = client.get(RULES, headers=_auth("2")).json()["data"]
        assert len(rows) == 1
        assert rows[0]["created_by_kind"] == "bot"
        assert rows[0]["created_by_bot_id"] == str(bot_id)


# ---------------------------------------------------------------- 事件


def test_crud_publishes_rule_events(settings: Settings, db_schema: None) -> None:
    with _client(settings) as (client, container):
        seen: list[tuple[str, dict]] = []

        async def _record(event: str, payload: dict) -> None:
            seen.append((event, payload))

        for name in (
            Events.APPROVAL_RULE_CREATED,
            Events.APPROVAL_RULE_UPDATED,
            Events.APPROVAL_RULE_DELETED,
        ):
            container.bus.subscribe(name, _record)

        created = _payment_rule(client)
        client.patch(f"{RULES}/{created['id']}", headers=_auth("2"), json={"enabled": False})
        client.delete(f"{RULES}/{created['id']}", headers=_auth("2"))

        assert [event for event, _ in seen] == [
            Events.APPROVAL_RULE_CREATED,
            Events.APPROVAL_RULE_UPDATED,
            Events.APPROVAL_RULE_DELETED,
        ]
        for _event, payload in seen:
            assert payload["id"] == created["id"]
            assert payload["cloud_user_id"] == "2"
            assert payload["type"] == "payment_amount_over"
            assert "params" not in payload
            assert "description" not in payload
