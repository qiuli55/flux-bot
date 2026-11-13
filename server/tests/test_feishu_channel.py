"""正式飞书频道契约测试（入站加密回调 + 审批保护出站）。

全部离线：加密用同一套 AES-256-CBC 算法在测试里现加密，模型调用替换成记录型
替身，发送接口也替换成记录型替身，不访问任何真实飞书接口。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flux.config import Settings
from flux.container import Container
from flux.core.channels.feishu_bot import (
    FEISHU_OUTBOUND_KIND,
    FeishuBotChannel,
    FeishuBotSender,
)
from flux.core.channels.feishu_crypto import (
    FeishuCryptoError,
    compute_signature,
    decrypt_payload,
    encrypt_payload,
    verify_signature,
)
from flux.core.task_engine.assistant import AssistantTurn
from flux.main import create_app

VERIFY_TOKEN = "verify-token-for-feishu-tests"
ENCRYPT_KEY = "encrypt-key-for-feishu-tests"
CHAT_ID = "oc_test_chat"
MESSAGE_ID = "om_test_message"
WEBHOOK = "/api/v1/channels/feishu"
PREFIX = "/api/v1"


# --- 替身 ---


class _FakeBots:
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.calls: list[uuid.UUID] = []

    async def get(self, bot_id: uuid.UUID) -> Any:
        self.calls.append(bot_id)
        return self.bot


class _FakeConversation:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.seen: set[str] = set()
        self.turns: list[Any] = []

    async def create_turn(
        self, bot: Any, *, content: str, client_turn_id: str | None = None
    ) -> tuple[Any, bool]:
        self.calls.append(
            {
                "bot_id": bot.id,
                "content": content,
                "client_turn_id": client_turn_id,
            }
        )
        if client_turn_id is not None and client_turn_id in self.seen:
            existing = self.turns[0]
            return existing, False
        if client_turn_id is not None:
            self.seen.add(client_turn_id)
        turn = SimpleNamespace(id=uuid.uuid4(), task_id=uuid.uuid4())
        self.turns.append(turn)
        return turn, True

    async def wait_for_turn(self, bot_id: uuid.UUID, turn_id: uuid.UUID, *, wait_seconds: float):
        return SimpleNamespace(id=turn_id, status="failed", reply_message_id=None)


class _FakeSender:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.messages: list[tuple[str, str]] = []
        self.error = error
        self.closed = False

    async def send_text(self, chat_id: str, text: str) -> None:
        self.messages.append((chat_id, text))
        if self.error is not None:
            raise self.error

    async def aclose(self) -> None:
        self.closed = True


class _FakeAssistant:
    async def respond(self, **kwargs: Any) -> AssistantTurn:
        on_delta = kwargs.get("on_delta")
        if on_delta is not None:
            await on_delta("思考中")
        return AssistantTurn(
            reply=f"echo: {kwargs['message']}",
            provider="fake",
            model="fake-model",
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )


# --- 工具 ---


def _plain_body(
    *,
    token: str | None = VERIFY_TOKEN,
    message_id: str = MESSAGE_ID,
    message_type: str = "text",
    text: str = "你好",
    event_type: str = "im.message.receive_v1",
    chat_id: str = CHAT_ID,
) -> dict[str, Any]:
    header: dict[str, Any] = {"event_id": "evt_test", "event_type": event_type}
    if token is not None:
        header["token"] = token
    return {
        "schema": "2.0",
        "header": header,
        "event": {
            "sender": {"sender_id": {"open_id": "ou_test"}},
            "message": {
                "message_id": message_id,
                "chat_id": chat_id,
                "message_type": message_type,
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }


def _signed_headers(raw: bytes, *, encrypt_key: str = ENCRYPT_KEY) -> dict[str, str]:
    timestamp = "1700000000"
    nonce = "nonce-for-tests"
    return {
        "X-Lark-Request-Timestamp": timestamp,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": compute_signature(encrypt_key, timestamp, nonce, raw),
    }


def _encrypted_envelope(
    payload: dict[str, Any], *, encrypt_key: str = ENCRYPT_KEY
) -> tuple[dict, bytes]:
    encrypted = encrypt_payload(encrypt_key, payload)
    body = json.dumps({"encrypt": encrypted}, separators=(",", ":")).encode("utf-8")
    return {"encrypt": encrypted}, body


def _unit_channel(*, bot_id: str | None = None, **kwargs: Any) -> FeishuBotChannel:
    bot = SimpleNamespace(id=uuid.uuid4(), name="飞书小助", status="active")
    container = SimpleNamespace(
        bots=_FakeBots(bot),
        bot_conversation=_FakeConversation(),
    )
    return FeishuBotChannel(
        container,  # type: ignore[arg-type]
        verification_token=VERIFY_TOKEN,
        bot_id=bot_id or str(bot.id),
        **kwargs,
    )


@contextmanager
def _app(settings: Settings, **updates: object) -> Iterator[tuple[TestClient, Container]]:
    configured = settings.model_copy(update=updates)
    app = create_app(configured)
    with TestClient(app) as client:
        yield client, app.state.container


def _wait_pending_decision(client: TestClient, task_id: str, timeout: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"{PREFIX}/tasks/{task_id}").json()["data"]
        pending = body.get("pending_decision")
        if pending is not None:
            return pending
        time.sleep(0.02)
    raise AssertionError("等待飞书出站审批超时")


@contextmanager
def _callback_client(settings: Settings) -> Iterator[tuple[TestClient, Container, dict]]:
    """起 app、建一个 Bot、把频道指向它，并替换掉真实模型与发送端。"""
    with _app(settings, feishu_verification_token=VERIFY_TOKEN) as (client, container):
        container.assistant = _FakeAssistant()  # type: ignore[assignment]
        created = client.post(f"{PREFIX}/bots", json={"name": "飞书小助", "description": "接飞书"})
        assert created.status_code == 200, created.text
        bot = created.json()["data"]
        container.feishu_bot._bot_id = uuid.UUID(bot["id"])  # noqa: SLF001 - 测试注入归属 Bot
        sender = _FakeSender()
        container.feishu_bot._sender = sender  # noqa: SLF001 - 测试替身
        yield client, container, {"bot": bot, "sender": sender}


# --- 加解密与签名 ---


def test_encrypt_decrypt_round_trip() -> None:
    payload = {"type": "url_verification", "challenge": "abc", "token": VERIFY_TOKEN}
    encrypted = encrypt_payload(ENCRYPT_KEY, payload)
    assert decrypt_payload(ENCRYPT_KEY, encrypted) == payload

    # 换错密钥 / 非法 base64 / 长度不对都必须失败
    with pytest.raises(FeishuCryptoError):
        decrypt_payload("wrong-key", encrypted)
    with pytest.raises(FeishuCryptoError):
        decrypt_payload(ENCRYPT_KEY, "not-base64")
    with pytest.raises(FeishuCryptoError):
        decrypt_payload(ENCRYPT_KEY, "AAAA")


def test_signature_matches_official_recipe() -> None:
    body = b'{"encrypt":"x"}'
    expected = hashlib.sha256(
        ("1700000000" + "nonce" + ENCRYPT_KEY).encode("utf-8") + body
    ).hexdigest()
    assert compute_signature(ENCRYPT_KEY, "1700000000", "nonce", body) == expected
    assert verify_signature(
        encrypt_key=ENCRYPT_KEY,
        timestamp="1700000000",
        nonce="nonce",
        body=body,
        signature=expected,
    )
    # 原始请求体变一个字节就验不过；缺任一头也 fail-closed
    assert not verify_signature(
        encrypt_key=ENCRYPT_KEY,
        timestamp="1700000000",
        nonce="nonce",
        body=body + b" ",
        signature=expected,
    )
    assert not verify_signature(
        encrypt_key=ENCRYPT_KEY,
        timestamp=None,
        nonce="nonce",
        body=body,
        signature=expected,
    )


# --- 回调：challenge 与失败路径 ---


async def test_plaintext_url_verification() -> None:
    channel = FeishuBotChannel(None, verification_token=VERIFY_TOKEN)  # type: ignore[arg-type]
    raw = json.dumps(
        {"type": "url_verification", "challenge": "abc123", "token": VERIFY_TOKEN}
    ).encode()
    result = await channel.handle(raw, {})
    assert result.status_code == 200
    assert result.payload == {"challenge": "abc123"}

    # 没配 token：任何回调都拒绝
    unconfigured = FeishuBotChannel(None)  # type: ignore[arg-type]
    assert (await unconfigured.handle(raw, {})).status_code == 400

    # token 不匹配
    wrong = json.dumps(
        {"type": "url_verification", "challenge": "abc123", "token": "wrong"}
    ).encode()
    assert (await channel.handle(wrong, {})).status_code == 403


async def test_encrypted_url_verification_and_signature_required() -> None:
    channel = FeishuBotChannel(
        None,  # type: ignore[arg-type]
        verification_token=VERIFY_TOKEN,
        encrypt_key=ENCRYPT_KEY,
    )
    payload = {"type": "url_verification", "challenge": "encrypted-ok", "token": VERIFY_TOKEN}
    _envelope, raw = _encrypted_envelope(payload)

    ok = await channel.handle(raw, _signed_headers(raw))
    assert ok.status_code == 200
    assert ok.payload == {"challenge": "encrypted-ok"}

    # 配了 Encrypt Key 就必须验签：缺头、错签名都拒绝
    assert (await channel.handle(raw, {})).status_code == 403
    bad = _signed_headers(raw)
    bad["X-Lark-Signature"] = "0" * 64
    assert (await channel.handle(raw, bad)).status_code == 403


async def test_ciphertext_without_encrypt_key_is_rejected() -> None:
    channel = FeishuBotChannel(None, verification_token=VERIFY_TOKEN)  # type: ignore[arg-type]
    result = await channel.handle(json.dumps({"encrypt": "ciphertext"}).encode(), {})
    assert result.status_code == 400
    assert "FLUX_FEISHU_ENCRYPT_KEY" in result.payload["error"]


async def test_encrypted_event_creates_turn_and_is_idempotent() -> None:
    channel = _unit_channel(encrypt_key=ENCRYPT_KEY)
    payload = _plain_body()
    _envelope, raw = _encrypted_envelope(payload)

    first = await channel.handle(raw, _signed_headers(raw))
    assert first.status_code == 200, first.payload
    assert first.payload["created"] is True

    replay = await channel.handle(raw, _signed_headers(raw))
    assert replay.status_code == 200, replay.payload
    assert replay.payload["created"] is False
    assert replay.payload["turn_id"] == first.payload["turn_id"]

    conversation = channel._container.bot_conversation  # type: ignore[attr-defined]  # noqa: SLF001
    assert len(conversation.calls) == 2
    assert conversation.calls[0]["content"] == "你好"
    assert conversation.calls[0]["client_turn_id"] == MESSAGE_ID
    await channel.aclose()


async def test_non_text_and_unknown_events_are_ignored() -> None:
    channel = _unit_channel()
    image = await channel.handle(json.dumps(_plain_body(message_type="image")).encode(), {})
    assert image.status_code == 200
    assert image.payload["ignored"] is True

    other = await channel.handle(
        json.dumps(_plain_body(event_type="im.chat.updated_v1")).encode(), {}
    )
    assert other.status_code == 200
    assert other.payload["ignored"] is True

    bad_token = await channel.handle(json.dumps(_plain_body(token="wrong")).encode(), {})
    assert bad_token.status_code == 403

    conversation = channel._container.bot_conversation  # type: ignore[attr-defined]  # noqa: SLF001
    assert conversation.calls == []
    await channel.aclose()


async def test_message_event_without_bot_id_is_fail_closed() -> None:
    channel = _unit_channel()
    channel._bot_id = None  # noqa: SLF001 - 模拟漏配
    result = await channel.handle(json.dumps(_plain_body()).encode(), {})
    assert result.status_code == 400
    assert "FLUX_FEISHU_BOT_ID" in result.payload["error"]


# --- 入站 → 审批草稿 → 允许/拒绝 ---


def test_inbound_creates_turn_but_never_sends_automatically(
    settings: Settings, db_schema: None
) -> None:
    with _callback_client(settings) as (client, container, context):
        response = client.post(WEBHOOK, json=_plain_body())
        assert response.status_code == 200, response.text
        assert response.json()["created"] is True

        bot = client.get(f"{PREFIX}/bots/{context['bot']['id']}").json()["data"]
        pending = _wait_pending_decision(client, bot["chat_task_id"])
        assert pending["kind"] == FEISHU_OUTBOUND_KIND
        assert pending["meta"]["chat_id"] == CHAT_ID
        assert pending["meta"]["message_id"] == MESSAGE_ID
        assert pending["meta"]["bot_id"] == context["bot"]["id"]
        assert pending["meta"]["text_hash"]
        assert [option["label"] for option in pending["options"]] == ["允许", "拒绝"]

        # 回复只以草稿形式存在：审批完成前绝不调用发送接口
        assert context["sender"].messages == []
        drafts = client.get(
            f"{PREFIX}/outbound-drafts",
            params={"channel": "feishu", "status": "pending"},
        ).json()["data"]
        assert len(drafts) == 1
        assert drafts[0]["id"] == pending["meta"]["draft_id"]
        assert drafts[0]["body"] == "echo: 你好"
        assert drafts[0]["dest"] == {"chat_id": CHAT_ID, "message_id": MESSAGE_ID}
        assert drafts[0]["decision_id"] == pending["id"]
        # 草稿详情来自数据库，而不是进程内表
        assert container.outbound_drafts is not None


def test_allow_sends_once_and_deny_does_not_send(settings: Settings, db_schema: None) -> None:
    with _callback_client(settings) as (client, _container, context):
        assert client.post(WEBHOOK, json=_plain_body()).status_code == 200
        bot = client.get(f"{PREFIX}/bots/{context['bot']['id']}").json()["data"]
        pending = _wait_pending_decision(client, bot["chat_task_id"])
        draft_id = pending["meta"]["draft_id"]

        denied = client.post(
            f"{PREFIX}/tasks/{bot['chat_task_id']}/decisions/choose",
            json={"decision_id": pending["id"], "action": "choose", "option": "拒绝"},
        )
        assert denied.status_code == 200, denied.text
        assert context["sender"].messages == []
        # 拒绝只改状态，不发送；正文仍可从草稿接口读到
        draft = client.get(f"{PREFIX}/outbound-drafts/{draft_id}").json()["data"]
        assert draft["status"] == "denied"
        assert draft["body"] == "echo: 你好"
        assert draft["sent_at"] is None

    # 第二条消息走「允许」：只发送一次
    with _callback_client(settings) as (client, _container, context):
        assert client.post(WEBHOOK, json=_plain_body(message_id="om_allow")).status_code == 200
        bot = client.get(f"{PREFIX}/bots/{context['bot']['id']}").json()["data"]
        pending = _wait_pending_decision(client, bot["chat_task_id"])
        draft_id = pending["meta"]["draft_id"]

        allowed = client.post(
            f"{PREFIX}/tasks/{bot['chat_task_id']}/decisions/choose",
            json={"decision_id": pending["id"], "action": "choose", "option": "允许"},
        )
        assert allowed.status_code == 200, allowed.text
        assert context["sender"].messages == [(CHAT_ID, "echo: 你好")]
        sent = client.get(f"{PREFIX}/outbound-drafts/{draft_id}").json()["data"]
        assert sent["status"] == "sent"
        assert sent["sent_at"] is not None
        assert sent["decision_id"] == pending["id"]

        # 同一条决策不能再次放行；草稿已是终态，绝不会重复发送
        again = client.post(
            f"{PREFIX}/tasks/{bot['chat_task_id']}/decisions/choose",
            json={"decision_id": pending["id"], "action": "choose", "option": "允许"},
        )
        assert again.status_code == 409
        assert context["sender"].messages == [(CHAT_ID, "echo: 你好")]


def test_feishu_pending_draft_survives_process_restart(settings: Settings, db_schema: None) -> None:
    """草稿落库而不是留在进程内：换一个容器仍能读到同一条 pending 草稿。"""
    with _callback_client(settings) as (client, _container, context):
        assert client.post(WEBHOOK, json=_plain_body()).status_code == 200
        bot = client.get(f"{PREFIX}/bots/{context['bot']['id']}").json()["data"]
        pending = _wait_pending_decision(client, bot["chat_task_id"])
        draft_id = pending["meta"]["draft_id"]
        assert context["sender"].messages == []

    # 同一 SQLite 文件上重新起一套 app / 容器
    with _app(settings, feishu_verification_token=VERIFY_TOKEN) as (client, _container):
        draft = client.get(f"{PREFIX}/outbound-drafts/{draft_id}").json()["data"]
        assert draft["status"] == "pending"
        assert draft["body"] == "echo: 你好"
        assert draft["bot_id"] == context["bot"]["id"]


def test_send_error_is_logged_and_does_not_break_decision(
    settings: Settings, db_schema: None
) -> None:
    with _callback_client(settings) as (client, container, context):
        sender = _FakeSender(error=RuntimeError("飞书炸了"))
        container.feishu_bot._sender = sender  # noqa: SLF001 - 测试替身
        assert client.post(WEBHOOK, json=_plain_body()).status_code == 200
        bot = client.get(f"{PREFIX}/bots/{context['bot']['id']}").json()["data"]
        pending = _wait_pending_decision(client, bot["chat_task_id"])
        draft_id = pending["meta"]["draft_id"]

        response = client.post(
            f"{PREFIX}/tasks/{bot['chat_task_id']}/decisions/choose",
            json={"decision_id": pending["id"], "action": "choose", "option": "允许"},
        )
        # 发送失败不能让决策接口 500
        assert response.status_code == 200, response.text
        assert len(sender.messages) == 1
        # 失败标 failed 但保留正文，且错误串不含正文
        failed = client.get(f"{PREFIX}/outbound-drafts/{draft_id}").json()["data"]
        assert failed["status"] == "failed"
        assert failed["body"] == "echo: 你好"
        assert failed["error"]
        assert "echo: 你好" not in failed["error"]


def test_plaintext_callback_replay_is_idempotent(settings: Settings, db_schema: None) -> None:
    with _callback_client(settings) as (client, _container, context):
        first = client.post(WEBHOOK, json=_plain_body())
        second = client.post(WEBHOOK, json=_plain_body())
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert second.json()["created"] is False
        assert second.json()["turn_id"] == first.json()["turn_id"]

        turns = client.get(f"{PREFIX}/bots/{context['bot']['id']}/turns").json()["data"]
        assert len(turns) == 1


def test_feishu_bot_sender_uses_official_send_api() -> None:
    """不触网校验发送端拼装：token 交换 + POST /open-apis/im/v1/messages。"""

    class _Response:
        status_code = 200

        def __init__(self, data: dict[str, Any]) -> None:
            self._data = data

        def json(self) -> dict[str, Any]:
            return self._data

    class _Client:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def request(self, method: str, url: str, **kwargs: Any) -> _Response:
            self.calls.append({"method": method, "url": url, **kwargs})
            if url.endswith("/tenant_access_token/internal"):
                return _Response({"code": 0, "tenant_access_token": "t-token", "expire": 7200})
            return _Response({"code": 0, "data": {"message_id": "om_sent"}})

        async def aclose(self) -> None:
            return None

    async def scenario() -> None:
        client = _Client()
        sender = FeishuBotSender(
            app_id="app",
            app_secret="secret",
            api_base="https://open.feishu.cn",
            client=client,  # type: ignore[arg-type]
        )
        await sender.send_text(CHAT_ID, "确认后的文本")
        assert [call["url"] for call in client.calls] == [
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            "https://open.feishu.cn/open-apis/im/v1/messages",
        ]
        send = client.calls[-1]
        assert send["params"] == {"receive_id_type": "chat_id"}
        assert send["headers"] == {"Authorization": "Bearer t-token"}
        assert send["json"]["receive_id"] == CHAT_ID
        assert send["json"]["msg_type"] == "text"
        assert json.loads(send["json"]["content"]) == {"text": "确认后的文本"}

    asyncio.run(scenario())
