"""正式钉钉频道契约测试（HTTP 事件订阅入站 + 审批保护出站）。

全部离线：加解密用同一套官方算法在测试里现算，发送接口与模型调用替换成记录型
替身，不访问任何真实钉钉接口。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flux.bot_node.main import create_app as create_bot_app
from flux.config import Settings
from flux.container import Container
from flux.core.channels.dingtalk_bot import (
    DINGTALK_OUTBOUND_KIND,
    DingTalkBotChannel,
    DingTalkBotSender,
)
from flux.core.channels.dingtalk_crypto import (
    DingTalkCryptoError,
    build_encrypted_response,
    compute_signature,
    decrypt_message,
    encrypt_message,
    verify_signature,
)
from flux.core.task_engine.assistant import AssistantTurn
from flux.main import create_app

TOKEN = "dingtalk-token-for-tests"
#: 43 位 EncodingAESKey（合法 base64 字母表，解码后 32 字节）
AES_KEY = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFG"
APP_KEY = "dingtestappkey0001"
APP_SECRET = "dingtalk-app-secret-for-tests"
CONVERSATION_ID = "cid_test_conversation=="
STAFF_ID = "014728255240768602"
MSG_ID = "msgr_test_message=="
ROBOT_CODE = "dingrobotcode0001"
WEBHOOK = "/api/v1/channels/dingtalk"
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
        self.calls.append({"bot_id": bot.id, "content": content, "client_turn_id": client_turn_id})
        if client_turn_id is not None and client_turn_id in self.seen:
            return self.turns[0], False
        if client_turn_id is not None:
            self.seen.add(client_turn_id)
        turn = SimpleNamespace(id=uuid.uuid4(), task_id=uuid.uuid4())
        self.turns.append(turn)
        return turn, True

    async def wait_for_turn(self, bot_id: uuid.UUID, turn_id: uuid.UUID, *, wait_seconds: float):
        return SimpleNamespace(id=turn_id, status="failed", reply_message_id=None)


class _FakeSender:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.messages: list[tuple[dict[str, Any], str]] = []
        self.error = error
        self.closed = False

    async def send_text(self, dest: dict[str, Any], text: str) -> None:
        self.messages.append((dict(dest), text))
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


def _message_payload(
    *,
    msg_id: str = MSG_ID,
    text: str = "你好",
    msgtype: str = "text",
    event_type: str = "chatbot_message",
    conversation_id: str = CONVERSATION_ID,
    staff_id: str = STAFF_ID,
    robot_code: str = ROBOT_CODE,
    conversation_type: str = "2",
) -> dict[str, Any]:
    return {
        "EventType": event_type,
        "EventId": f"evt_{msg_id}",
        "CorpId": "dingtestcorp",
        "msgId": msg_id,
        "msgtype": msgtype,
        "text": {"content": text},
        "conversationId": conversation_id,
        "conversationType": conversation_type,
        "senderStaffId": staff_id,
        "senderId": "$:LWCP_v1:$sender",
        "robotCode": robot_code,
        "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=test",
    }


def _encrypted_request(
    payload: dict[str, Any],
    *,
    token: str = TOKEN,
    aes_key: str = AES_KEY,
    owner_key: str = APP_KEY,
) -> tuple[dict[str, str], bytes]:
    """按官方请求形状构造查询串签名 + 加密体。"""
    encrypt = encrypt_message(aes_key, owner_key, json.dumps(payload, ensure_ascii=False))
    raw = json.dumps({"encrypt": encrypt}, separators=(",", ":")).encode("utf-8")
    timestamp = "1700000000"
    nonce = "nonce-for-tests"
    query = {
        # 请求侧官方文档写的是 signature / timestamp，兼容别名走这里。
        "signature": compute_signature(token, timestamp, nonce, encrypt),
        "timestamp": timestamp,
        "nonce": nonce,
    }
    return query, raw


def _assert_encrypted_success(response: Any) -> str:
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"msg_signature", "timeStamp", "nonce", "encrypt"}
    message = decrypt_message(
        token=TOKEN,
        aes_key=AES_KEY,
        owner_key=APP_KEY,
        signature=body["msg_signature"],
        timestamp=body["timeStamp"],
        nonce=body["nonce"],
        encrypt=body["encrypt"],
    )
    assert message == "success"
    return message


def _unit_channel(*, bot_id: str | None = None, **kwargs: Any) -> DingTalkBotChannel:
    bot = SimpleNamespace(id=uuid.uuid4(), name="钉钉小助", status="active")
    container = SimpleNamespace(
        bots=_FakeBots(bot),
        bot_conversation=_FakeConversation(),
    )
    return DingTalkBotChannel(
        container,  # type: ignore[arg-type]
        app_key=APP_KEY,
        token=TOKEN,
        aes_key=AES_KEY,
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
    raise AssertionError("等待钉钉出站审批超时")


def _dingtalk_settings(settings: Settings, **updates: object) -> Settings:
    return settings.model_copy(
        update={
            "dingtalk_token": TOKEN,
            "dingtalk_aes_key": AES_KEY,
            "dingtalk_app_key": APP_KEY,
            "dingtalk_app_secret": APP_SECRET,
            **updates,
        }
    )


@contextmanager
def _callback_client(settings: Settings) -> Iterator[tuple[TestClient, Container, dict]]:
    """起 app、建一个 Bot、把频道指向它，并替换掉真实模型与发送端。"""
    with _app(_dingtalk_settings(settings)) as (client, container):
        container.assistant = _FakeAssistant()  # type: ignore[assignment]
        created = client.post(f"{PREFIX}/bots", json={"name": "钉钉小助", "description": "接钉钉"})
        assert created.status_code == 200, created.text
        bot = created.json()["data"]
        container.dingtalk_bot._bot_id = uuid.UUID(bot["id"])  # noqa: SLF001 - 测试注入归属 Bot
        sender = _FakeSender()
        container.dingtalk_bot._sender = sender  # noqa: SLF001 - 测试替身
        yield client, container, {"bot": bot, "sender": sender}


# --- 加解密与签名 ---


def test_encrypt_decrypt_round_trip() -> None:
    encrypt = encrypt_message(AES_KEY, APP_KEY, "success")
    assert (
        decrypt_message(
            token=TOKEN,
            aes_key=AES_KEY,
            owner_key=APP_KEY,
            signature=compute_signature(TOKEN, "1700000000", "nonce", encrypt),
            timestamp="1700000000",
            nonce="nonce",
            encrypt=encrypt,
        )
        == "success"
    )

    # 错密钥 / 非法 base64 / 长度不对都必须失败
    with pytest.raises(DingTalkCryptoError):
        decrypt_message(
            token=TOKEN,
            aes_key="1" * 43,
            owner_key=APP_KEY,
            signature=compute_signature(TOKEN, "1700000000", "nonce", encrypt),
            timestamp="1700000000",
            nonce="nonce",
            encrypt=encrypt,
        )
    with pytest.raises(DingTalkCryptoError):
        decrypt_message(
            token=TOKEN,
            aes_key=AES_KEY,
            owner_key=APP_KEY,
            signature=compute_signature(TOKEN, "1700000000", "nonce", "not-base64"),
            timestamp="1700000000",
            nonce="nonce",
            encrypt="not-base64",
        )
    with pytest.raises(DingTalkCryptoError):
        decrypt_message(
            token=TOKEN,
            aes_key=AES_KEY,
            owner_key=APP_KEY,
            signature=compute_signature(TOKEN, "1700000000", "nonce", "AAAA"),
            timestamp="1700000000",
            nonce="nonce",
            encrypt="AAAA",
        )
    # ownerKey 填错（应用级事件必须是 AppKey）
    with pytest.raises(DingTalkCryptoError):
        decrypt_message(
            token=TOKEN,
            aes_key=AES_KEY,
            owner_key="wrong-owner-key",
            signature=compute_signature(TOKEN, "1700000000", "nonce", encrypt),
            timestamp="1700000000",
            nonce="nonce",
            encrypt=encrypt,
        )


def test_signature_matches_official_recipe() -> None:
    encrypt = "ciphertext"
    expected = hashlib.sha1(
        "".join(sorted([TOKEN, "1700000000", "nonce", encrypt])).encode("utf-8")
    ).hexdigest()
    assert compute_signature(TOKEN, "1700000000", "nonce", encrypt) == expected
    assert verify_signature(
        token=TOKEN,
        timestamp="1700000000",
        nonce="nonce",
        encrypt=encrypt,
        signature=expected,
    )
    # 任一字段变化或缺失都必须 fail-closed
    assert not verify_signature(
        token=TOKEN,
        timestamp="1700000001",
        nonce="nonce",
        encrypt=encrypt,
        signature=expected,
    )
    assert not verify_signature(
        token=TOKEN,
        timestamp=None,
        nonce="nonce",
        encrypt=encrypt,
        signature=expected,
    )
    assert not verify_signature(
        token=TOKEN,
        timestamp="1700000000",
        nonce="nonce",
        encrypt=encrypt,
        signature=None,
    )


def test_encrypted_response_uses_official_field_names_and_layout() -> None:
    success = build_encrypted_response(
        token=TOKEN, aes_key=AES_KEY, owner_key=APP_KEY, timestamp="1000", nonce="n"
    )
    assert set(success) == {"msg_signature", "timeStamp", "nonce", "encrypt"}
    assert success["timeStamp"] == "1000"
    assert success["nonce"] == "n"
    assert success["msg_signature"] == compute_signature(TOKEN, "1000", "n", success["encrypt"])
    assert (
        decrypt_message(
            token=TOKEN,
            aes_key=AES_KEY,
            owner_key=APP_KEY,
            signature=success["msg_signature"],
            timestamp=success["timeStamp"],
            nonce=success["nonce"],
            encrypt=success["encrypt"],
        )
        == "success"
    )


# --- 回调：check_url 与失败路径 ---


async def test_check_url_returns_encrypted_success() -> None:
    channel = DingTalkBotChannel(
        None,  # type: ignore[arg-type]
        app_key=APP_KEY,
        token=TOKEN,
        aes_key=AES_KEY,
    )
    payload = {"EventType": "check_url", "EventId": "evt_check"}
    query, raw = _encrypted_request(payload)
    result = await channel.handle(raw, query, {})
    assert result.status_code == 200
    body = result.payload
    assert set(body) == {"msg_signature", "timeStamp", "nonce", "encrypt"}
    assert (
        decrypt_message(
            token=TOKEN,
            aes_key=AES_KEY,
            owner_key=APP_KEY,
            signature=body["msg_signature"],
            timestamp=body["timeStamp"],
            nonce=body["nonce"],
            encrypt=body["encrypt"],
        )
        == "success"
    )

    # 官方 Java 示例按 msg_signature / timeStamp 读，两种别名都要兼容。
    aliases = {
        "msg_signature": query["signature"],
        "timeStamp": query["timestamp"],
        "nonce": query["nonce"],
    }
    alias_result = await channel.handle(raw, aliases, {})
    assert alias_result.status_code == 200


async def test_bad_signature_and_missing_config_are_rejected() -> None:
    channel = DingTalkBotChannel(
        None,  # type: ignore[arg-type]
        app_key=APP_KEY,
        token=TOKEN,
        aes_key=AES_KEY,
    )
    query, raw = _encrypted_request({"EventType": "check_url"})

    missing = await channel.handle(raw, {}, {})
    assert missing.status_code == 403

    bad = dict(query)
    bad["signature"] = "0" * 40
    assert (await channel.handle(raw, bad, {})).status_code == 403

    # 未配置 token / aes key / app key 时一律 fail-closed
    no_token = DingTalkBotChannel(
        None,
        app_key=APP_KEY,
        aes_key=AES_KEY,  # type: ignore[arg-type]
    )
    result = await no_token.handle(raw, query, {})
    assert result.status_code == 400
    assert "FLUX_DINGTALK_TOKEN" in result.payload["error"]

    no_aes = DingTalkBotChannel(
        None,
        app_key=APP_KEY,
        token=TOKEN,  # type: ignore[arg-type]
    )
    result = await no_aes.handle(raw, query, {})
    assert result.status_code == 400
    assert "FLUX_DINGTALK_AES_KEY" in result.payload["error"]

    no_app = DingTalkBotChannel(
        None,
        token=TOKEN,
        aes_key=AES_KEY,  # type: ignore[arg-type]
    )
    result = await no_app.handle(raw, query, {})
    assert result.status_code == 400
    assert "FLUX_DINGTALK_APP_KEY" in result.payload["error"]

    # 加密回调缺少 encrypt 字段
    result = await channel.handle(b"{}", query, {})
    assert result.status_code == 400


async def test_wrong_owner_key_is_rejected() -> None:
    channel = DingTalkBotChannel(
        None,  # type: ignore[arg-type]
        app_key=APP_KEY,
        token=TOKEN,
        aes_key=AES_KEY,
    )
    query, raw = _encrypted_request({"EventType": "check_url"}, owner_key="other-app-key")
    result = await channel.handle(raw, query, {})
    assert result.status_code == 400
    assert "ownerKey" in result.payload["error"]


async def test_encrypted_event_creates_turn_and_is_idempotent() -> None:
    channel = _unit_channel()
    query, raw = _encrypted_request(_message_payload())

    first = await channel.handle(raw, query, {})
    assert first.status_code == 200
    assert set(first.payload) == {"msg_signature", "timeStamp", "nonce", "encrypt"}

    replay = await channel.handle(raw, query, {})
    assert replay.status_code == 200
    assert set(replay.payload) == {"msg_signature", "timeStamp", "nonce", "encrypt"}

    conversation = channel._container.bot_conversation  # type: ignore[attr-defined]  # noqa: SLF001
    assert len(conversation.calls) == 2
    assert conversation.calls[0]["content"] == "你好"
    assert conversation.calls[0]["client_turn_id"] == MSG_ID
    await channel.aclose()


async def test_v2_event_envelope_is_supported() -> None:
    """新版事件把元信息放在 headers、消息体放在 data，同样要能建 turn。"""
    channel = _unit_channel()
    payload = {
        "specVersion": "1.0",
        "type": "event",
        "headers": {
            "eventType": "/v1.0/im/bot/messages/get",
            "eventId": "evt_v2_message",
        },
        "data": {
            "msgId": "msgr_v2_message==",
            "msgtype": "text",
            "text": {"content": "新版事件"},
            "conversationId": CONVERSATION_ID,
            "senderStaffId": STAFF_ID,
            "robotCode": ROBOT_CODE,
        },
    }
    query, raw = _encrypted_request(payload)
    result = await channel.handle(raw, query, {})
    assert result.status_code == 200
    conversation = channel._container.bot_conversation  # type: ignore[attr-defined]  # noqa: SLF001
    assert conversation.calls[0]["content"] == "新版事件"
    assert conversation.calls[0]["client_turn_id"] == "msgr_v2_message=="
    await channel.aclose()


async def test_non_text_and_unknown_events_are_ignored_but_acknowledged() -> None:
    channel = _unit_channel()
    query, raw = _encrypted_request(_message_payload(msgtype="picture"))
    result = await channel.handle(raw, query, {})
    assert result.status_code == 200
    assert set(result.payload) == {"msg_signature", "timeStamp", "nonce", "encrypt"}

    query, raw = _encrypted_request(_message_payload(event_type="user_add_org"))
    result = await channel.handle(raw, query, {})
    assert result.status_code == 200
    assert set(result.payload) == {"msg_signature", "timeStamp", "nonce", "encrypt"}

    # 无论忽略与否都不能建 turn
    conversation = channel._container.bot_conversation  # type: ignore[attr-defined]  # noqa: SLF001
    assert conversation.calls == []
    await channel.aclose()


async def test_message_event_without_bot_id_is_fail_closed() -> None:
    channel = _unit_channel()
    channel._bot_id = None  # noqa: SLF001 - 模拟漏配
    query, raw = _encrypted_request(_message_payload())
    result = await channel.handle(raw, query, {})
    assert result.status_code == 400
    assert "FLUX_DINGTALK_BOT_ID" in result.payload["error"]


async def test_logs_do_not_contain_secrets_or_body(caplog: pytest.LogCaptureFixture) -> None:
    channel = _unit_channel()
    payload = _message_payload(text="绝密正文XYZ")
    query, raw = _encrypted_request(payload)

    with caplog.at_level(logging.INFO, logger="flux.core.channels.dingtalk_bot"):
        await channel.handle(raw, query, {})

    logs = caplog.text
    assert TOKEN not in logs
    assert AES_KEY not in logs
    assert APP_SECRET not in logs
    assert payload["text"]["content"] not in logs
    assert raw.decode("utf-8") not in logs
    assert query["signature"] not in logs
    await channel.aclose()


# --- 入站 → 审批草稿 → 允许/拒绝 ---


def test_inbound_creates_turn_but_never_sends_automatically(
    settings: Settings, db_schema: None
) -> None:
    with _callback_client(settings) as (client, container, context):
        query, raw = _encrypted_request(_message_payload())
        response = client.post(WEBHOOK, params=query, content=raw)
        _assert_encrypted_success(response)

        bot = client.get(f"{PREFIX}/bots/{context['bot']['id']}").json()["data"]
        pending = _wait_pending_decision(client, bot["chat_task_id"])
        assert pending["kind"] == DINGTALK_OUTBOUND_KIND
        assert pending["meta"]["conversation_id"] == CONVERSATION_ID
        assert pending["meta"]["staff_id"] == STAFF_ID
        assert pending["meta"]["robot_code"] == ROBOT_CODE
        assert pending["meta"]["message_id"] == MSG_ID
        assert pending["meta"]["bot_id"] == context["bot"]["id"]
        assert pending["meta"]["text_hash"]
        assert [option["label"] for option in pending["options"]] == ["允许", "拒绝"]

        # 回复只以草稿形式存在：审批完成前绝不调用发送接口
        assert context["sender"].messages == []
        drafts = client.get(
            f"{PREFIX}/outbound-drafts",
            params={"channel": "dingtalk", "status": "pending"},
        ).json()["data"]
        assert len(drafts) == 1
        assert drafts[0]["id"] == pending["meta"]["draft_id"]
        assert drafts[0]["body"] == "echo: 你好"
        assert drafts[0]["dest"]["conversation_id"] == CONVERSATION_ID
        assert drafts[0]["dest"]["staff_id"] == STAFF_ID
        assert drafts[0]["dest"]["robot_code"] == ROBOT_CODE
        assert drafts[0]["dest"]["message_id"] == MSG_ID
        assert drafts[0]["decision_id"] == pending["id"]
        assert container.outbound_drafts is not None


def test_allow_sends_once_and_deny_does_not_send(settings: Settings, db_schema: None) -> None:
    with _callback_client(settings) as (client, _container, context):
        query, raw = _encrypted_request(_message_payload())
        _assert_encrypted_success(client.post(WEBHOOK, params=query, content=raw))
        bot = client.get(f"{PREFIX}/bots/{context['bot']['id']}").json()["data"]
        pending = _wait_pending_decision(client, bot["chat_task_id"])
        draft_id = pending["meta"]["draft_id"]

        denied = client.post(
            f"{PREFIX}/tasks/{bot['chat_task_id']}/decisions/choose",
            json={"decision_id": pending["id"], "action": "choose", "option": "拒绝"},
        )
        assert denied.status_code == 200, denied.text
        assert context["sender"].messages == []
        draft = client.get(f"{PREFIX}/outbound-drafts/{draft_id}").json()["data"]
        assert draft["status"] == "denied"
        assert draft["body"] == "echo: 你好"
        assert draft["sent_at"] is None

    # 第二条消息走「允许」：只发送一次
    with _callback_client(settings) as (client, _container, context):
        query, raw = _encrypted_request(_message_payload(msg_id="msgr_allow=="))
        _assert_encrypted_success(client.post(WEBHOOK, params=query, content=raw))
        bot = client.get(f"{PREFIX}/bots/{context['bot']['id']}").json()["data"]
        pending = _wait_pending_decision(client, bot["chat_task_id"])
        draft_id = pending["meta"]["draft_id"]

        allowed = client.post(
            f"{PREFIX}/tasks/{bot['chat_task_id']}/decisions/choose",
            json={"decision_id": pending["id"], "action": "choose", "option": "允许"},
        )
        assert allowed.status_code == 200, allowed.text
        assert len(context["sender"].messages) == 1
        dest, text = context["sender"].messages[0]
        assert text == "echo: 你好"
        assert dest["conversation_id"] == CONVERSATION_ID
        assert dest["staff_id"] == STAFF_ID
        assert dest["robot_code"] == ROBOT_CODE
        sent = client.get(f"{PREFIX}/outbound-drafts/{draft_id}").json()["data"]
        assert sent["status"] == "sent"
        assert sent["sent_at"] is not None
        assert sent["decision_id"] == pending["id"]

        again = client.post(
            f"{PREFIX}/tasks/{bot['chat_task_id']}/decisions/choose",
            json={"decision_id": pending["id"], "action": "choose", "option": "允许"},
        )
        assert again.status_code == 409
        assert len(context["sender"].messages) == 1


def test_send_error_is_logged_and_does_not_break_decision(
    settings: Settings, db_schema: None
) -> None:
    with _callback_client(settings) as (client, container, context):
        sender = _FakeSender(error=RuntimeError("钉钉炸了"))
        container.dingtalk_bot._sender = sender  # noqa: SLF001 - 测试替身
        query, raw = _encrypted_request(_message_payload())
        _assert_encrypted_success(client.post(WEBHOOK, params=query, content=raw))
        bot = client.get(f"{PREFIX}/bots/{context['bot']['id']}").json()["data"]
        pending = _wait_pending_decision(client, bot["chat_task_id"])
        draft_id = pending["meta"]["draft_id"]

        response = client.post(
            f"{PREFIX}/tasks/{bot['chat_task_id']}/decisions/choose",
            json={"decision_id": pending["id"], "action": "choose", "option": "允许"},
        )
        assert response.status_code == 200, response.text
        assert len(sender.messages) == 1
        failed = client.get(f"{PREFIX}/outbound-drafts/{draft_id}").json()["data"]
        assert failed["status"] == "failed"
        assert failed["body"] == "echo: 你好"
        assert failed["error"]
        assert "echo: 你好" not in failed["error"]


def test_pending_draft_survives_process_restart(settings: Settings, db_schema: None) -> None:
    with _callback_client(settings) as (client, _container, context):
        query, raw = _encrypted_request(_message_payload())
        _assert_encrypted_success(client.post(WEBHOOK, params=query, content=raw))
        bot = client.get(f"{PREFIX}/bots/{context['bot']['id']}").json()["data"]
        pending = _wait_pending_decision(client, bot["chat_task_id"])
        draft_id = pending["meta"]["draft_id"]
        assert context["sender"].messages == []

    with _app(_dingtalk_settings(settings)) as (client, _container):
        draft = client.get(f"{PREFIX}/outbound-drafts/{draft_id}").json()["data"]
        assert draft["status"] == "pending"
        assert draft["channel"] == "dingtalk"
        assert draft["body"] == "echo: 你好"
        assert draft["bot_id"] == context["bot"]["id"]


# --- 公开路由与官方发送端 ---


def test_callback_route_is_public_on_main_app(settings: Settings, db_schema: None) -> None:
    """主应用配了 REST 令牌时，钉钉回调仍免鉴权（未配置频道则 400，不是 401）。"""
    app = create_app(settings.model_copy(update={"auth_token": "rest-token-for-tests"}))
    with TestClient(app) as client:
        response = client.post(WEBHOOK, json={})
        assert response.status_code == 400, response.text
        assert "FLUX_DINGTALK_TOKEN" in response.json()["error"]
        # 对照：REST 面仍然要令牌
        assert client.get(f"{PREFIX}/bots").status_code == 401


def test_callback_route_is_public_on_bot_node(settings: Settings, db_schema: None) -> None:
    secured = settings.model_copy(
        update={"auth_token": "rest-token-for-tests", "allow_no_auth": False}
    )
    app = create_bot_app(secured)
    with TestClient(app, client=("203.0.113.9", 41234)) as client:
        response = client.post(WEBHOOK, json={})
        assert response.status_code == 400, response.text
        assert "FLUX_DINGTALK_TOKEN" in response.json()["error"]
        assert client.get(f"{PREFIX}/bots").status_code == 401


def test_dingtalk_bot_sender_uses_official_robot_api() -> None:
    """不触网校验发送端拼装：accessToken + 单聊 / 群聊机器人消息接口。"""

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
            if url.endswith("/v1.0/oauth2/accessToken"):
                return _Response({"accessToken": "ding-token", "expireIn": 7200})
            return _Response({"processQueryKey": "pqk"})

        async def aclose(self) -> None:
            return None

    async def scenario() -> None:
        client = _Client()
        sender = DingTalkBotSender(
            app_key=APP_KEY,
            app_secret=APP_SECRET,
            api_base="https://api.dingtalk.com",
            client=client,  # type: ignore[arg-type]
        )
        await sender.send_text(
            {
                "conversation_id": CONVERSATION_ID,
                "staff_id": STAFF_ID,
                "robot_code": ROBOT_CODE,
                "conversation_type": "2",
            },
            "确认后的文本",
        )
        await sender.send_text(
            {"staff_id": STAFF_ID, "robot_code": ROBOT_CODE, "conversation_type": "1"},
            "单聊文本",
        )
        assert [call["url"] for call in client.calls] == [
            "https://api.dingtalk.com/v1.0/oauth2/accessToken",
            "https://api.dingtalk.com/v1.0/robot/groupMessages/send",
            "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
        ]
        token_call = client.calls[0]
        assert token_call["json"] == {"appKey": APP_KEY, "appSecret": APP_SECRET}

        group = client.calls[1]
        assert group["headers"] == {"x-acs-dingtalk-access-token": "ding-token"}
        assert group["json"]["robotCode"] == ROBOT_CODE
        assert group["json"]["openConversationId"] == CONVERSATION_ID
        assert group["json"]["msgKey"] == "sampleText"
        assert json.loads(group["json"]["msgParam"]) == {"content": "确认后的文本"}

        single = client.calls[2]
        assert single["json"]["robotCode"] == ROBOT_CODE
        assert single["json"]["userIds"] == [STAFF_ID]
        assert json.loads(single["json"]["msgParam"]) == {"content": "单聊文本"}

    asyncio.run(scenario())
