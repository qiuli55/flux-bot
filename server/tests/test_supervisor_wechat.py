"""Supervisor P2：微信接入（OpenClaw / 微信 ClawBot 只做传输层）。

验收标准：

1. 入站 fail-closed：未配令牌 / 令牌错误 / 白名单为空 / 发送者不在白名单一律拒绝，
   非法 JSON 与缺字段返回 400；
2. 一条微信消息落成一条 assistant 任务 + user/assistant 两条消息，自动进入 RAG 索引；
3. 同一条 message_id 重试只调用一次模型，第二次回放上次回复并标记 duplicate；
4. 模型失败返回 503 交回 OpenClaw 原生模型，归还幂等游标后同一条消息可重试。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

from flux.config import Settings
from flux.container import Container
from flux.enums import TaskMessageRole, TaskOrigin
from flux.main import create_app

PATH = "/api/v1/supervisor/channels/wechat"
TOKEN = "wechat-bridge-token-for-tests"
SENDER = "wxid_owner"
ALLOW = f"{SENDER},wxid_other"


def _body(**overrides: object) -> dict:
    payload = {
        "message_id": "msg_1",
        "session_key": "session_1",
        "sender_id": SENDER,
        "text": "现在做到哪了？",
    }
    payload.update(overrides)
    return payload


def _post(client: TestClient, body: dict | None = None, *, token: str | None = TOKEN):
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return client.post(PATH, json=body if body is not None else _body(), headers=headers)


@contextmanager
def _app(settings: Settings, **updates: object) -> Iterator[tuple[TestClient, Container]]:
    configured = settings.model_copy(update=updates)
    app = create_app(configured)
    with TestClient(app) as client:
        yield client, app.state.container


def _configured(settings: Settings) -> Iterator[tuple[TestClient, Container]]:
    return _app(
        settings,
        supervisor_wechat_bridge_token=TOKEN,
        supervisor_wechat_allow_from=ALLOW,
    )


# --- 入站 fail-closed ---


def test_wechat_callback_is_fail_closed(settings: Settings, db_schema: None) -> None:
    # 没配令牌：拒绝一切回调
    with _app(settings) as (client, _):
        response = client.post(PATH, json=_body())
        assert response.status_code == 400
        assert response.json()["handled"] is False

    with _configured(settings) as (client, _):
        # 令牌不匹配 / 完全不带令牌
        assert _post(client, token="wrong").status_code == 403
        assert _post(client, token=None).status_code == 403
        # 白名单不为空，但发送者不在其中
        assert _post(client, _body(sender_id="wxid_stranger")).status_code == 403
        # 非法 JSON / 缺字段
        assert (
            client.post(
                PATH, content=b"not-json", headers={"Authorization": f"Bearer {TOKEN}"}
            ).status_code
            == 400
        )
        assert _post(client, {"message_id": "m"}).status_code == 400

    # 令牌对了但白名单没配：宁可拒绝，也不能默认放行所有人
    with _app(settings, supervisor_wechat_bridge_token=TOKEN) as (client, _):
        assert _post(client).status_code == 403


def test_wechat_allow_from_parses_separators(settings: Settings) -> None:
    configured = settings.model_copy(update={"supervisor_wechat_allow_from": " a, b;c\n d , "})
    assert configured.wechat_allow_from == {"a", "b", "c", "d"}


# --- 正常链路 ---


async def test_wechat_message_creates_assistant_task(settings: Settings, db_schema: None) -> None:
    with _configured(settings) as (client, container):
        response = _post(client)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["handled"] is True
        reply_text = payload["reply"]["text"]
        assert "[local-echo]" in reply_text
        assert "现在做到哪了？" in reply_text

        task_id = uuid.UUID(payload["task_id"])
        task = await container.task_repo.get(task_id)
        assert task.origin == TaskOrigin.ASSISTANT.value

        messages, has_more = await container.task_repo.list_messages(task_id, limit=10)
        assert has_more is False
        assert [message.role for message in messages] == [
            TaskMessageRole.USER.value,
            TaskMessageRole.ASSISTANT.value,
        ]
        assert messages[0].content == "现在做到哪了？"
        assert messages[1].content == reply_text

        session = await container.wechat_sessions.get("wechat", "session_1")
        assert session is not None
        assert session.task_id == task_id
        assert session.sender_id == SENDER
        assert session.message_count == 2
        assert session.last_message_id == "msg_1"
        assert str(session.id) == payload["session_id"]


async def test_wechat_thread_reuses_one_task(settings: Settings, db_schema: None) -> None:
    with _configured(settings) as (client, container):
        first = _post(client).json()
        second = _post(client, _body(message_id="msg_2", text="那下一步呢？")).json()

        assert second["task_id"] == first["task_id"]
        task_id = uuid.UUID(first["task_id"])
        messages, _ = await container.task_repo.list_messages(task_id, limit=10)
        assert len(messages) == 4
        assert [message.content for message in messages[2:]] == [
            "那下一步呢？",
            second["reply"]["text"],
        ]

        session = await container.wechat_sessions.get("wechat", "session_1")
        assert session is not None
        assert session.message_count == 4
        assert session.last_message_id == "msg_2"


async def test_wechat_message_is_processed_once(settings: Settings, db_schema: None) -> None:
    with _configured(settings) as (client, container):
        first = _post(client).json()
        replay = _post(client).json()

        assert replay["handled"] is True
        assert replay["duplicate"] is True
        assert replay["task_id"] == first["task_id"]
        assert replay["reply"]["text"] == first["reply"]["text"]

        task_id = uuid.UUID(first["task_id"])
        messages, _ = await container.task_repo.list_messages(task_id, limit=10)
        assert len(messages) == 2

        session = await container.wechat_sessions.get("wechat", "session_1")
        assert session is not None
        assert session.message_count == 2


# --- 失败可重试 ---


class _FailingRouter:
    async def chat(self, messages):  # noqa: ANN001, ANN201 - 只用于模拟模型故障
        raise RuntimeError("模型不可用")


async def test_wechat_failure_returns_503_and_allows_retry(
    settings: Settings, db_schema: None
) -> None:
    with _configured(settings) as (client, container):
        assistant = container.supervisor_wechat.assistant
        router = assistant.router
        assistant.router = _FailingRouter()
        try:
            failed = _post(client)
        finally:
            assistant.router = router

        assert failed.status_code == 503
        assert failed.json()["handled"] is False

        session = await container.wechat_sessions.get("wechat", "session_1")
        assert session is not None
        assert session.last_message_id is None
        assert session.message_count == 0

        retried = _post(client)
        assert retried.status_code == 200, retried.text
        payload = retried.json()
        assert payload["handled"] is True
        assert "duplicate" not in payload

        task_id = uuid.UUID(payload["task_id"])
        messages, _ = await container.task_repo.list_messages(task_id, limit=10)
        assert len(messages) == 2
