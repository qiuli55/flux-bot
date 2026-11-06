"""安全输入卡片（隐私密钥）测试。

口径：
- 密钥明文只在提交请求里出现一次，落库即加密；接口与消息表都只留掩码与回执；
- 每张卡片最多提交一次（重复提交 409），卡片必须属于当前任务；
- 模型给的卡片描述不可用（供应商非法、没有模型）时不出卡片，避免用户白填。
"""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi.testclient import TestClient

from flux.container import Container
from flux.core.task_engine.assistant import parse_secure_input
from flux.core.task_engine.repository import MessageDraft

PREFIX = "/api/v1"

_SECRET = "sk-live-abcdef1234"

_CARD = {
    "title": "Grok API key",
    "description": "在 grok.com/dashboard 选 Grok API 类型创建的 key",
    "placeholder": "Grok API key",
    "name": "Grok API key",
    "provider": "openai",
    "models": ["grok-4"],
    "base_url": "",
    "wire_api": "",
}


def _create_task(client: TestClient, description: str = "接入 Grok") -> str:
    response = client.post(f"{PREFIX}/tasks", json={"description": description})
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["id"])


def _seed_card(client: TestClient, task_id: str, *, kind: str = "secure_input") -> str:
    """直接在库里插一条助手消息当作安全输入卡片（助手回复本身由模型产生，不伪造接口）。"""
    container: Container = client.app.state.container

    async def _run() -> str:
        created = await container.task_repo.add_messages(
            uuid.UUID(task_id),
            [
                MessageDraft(
                    role="assistant",
                    kind=kind,
                    content="key 只会显示一次，填到下面的安全输入框里。",
                    payload={"secure_input": dict(_CARD)},
                )
            ],
        )
        return str(created[0].id)

    return asyncio.run(_run())


def _messages(client: TestClient, task_id: str) -> list[dict]:
    response = client.get(f"{PREFIX}/tasks/{task_id}/messages", params={"limit": 100})
    assert response.status_code == 200, response.text
    return response.json()["data"]


# --- 协议解析 ---


def test_parse_secure_input_accepts_usable_card() -> None:
    raw = json.dumps({"reply": "请填写密钥", "secure_input": _CARD}, ensure_ascii=False)
    card = parse_secure_input(raw)
    assert card is not None
    assert card["name"] == "Grok API key"
    assert card["provider"] == "openai"
    assert card["models"] == ["grok-4"]
    assert card["placeholder"] == "Grok API key"


def test_parse_secure_input_rejects_unusable_cards() -> None:
    def card(**overrides: object) -> str:
        payload = {"reply": "请填写密钥", "secure_input": {**_CARD, **overrides}}
        return json.dumps(payload, ensure_ascii=False)

    assert parse_secure_input("纯文本，没有卡片") is None
    assert parse_secure_input(json.dumps({"secure_input": None})) is None
    assert parse_secure_input(card(provider="unknown")) is None
    assert parse_secure_input(card(models=[])) is None
    assert parse_secure_input(card(title="")) is None
    # openai_compatible 缺 base_url 时卡片落不了库，宁可不展示
    assert parse_secure_input(card(provider="openai_compatible", base_url="")) is None
    assert parse_secure_input(card(provider="openai_compatible", base_url="https://x.dev/v1"))


# --- 提交接口 ---


def test_submit_secure_input_stores_key_and_never_echoes_plaintext(client: TestClient) -> None:
    task_id = _create_task(client)
    card_id = _seed_card(client, task_id)

    response = client.post(
        f"{PREFIX}/tasks/{task_id}/secure-input",
        json={"message_id": card_id, "value": _SECRET},
    )
    assert response.status_code == 200, response.text
    assert _SECRET not in response.text

    data = response.json()["data"]
    assert data["key"]["name"] == "Grok API key"
    assert data["key"]["provider"] == "openai"
    assert data["key"]["key_hint"] == "••••1234"
    assert "key_ciphertext" not in data["key"]
    assert data["message"]["kind"] == "secure_input_result"
    assert data["message"]["payload"]["message_id"] == card_id
    assert data["message"]["payload"]["status"] == "stored"
    assert _SECRET not in data["message"]["content"]

    # 消息表里只有回执，没有明文
    messages = _messages(client, task_id)
    assert _SECRET not in json.dumps(messages, ensure_ascii=False)
    assert messages[-1]["kind"] == "secure_input_result"

    # 密钥库能查到这条 key，且只给掩码
    keys = client.get(f"{PREFIX}/keys").json()["data"]
    stored = next(item for item in keys if item["name"] == "Grok API key")
    assert stored["key_hint"] == "••••1234"
    assert _SECRET not in json.dumps(stored, ensure_ascii=False)


def test_submit_secure_input_is_idempotent_per_card(client: TestClient) -> None:
    task_id = _create_task(client)
    card_id = _seed_card(client, task_id)
    body = {"message_id": card_id, "value": _SECRET}

    assert client.post(f"{PREFIX}/tasks/{task_id}/secure-input", json=body).status_code == 200
    duplicate = client.post(f"{PREFIX}/tasks/{task_id}/secure-input", json=body)
    assert duplicate.status_code == 409
    assert _SECRET not in duplicate.text


def test_submit_secure_input_requires_card_in_same_task(client: TestClient) -> None:
    owner_task = _create_task(client, "任务甲")
    other_task = _create_task(client, "任务乙")
    card_id = _seed_card(client, owner_task)

    response = client.post(
        f"{PREFIX}/tasks/{other_task}/secure-input",
        json={"message_id": card_id, "value": _SECRET},
    )
    assert response.status_code == 404
    assert _SECRET not in response.text

    # 不是安全输入卡片的普通消息不能被当成卡片提交
    text_id = _seed_card(client, owner_task, kind="text")
    mismatch = client.post(
        f"{PREFIX}/tasks/{owner_task}/secure-input",
        json={"message_id": text_id, "value": _SECRET},
    )
    assert mismatch.status_code == 404
    assert client.get(f"{PREFIX}/keys").json()["data"] == []
