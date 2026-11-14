"""组件动作回调 API：`POST /tasks/{id}/messages/{message_id}/actions`。

口径：
- 只接受落库组件树里真实存在的 `action_id`；未知动作 / 已失效动作 404；
- 消息必须属于当前用户可访问的任务（跨用户一律 404）；
- 决策按钮走服务端校验，不能跳过 pending 状态；
- 安全输入沿用既有密钥库逻辑：明文不落消息表、每张卡只能提交一次；
- 客户端动作不经过服务端（回调路由直接拒绝）。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flux.config import Settings
from flux.container import Container
from flux.core.components.actions import route_component_action
from flux.core.task_engine.decisions import build_decision
from flux.core.task_engine.repository import MessageDraft
from flux.enums import DecisionMode, TaskStatus
from flux.errors import ValidationError
from flux.main import create_app
from tests.cloud_jwt import mint_cloud_token

PREFIX = "/api/v1"
_SECRET = "sk-test-dummy-1234"

_SECURE_CARD = {
    "title": "Grok API key",
    "description": "在控制台创建的 key",
    "placeholder": "粘贴密钥",
    "name": "Grok API key",
    "provider": "openai",
    "models": ["grok-4"],
    "base_url": "",
    "wire_api": "",
}


def _auth(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_cloud_token(user)}"}


@contextmanager
def _client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        yield client


def _create_task(client: TestClient, headers: dict[str, str] | None = None) -> str:
    response = client.post(f"{PREFIX}/tasks", json={"description": "组件动作用例"}, headers=headers)
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["id"])


def _container(client: TestClient) -> Container:
    return client.app.state.container


def _seed_decision(container: Container, task_id: str) -> tuple[str, str]:
    async def _run() -> tuple[str, str]:
        record = build_decision(
            question="选哪个方案？",
            options=[{"label": "方案 A"}, {"label": "方案 B"}],
            context="两个都能用",
            recommendation="方案 A",
            mode=DecisionMode.MANUAL,
        )
        await container.task_repo.raise_decision(uuid.UUID(task_id), record)
        await container.task_repo.set_status(
            uuid.UUID(task_id), TaskStatus.WAITING_FOR_USER_DECISION
        )
        messages = await container.task_repo.add_messages(
            uuid.UUID(task_id),
            [
                MessageDraft(
                    role="assistant",
                    kind="decision",
                    content="需要你决定",
                    payload={"decision": record},
                )
            ],
        )
        return str(messages[0].id), str(record["id"])

    return asyncio.run(_run())


def _seed_secure_card(container: Container, task_id: str) -> str:
    async def _run() -> str:
        messages = await container.task_repo.add_messages(
            uuid.UUID(task_id),
            [
                MessageDraft(
                    role="assistant",
                    kind="secure_input",
                    content="key 只会显示一次",
                    payload={"secure_input": dict(_SECURE_CARD)},
                )
            ],
        )
        return str(messages[0].id)

    return asyncio.run(_run())


def _messages(client: TestClient, task_id: str) -> list[dict[str, Any]]:
    response = client.get(f"{PREFIX}/tasks/{task_id}/messages", params={"limit": 100})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _find_button_action(client: TestClient, task_id: str, message_id: str, label: str) -> dict:
    message = next(item for item in _messages(client, task_id) if item["id"] == message_id)
    for child in message["payload"]["component"]["children"]:
        if child.get("type") == "button" and child.get("label") == label:
            return child["action"]
    raise AssertionError(f"没找到按钮 {label}")


def _find_input_action(client: TestClient, task_id: str, message_id: str) -> dict:
    message = next(item for item in _messages(client, task_id) if item["id"] == message_id)
    for child in message["payload"]["component"]["children"]:
        if child.get("type") == "input":
            return child["action"]
    raise AssertionError("没找到输入框动作")


def test_decision_button_resolves_pending_decision(client: TestClient) -> None:
    task_id = _create_task(client)
    message_id, _decision_id = _seed_decision(_container(client), task_id)
    action = _find_button_action(client, task_id, message_id, "方案 A")

    response = client.post(
        f"{PREFIX}/tasks/{task_id}/messages/{message_id}/actions",
        json={"action_id": action["action_id"]},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["decision"]["status"] == "resolved"
    assert data["decision"]["chosen"] == "方案 A"
    assert data["task"]["status"] == "running"
    assert data["message"]["kind"] == "decision_result"
    assert data["message"]["role"] == "user"

    messages = _messages(client, task_id)
    assert messages[-1]["kind"] == "decision_result"
    assert "方案 A" in messages[-1]["content"]

    # 同一个按钮再点一次：决策已不在 pending，冲突而不是重复执行
    again = client.post(
        f"{PREFIX}/tasks/{task_id}/messages/{message_id}/actions",
        json={"action_id": action["action_id"]},
    )
    assert again.status_code == 409, again.text


def test_decision_reject_button(client: TestClient) -> None:
    task_id = _create_task(client)
    message_id, _decision_id = _seed_decision(_container(client), task_id)
    action = _find_button_action(client, task_id, message_id, "都不合适")
    response = client.post(
        f"{PREFIX}/tasks/{task_id}/messages/{message_id}/actions",
        json={"action_id": action["action_id"], "note": "都不行"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["decision"]["status"] == "rejected"
    assert "都不行" in data["message"]["content"]


def test_unknown_action_and_unknown_message_are_404(client: TestClient) -> None:
    task_id = _create_task(client)
    message_id, _ = _seed_decision(_container(client), task_id)
    unknown = client.post(
        f"{PREFIX}/tasks/{task_id}/messages/{message_id}/actions",
        json={"action_id": "decision:forged:0"},
    )
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["code"] == "not_found"

    missing = client.post(
        f"{PREFIX}/tasks/{task_id}/messages/{uuid.uuid4()}/actions",
        json={"action_id": "decision:forged:0"},
    )
    assert missing.status_code == 404, missing.text


async def test_client_action_is_not_routed_to_server(client: TestClient) -> None:
    """客户端动作由 App 自己处理；即使被提交到服务端也不会执行。"""
    task_id = _create_task(client)
    container = _container(client)
    messages = await container.task_repo.add_messages(
        uuid.UUID(task_id),
        [
            MessageDraft(
                role="assistant",
                kind="handoff",
                content="已交接",
                payload={"from_agent": "旧", "to_agent": "新"},
            )
        ],
    )
    task = await container.task_repo.get(uuid.UUID(task_id))
    with pytest.raises(ValidationError) as excinfo:
        await route_component_action(
            container,
            task=task,
            message=messages[0],
            action={"type": "client", "name": "open_desktop"},
            value=None,
            option=None,
            note=None,
            cloud_user_id="1",
        )
    assert "客户端处理" in excinfo.value.message


def test_secure_input_action_stores_key_without_echoing_plaintext(client: TestClient) -> None:
    task_id = _create_task(client)
    message_id = _seed_secure_card(_container(client), task_id)
    action = _find_input_action(client, task_id, message_id)

    response = client.post(
        f"{PREFIX}/tasks/{task_id}/messages/{message_id}/actions",
        json={"action_id": action["action_id"], "value": _SECRET},
    )
    assert response.status_code == 200, response.text
    assert _SECRET not in response.text
    data = response.json()["data"]
    assert data["key"]["name"] == "Grok API key"
    assert data["key"]["key_hint"] == "••••1234"
    assert data["message"]["kind"] == "secure_input_result"
    assert _SECRET not in str(data["message"])

    messages = _messages(client, task_id)
    assert _SECRET not in str(messages)
    assert messages[-1]["kind"] == "secure_input_result"

    # 同一张卡片只能提交一次
    duplicate = client.post(
        f"{PREFIX}/tasks/{task_id}/messages/{message_id}/actions",
        json={"action_id": action["action_id"], "value": _SECRET},
    )
    assert duplicate.status_code == 409, duplicate.text
    assert _SECRET not in duplicate.text


def test_secure_input_action_requires_a_value(client: TestClient) -> None:
    task_id = _create_task(client)
    message_id = _seed_secure_card(_container(client), task_id)
    action = _find_input_action(client, task_id, message_id)
    response = client.post(
        f"{PREFIX}/tasks/{task_id}/messages/{message_id}/actions",
        json={"action_id": action["action_id"]},
    )
    assert response.status_code == 422, response.text


def test_action_is_isolated_between_users(settings: Settings, db_schema: None) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": None})
    with _client(cfg) as client:
        task_id = _create_task(client, headers=_auth("2"))
        message_id, decision_id = _seed_decision(_container(client), task_id)
        # 动作 id 是确定性的（服务端映射既有消息时铸造），不需要先以用户 2 读一次。
        action_id = f"decision:{decision_id}:0"

        # 另一个用户看不到这个任务，也就无法触发它的动作
        other = client.post(
            f"{PREFIX}/tasks/{task_id}/messages/{message_id}/actions",
            json={"action_id": action_id},
            headers=_auth("3"),
        )
        assert other.status_code == 404, other.text
        assert other.json()["code"] == "not_found"

        # 任务仍然 pending，真正的用户随后可以正常操作
        own = client.post(
            f"{PREFIX}/tasks/{task_id}/messages/{message_id}/actions",
            json={"action_id": action_id},
            headers=_auth("2"),
        )
        assert own.status_code == 200, own.text
