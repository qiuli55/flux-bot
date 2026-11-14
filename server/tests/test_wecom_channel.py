"""正式企业微信（WeCom）频道契约测试（加密回调入站 + 审批保护出站）。

全部离线：密文用同一套 AES-CBC 打包规则在测试里现加密，模型调用与发送接口都
替换成记录型替身；发送端另用 httpx.MockTransport 校验官方接口拼装。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from flux.bot_node.main import create_app as create_bot_app
from flux.config import Settings
from flux.container import Container
from flux.core.channels.inbound_media import (
    VOICE_ASR_FAILED_TEXT,
    VOICE_NO_ASR_TEXT,
    DownloadedMedia,
)
from flux.core.channels.wecom_bot import (
    WECOM_OUTBOUND_KIND,
    WeComBotChannel,
    WeComBotSender,
)
from flux.core.channels.wecom_crypto import (
    WeComCryptoError,
    compute_signature,
    decrypt_message,
    encrypt_message,
    verify_signature,
)
from flux.core.task_engine.assistant import AssistantTurn
from flux.main import create_app
from tests.fakes import AttachingAssistant

TOKEN = "wecom-token-for-tests"
CORP_ID = "ww_test_corp"
AGENT_ID = "1000002"
SECRET = "wecom-secret-for-tests"
#: 企业微信 EncodingAESKey：43 个字符，补 '=' 后 base64 解码为 32 字节。
ENCODING_AES_KEY = base64.b64encode(hashlib.sha256(b"wecom-test-key").digest()).decode().rstrip("=")
USER_ID = "zhangsan"
MESSAGE_ID = "7200000000000001"
WEBHOOK = "/api/v1/channels/wecom"
PREFIX = "/api/v1"
TIMESTAMP = "1700000000"
NONCE = "nonce-for-wecom-tests"


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
        self.find_calls: list[str | None] = []
        self.by_client_id: dict[str, Any] = {}
        self.turns: list[Any] = []

    async def create_turn(
        self,
        bot: Any,
        *,
        content: str,
        files: list[Any] | None = None,
        client_turn_id: str | None = None,
        cloud_user_id: str | None = None,
    ) -> tuple[Any, bool]:
        self.calls.append(
            {
                "bot_id": bot.id,
                "content": content,
                "files": list(files or ()),
                "client_turn_id": client_turn_id,
                "cloud_user_id": cloud_user_id,
            }
        )
        if client_turn_id is not None and client_turn_id in self.by_client_id:
            return self.by_client_id[client_turn_id], False
        turn = SimpleNamespace(id=uuid.uuid4(), task_id=uuid.uuid4())
        self.turns.append(turn)
        if client_turn_id is not None:
            self.by_client_id[client_turn_id] = turn
        return turn, True

    async def find_turn(self, bot: Any, client_turn_id: str | None) -> Any | None:
        self.find_calls.append(client_turn_id)
        if client_turn_id is None:
            return None
        return self.by_client_id.get(client_turn_id)

    async def wait_for_turn(self, bot_id: uuid.UUID, turn_id: uuid.UUID, *, wait_seconds: float):
        return SimpleNamespace(id=turn_id, status="failed", reply_message_id=None)


class _FakeBindings:
    """单元测试用的绑定服务：默认把测试发送者绑到主用户 1。"""

    def __init__(self, cloud_user_id: str | None = "1") -> None:
        self.cloud_user_id = cloud_user_id
        self.redeemed: list[str] = []

    async def get_binding(self, channel: str, external_user_id: str) -> Any | None:
        if self.cloud_user_id is None:
            return None
        return SimpleNamespace(cloud_user_id=self.cloud_user_id)

    async def redeem_code(self, code: str) -> str | None:
        self.redeemed.append(code)
        return None

    async def bind(self, **kwargs: Any) -> Any:
        return SimpleNamespace(cloud_user_id=kwargs.get("cloud_user_id"))


async def _chunks(*parts: bytes):
    for part in parts:
        yield part


class _FakeSender:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        payload: bytes = b"media-bytes",
        mime: str = "application/octet-stream",
        name: str = "",
        parts: list[bytes] | None = None,
    ) -> None:
        self.messages: list[tuple[dict[str, Any], str]] = []
        self.error = error
        self.closed = False
        self.payload = payload
        self.mime = mime
        self.name = name
        self.parts = parts
        self.downloads: list[str] = []
        self.media: list[dict[str, Any]] = []

    async def send_text(self, dest: dict[str, Any], text: str) -> None:
        self.messages.append((dest, text))
        if self.error is not None:
            raise self.error

    async def send_media(
        self, dest: dict[str, Any], *, path: str, name: str, mime: str, kind: str
    ) -> None:
        self.media.append(
            {
                "target": dict(dest),
                "name": name,
                "mime": mime,
                "kind": kind,
                "bytes": Path(path).read_bytes(),
            }
        )
        if self.error is not None:
            raise self.error

    async def download_media(self, media_id: str) -> DownloadedMedia:
        self.downloads.append(media_id)
        if self.error is not None:
            raise self.error
        parts = self.parts if self.parts is not None else [self.payload]
        return DownloadedMedia(name=self.name, mime=self.mime, chunks=_chunks(*parts))

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


def _text_message_xml(
    *,
    msg_id: str = MESSAGE_ID,
    content: str = "你好",
    from_user: str = USER_ID,
    msg_type: str = "text",
    corp_id: str = CORP_ID,
) -> str:
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{corp_id}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        "<CreateTime>1700000000</CreateTime>"
        f"<MsgType><![CDATA[{msg_type}]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        f"<MsgId>{msg_id}</MsgId>"
        f"<AgentID>{AGENT_ID}</AgentID>"
        "</xml>"
    )


def _media_message_xml(
    *,
    msg_type: str,
    msg_id: str = MESSAGE_ID,
    from_user: str = USER_ID,
    media_id: str = "MEDIA_ID_1",
    extra: str = "",
) -> str:
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        "<CreateTime>1700000000</CreateTime>"
        f"<MsgType><![CDATA[{msg_type}]]></MsgType>"
        f"<MediaId><![CDATA[{media_id}]]></MediaId>"
        f"{extra}"
        f"<MsgId>{msg_id}</MsgId>"
        f"<AgentID>{AGENT_ID}</AgentID>"
        "</xml>"
    )


def _encrypted_body(
    inner_xml: str,
    *,
    corp_id: str = CORP_ID,
    encoding_key: str = ENCODING_AES_KEY,
) -> tuple[str, bytes]:
    """生成密文与 POST 外层 XML，返回 (encrypt, raw_body)。"""
    encrypted = encrypt_message(encoding_key, inner_xml, corp_id)
    raw = (
        "<xml>"
        f"<ToUserName><![CDATA[{corp_id}]]></ToUserName>"
        f"<Encrypt><![CDATA[{encrypted}]]></Encrypt>"
        f"<AgentID>{AGENT_ID}</AgentID>"
        "</xml>"
    ).encode()
    return encrypted, raw


def _signed_query(
    encrypted: str,
    *,
    token: str = TOKEN,
    timestamp: str = TIMESTAMP,
    nonce: str = NONCE,
) -> dict[str, str]:
    return {
        "msg_signature": compute_signature(token, timestamp, nonce, encrypted),
        "timestamp": timestamp,
        "nonce": nonce,
    }


def _unit_channel(
    *,
    settings: Settings | None = None,
    sender: Any | None = None,
    **kwargs: Any,
) -> WeComBotChannel:
    bot = SimpleNamespace(id=uuid.uuid4(), name="企微小助", status="active")
    container = SimpleNamespace(
        bots=_FakeBots(bot),
        bot_conversation=_FakeConversation(),
        channel_bindings=_FakeBindings(),
        settings=settings,
    )
    kwargs.setdefault("bot_id", str(bot.id))
    return WeComBotChannel(
        container,  # type: ignore[arg-type]
        corp_id=CORP_ID,
        token=TOKEN,
        encoding_aes_key=ENCODING_AES_KEY,
        sender=sender,
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
    raise AssertionError("等待企业微信出站审批超时")


def _wait_queued_drafts(
    client: TestClient, task_id: str, count: int, timeout: float = 8.0
) -> list[dict]:
    """等该对话出现指定数量的「已落库、尚未挂卡」草稿，并按入队顺序返回。"""
    deadline = time.monotonic() + timeout
    rows: list[dict] = []
    while time.monotonic() < deadline:
        data = client.get(
            f"{PREFIX}/outbound-drafts", params={"channel": "wecom", "status": "pending"}
        ).json()["data"]
        rows = [row for row in data if row["task_id"] == task_id and row["decision_id"] is None]
        if len(rows) >= count:
            return sorted(rows, key=lambda row: row["created_at"])
        time.sleep(0.02)
    raise AssertionError(f"等待企业微信排队草稿超时：期望 {count}，实际 {len(rows)}")


def _wait_next_pending(
    client: TestClient, task_id: str, previous_id: str, timeout: float = 8.0
) -> dict:
    """等上一张卡被解决后新挂出的那张卡。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"{PREFIX}/tasks/{task_id}").json()["data"]
        pending = body.get("pending_decision")
        if pending is not None and pending["id"] != previous_id:
            return pending
        time.sleep(0.02)
    raise AssertionError("等待企业微信下一张出站审批卡超时")


@contextmanager
def _callback_client(
    settings: Settings, *, outbound_rule: bool = True
) -> Iterator[tuple[TestClient, Container, dict]]:
    """起 app、建一个 Bot、把频道指向它，并替换掉真实模型与发送端。

    默认插入一条 outbound_message 规则；验证默认零审批的用例传 `outbound_rule=False`。
    """
    with _app(
        settings,
        wecom_corp_id=CORP_ID,
        wecom_token=TOKEN,
        wecom_encoding_aes_key=ENCODING_AES_KEY,
    ) as (client, container):
        asyncio.run(
            container.channel_bindings.bind(
                channel="wecom", external_user_id=USER_ID, cloud_user_id="1"
            )
        )
        if outbound_rule:
            asyncio.run(
                container.approval_rules.create(
                    cloud_user_id="1",
                    rule_type="outbound_message",
                    params={"channel": "wecom"},
                )
            )
        container.assistant = _FakeAssistant()  # type: ignore[assignment]
        created = client.post(f"{PREFIX}/bots", json={"name": "企微小助", "description": "接企微"})
        assert created.status_code == 200, created.text
        bot = created.json()["data"]
        container.wecom_bot._bot_id = uuid.UUID(bot["id"])  # noqa: SLF001 - 测试注入归属 Bot
        sender = _FakeSender()
        container.wecom_bot._sender = sender  # noqa: SLF001 - 测试替身
        yield client, container, {"bot": bot, "sender": sender}


def _post_callback(client: TestClient, *, content: str = "你好", msg_id: str = MESSAGE_ID):
    encrypted, raw = _encrypted_body(_text_message_xml(msg_id=msg_id, content=content))
    return client.post(WEBHOOK, params=_signed_query(encrypted), content=raw)


# --- 加解密与签名 ---


def test_encrypt_decrypt_round_trip() -> None:
    plaintext = "<xml><Content><![CDATA[你好]]></Content></xml>"
    encrypted = encrypt_message(ENCODING_AES_KEY, plaintext, CORP_ID)
    msg, receiveid = decrypt_message(ENCODING_AES_KEY, encrypted)
    assert msg == plaintext
    assert receiveid == CORP_ID

    # receiveid 不一致 / 换错密钥 / 非法 base64 都必须失败
    with pytest.raises(WeComCryptoError):
        decrypt_message(ENCODING_AES_KEY, encrypted, expected_receiveid="other-corp")
    other_key = base64.b64encode(hashlib.sha256(b"other-key").digest()).decode().rstrip("=")
    with pytest.raises(WeComCryptoError):
        decrypt_message(other_key, encrypted)
    with pytest.raises(WeComCryptoError):
        decrypt_message(ENCODING_AES_KEY, "not-base64")


def test_signature_matches_official_recipe() -> None:
    encrypted = "ciphertext"
    expected = hashlib.sha1(
        "".join(sorted([TOKEN, TIMESTAMP, NONCE, encrypted])).encode("utf-8")
    ).hexdigest()
    assert compute_signature(TOKEN, TIMESTAMP, NONCE, encrypted) == expected
    assert verify_signature(
        token=TOKEN,
        timestamp=TIMESTAMP,
        nonce=NONCE,
        encrypted=encrypted,
        signature=expected,
    )
    # 密文变一个字符就验不过；缺任一项 fail-closed
    assert not verify_signature(
        token=TOKEN,
        timestamp=TIMESTAMP,
        nonce=NONCE,
        encrypted=encrypted + "x",
        signature=expected,
    )
    assert not verify_signature(
        token=None,
        timestamp=TIMESTAMP,
        nonce=NONCE,
        encrypted=encrypted,
        signature=expected,
    )
    # 公网传来的签名可能是非 ASCII：必须安全地判 False，而不是抛 TypeError
    assert not verify_signature(
        token=TOKEN,
        timestamp=TIMESTAMP,
        nonce=NONCE,
        encrypted=encrypted,
        signature="伪造签名",
    )


# --- GET VerifyURL ---


async def test_get_verify_returns_plaintext_echostr() -> None:
    channel = _unit_channel()
    plaintext = "echo-plaintext-abc123"
    echostr = encrypt_message(ENCODING_AES_KEY, plaintext, CORP_ID)
    query = _signed_query(echostr) | {"echostr": echostr}

    result = await channel.handle_get(query)
    assert result.status_code == 200
    # 必须返回明文，而不是密文或 JSON
    assert result.body == plaintext
    await channel.aclose()


async def test_get_verify_rejects_bad_signature_and_missing_config() -> None:
    channel = _unit_channel()
    echostr = encrypt_message(ENCODING_AES_KEY, "ok", CORP_ID)
    query = _signed_query(echostr) | {"echostr": echostr}

    bad = dict(query, msg_signature="0" * 40)
    rejected = await channel.handle_get(bad)
    assert 400 <= rejected.status_code < 500

    # 未配置 token / EncodingAESKey：一切回调都拒绝
    unconfigured = WeComBotChannel(None, corp_id=CORP_ID)  # type: ignore[arg-type]
    missing = await unconfigured.handle_get(query)
    assert 400 <= missing.status_code < 500

    # 缺 echostr 也拒绝
    absent = await channel.handle_get(_signed_query(echostr))
    assert 400 <= absent.status_code < 500
    await channel.aclose()


async def test_get_verify_rejects_corp_mismatch() -> None:
    channel = _unit_channel()
    echostr = encrypt_message(ENCODING_AES_KEY, "ok", "ww_other_corp")
    query = _signed_query(echostr) | {"echostr": echostr}
    result = await channel.handle_get(query)
    assert 400 <= result.status_code < 500
    await channel.aclose()


# --- POST 加密 XML ---


async def test_post_decrypts_text_message_and_is_idempotent() -> None:
    channel = _unit_channel()
    encrypted, raw = _encrypted_body(_text_message_xml())
    query = _signed_query(encrypted)

    first = await channel.handle_post(raw, query)
    assert first.status_code == 200, first.body
    assert first.body == "success"

    replay = await channel.handle_post(raw, query)
    assert replay.status_code == 200

    conversation = channel._container.bot_conversation  # type: ignore[attr-defined]  # noqa: SLF001
    assert len(conversation.calls) == 2
    assert conversation.calls[0]["content"] == "你好"
    assert conversation.calls[0]["client_turn_id"] == MESSAGE_ID
    await channel.aclose()


async def test_post_rejects_bad_signature_and_ignores_unsupported_types() -> None:
    channel = _unit_channel()
    encrypted, raw = _encrypted_body(_text_message_xml())
    bad = dict(_signed_query(encrypted), msg_signature="0" * 40)
    rejected = await channel.handle_post(raw, bad)
    assert rejected.status_code == 403

    # 视频 / 位置 / 链接 / 事件都不支持；图片/语音/文件已支持，不再忽略。
    for msg_type in ("video", "location", "link", "event"):
        body_encrypted, body_raw = _encrypted_body(_media_message_xml(msg_type=msg_type))
        ignored = await channel.handle_post(body_raw, _signed_query(body_encrypted))
        assert ignored.status_code == 200, msg_type
        assert ignored.body == "success"

    conversation = channel._container.bot_conversation  # type: ignore[attr-defined]  # noqa: SLF001
    assert conversation.calls == []
    await channel.aclose()


async def test_post_rejects_dtd_and_malformed_xml() -> None:
    channel = _unit_channel()
    dtd = (
        b'<?xml version="1.0"?><!DOCTYPE xml [<!ENTITY x "boom">]><xml><Encrypt>&x;</Encrypt></xml>'
    )
    rejected = await channel.handle_post(dtd, {})
    assert 400 <= rejected.status_code < 500

    malformed = await channel.handle_post(b"not xml", {})
    assert 400 <= malformed.status_code < 500
    await channel.aclose()


async def test_post_without_bot_id_is_fail_closed() -> None:
    channel = _unit_channel()
    channel._bot_id = None  # noqa: SLF001 - 模拟漏配
    encrypted, raw = _encrypted_body(_text_message_xml())
    result = await channel.handle_post(raw, _signed_query(encrypted))
    assert result.status_code == 400
    assert "FLUX_WECOM_BOT_ID" in result.body
    await channel.aclose()


# --- 入站媒体 ---


async def _post_media(
    channel: WeComBotChannel,
    *,
    msg_type: str,
    msg_id: str = MESSAGE_ID,
    media_id: str = "MEDIA_ID_1",
    extra: str = "",
):
    xml = _media_message_xml(msg_type=msg_type, msg_id=msg_id, media_id=media_id, extra=extra)
    encrypted, raw = _encrypted_body(xml)
    return await channel.handle_post(raw, _signed_query(encrypted))


async def test_inbound_image_downloads_and_creates_turn(settings: Settings) -> None:
    sender = _FakeSender(payload=b"jpeg-bytes", mime="image/jpeg", name="wx-photo.jpg")
    channel = _unit_channel(settings=settings, sender=sender)

    result = await _post_media(channel, msg_type="image", media_id="MEDIA_IMG")
    assert result.status_code == 200
    assert result.body == "success"

    conversation = channel._container.bot_conversation  # type: ignore[attr-defined]  # noqa: SLF001
    call = conversation.calls[0]
    assert call["content"] == ""
    uploaded = call["files"][0]
    assert uploaded.name == "wx-photo.jpg"
    assert uploaded.mime == "image/jpeg"
    assert uploaded.size == len(b"jpeg-bytes")
    assert Path(uploaded.path).is_file()
    assert Path(uploaded.path).resolve().is_relative_to(Path(settings.uploads_dir).resolve())
    assert sender.downloads == ["MEDIA_IMG"]
    await channel.aclose()


async def test_inbound_file_keeps_platform_filename(settings: Settings) -> None:
    sender = _FakeSender(payload=b"file-body", mime="application/octet-stream")
    channel = _unit_channel(settings=settings, sender=sender)

    result = await _post_media(
        channel,
        msg_type="file",
        media_id="MEDIA_FILE",
        extra="<FileName><![CDATA[报销单.xlsx]]></FileName>",
    )
    assert result.status_code == 200

    conversation = channel._container.bot_conversation  # type: ignore[attr-defined]  # noqa: SLF001
    uploaded = conversation.calls[0]["files"][0]
    assert uploaded.name == "报销单.xlsx"
    assert uploaded.size == len(b"file-body")
    await channel.aclose()


async def test_inbound_voice_without_asr_keeps_audio_and_explains(settings: Settings) -> None:
    sender = _FakeSender(payload=b"amr", mime="audio/amr")
    channel = _unit_channel(settings=settings, sender=sender)

    result = await _post_media(
        channel,
        msg_type="voice",
        media_id="MEDIA_VOICE",
        extra="<Format><![CDATA[amr]]></Format>",
    )
    assert result.status_code == 200

    conversation = channel._container.bot_conversation  # type: ignore[attr-defined]  # noqa: SLF001
    call = conversation.calls[0]
    assert call["content"] == VOICE_NO_ASR_TEXT
    assert call["files"][0].mime == "audio/amr"
    assert sender.downloads == ["MEDIA_VOICE"]
    await channel.aclose()


async def test_inbound_voice_uses_platform_recognition(settings: Settings) -> None:
    sender = _FakeSender(payload=b"amr", mime="audio/amr")
    channel = _unit_channel(settings=settings, sender=sender)

    await _post_media(
        channel,
        msg_type="voice",
        extra=(
            "<Format><![CDATA[amr]]></Format>"
            "<Recognition><![CDATA[帮我订明天下午的会议室]]></Recognition>"
        ),
    )

    conversation = channel._container.bot_conversation  # type: ignore[attr-defined]  # noqa: SLF001
    assert conversation.calls[0]["content"] == "帮我订明天下午的会议室"
    assert len(conversation.calls[0]["files"]) == 1
    await channel.aclose()


async def test_inbound_voice_uses_injected_transcriber(settings: Settings) -> None:
    class _Transcriber:
        def __init__(self) -> None:
            self.calls: list[dict[str, str]] = []

        async def transcribe(self, *, path: str, name: str, mime: str) -> str:
            self.calls.append({"path": path, "name": name, "mime": mime})
            return "企微转写结果"

    transcriber = _Transcriber()
    sender = _FakeSender(payload=b"speex", mime="audio/speex")
    channel = _unit_channel(settings=settings, sender=sender, transcriber=transcriber)

    await _post_media(channel, msg_type="voice", extra="<Format><![CDATA[speex]]></Format>")

    conversation = channel._container.bot_conversation  # type: ignore[attr-defined]  # noqa: SLF001
    uploaded = conversation.calls[0]["files"][0]
    assert conversation.calls[0]["content"] == "企微转写结果"
    assert transcriber.calls == [
        {"path": uploaded.path, "name": uploaded.name, "mime": uploaded.mime}
    ]
    await channel.aclose()


async def test_inbound_voice_transcriber_failure_is_explained(settings: Settings) -> None:
    class _BrokenTranscriber:
        async def transcribe(self, *, path: str, name: str, mime: str) -> str:
            raise RuntimeError("asr failed access_token=leak")

    sender = _FakeSender(payload=b"amr", mime="audio/amr")
    channel = _unit_channel(settings=settings, sender=sender, transcriber=_BrokenTranscriber())

    result = await _post_media(channel, msg_type="voice", extra="<Format><![CDATA[amr]]></Format>")
    assert result.status_code == 200

    conversation = channel._container.bot_conversation  # type: ignore[attr-defined]  # noqa: SLF001
    assert conversation.calls[0]["content"] == VOICE_ASR_FAILED_TEXT
    assert len(conversation.calls[0]["files"]) == 1
    await channel.aclose()


async def test_inbound_media_replay_does_not_download_twice(settings: Settings) -> None:
    sender = _FakeSender(payload=b"img", mime="image/png", name="a.png")
    channel = _unit_channel(settings=settings, sender=sender)

    first = await _post_media(channel, msg_type="image", msg_id="MSG_REPLAY")
    second = await _post_media(channel, msg_type="image", msg_id="MSG_REPLAY")
    assert first.status_code == 200 and second.status_code == 200
    assert first.body == second.body == "success"
    assert sender.downloads == ["MEDIA_ID_1"]

    conversation = channel._container.bot_conversation  # type: ignore[attr-defined]  # noqa: SLF001
    assert len(conversation.calls) == 1
    assert conversation.find_calls == ["MSG_REPLAY", "MSG_REPLAY"]
    await channel.aclose()


async def test_inbound_media_over_50mb_is_acknowledged_without_upload(
    settings: Settings,
) -> None:
    megabyte = b"x" * (1024 * 1024)
    sender = _FakeSender(mime="application/octet-stream", parts=[megabyte] * 51)
    channel = _unit_channel(settings=settings, sender=sender)

    result = await _post_media(channel, msg_type="file")
    assert result.status_code == 200
    assert result.body == "success"

    conversation = channel._container.bot_conversation  # type: ignore[attr-defined]  # noqa: SLF001
    assert conversation.calls == []
    assert list(Path(settings.uploads_dir).glob("*")) == []
    await channel.aclose()


async def test_inbound_media_without_credentials_explains_in_chinese(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    channel = _unit_channel(settings=settings)  # 没有 corp_secret → 无发送端

    result = await _post_media(channel, msg_type="image")
    assert result.status_code == 200
    assert result.body == "success"

    conversation = channel._container.bot_conversation  # type: ignore[attr-defined]  # noqa: SLF001
    call = conversation.calls[0]
    assert "未配置企业微信应用凭据" in call["content"]
    assert call["files"] == []
    assert "corpsecret" not in caplog.text.lower()
    await channel.aclose()


def _wait_sent(client: TestClient, task_id: str, timeout: float = 8.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    rows: list[dict] = []
    while time.monotonic() < deadline:
        data = client.get(f"{PREFIX}/outbound-drafts", params={"channel": "wecom"}).json()["data"]
        rows = [row for row in data if row["task_id"] == task_id and row["status"] == "sent"]
        if rows:
            return rows
        time.sleep(0.02)
    raise AssertionError("等待自动发送超时")


def test_no_outbound_rule_auto_sends_without_a_card(settings: Settings, db_schema: None) -> None:
    with _callback_client(settings, outbound_rule=False) as (client, _container, context):
        assert _post_callback(client).status_code == 200
        bot = client.get(f"{PREFIX}/bots/{context['bot']['id']}").json()["data"]
        sent = _wait_sent(client, bot["chat_task_id"])
        assert context["sender"].messages[0][1] == "echo: 你好"
        assert sent[0]["decision_id"] is None
        task = client.get(f"{PREFIX}/tasks/{bot['chat_task_id']}").json()["data"]
        assert task["pending_decision"] is None


# --- 命中出站规则 → 审批草稿 → 允许/拒绝 ---


def test_inbound_creates_turn_but_never_sends_automatically(
    settings: Settings, db_schema: None
) -> None:
    with _callback_client(settings) as (client, container, context):
        response = _post_callback(client)
        assert response.status_code == 200, response.text
        assert response.text == "success"

        bot = client.get(f"{PREFIX}/bots/{context['bot']['id']}").json()["data"]
        pending = _wait_pending_decision(client, bot["chat_task_id"])
        assert pending["kind"] == WECOM_OUTBOUND_KIND
        assert pending["meta"]["touser"] == USER_ID
        assert pending["meta"]["message_id"] == MESSAGE_ID
        assert pending["meta"]["bot_id"] == context["bot"]["id"]
        assert pending["meta"]["text_hash"]
        assert [option["label"] for option in pending["options"]] == ["允许", "拒绝"]

        # 回复只以草稿形式存在：审批完成前绝不调用发送接口
        assert context["sender"].messages == []
        drafts = client.get(
            f"{PREFIX}/outbound-drafts",
            params={"channel": "wecom", "status": "pending"},
        ).json()["data"]
        assert len(drafts) == 1
        assert drafts[0]["id"] == pending["meta"]["draft_id"]
        assert drafts[0]["body"] == "echo: 你好"
        assert drafts[0]["dest"] == {"touser": USER_ID, "message_id": MESSAGE_ID}
        assert drafts[0]["decision_id"] == pending["id"]
        assert container.wecom_bot is not None


def test_allow_sends_once_and_deny_does_not_send(settings: Settings, db_schema: None) -> None:
    with _callback_client(settings) as (client, _container, context):
        assert _post_callback(client).status_code == 200
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
        assert _post_callback(client, msg_id="7200000000000002").status_code == 200
        bot = client.get(f"{PREFIX}/bots/{context['bot']['id']}").json()["data"]
        pending = _wait_pending_decision(client, bot["chat_task_id"])
        draft_id = pending["meta"]["draft_id"]

        allowed = client.post(
            f"{PREFIX}/tasks/{bot['chat_task_id']}/decisions/choose",
            json={"decision_id": pending["id"], "action": "choose", "option": "允许"},
        )
        assert allowed.status_code == 200, allowed.text
        assert context["sender"].messages == [
            ({"touser": USER_ID, "message_id": "7200000000000002"}, "echo: 你好")
        ]
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
        assert len(context["sender"].messages) == 1


def test_pending_decision_queues_replies_and_drains_in_order(
    settings: Settings, db_schema: None
) -> None:
    """BUG-H：已有待决策项时不丢回复，排队后按顺序逐条挂卡。"""
    with _callback_client(settings) as (client, _container, context):
        assert (
            _post_callback(client, content="第一条", msg_id="7200000000000101").status_code == 200
        )
        bot = client.get(f"{PREFIX}/bots/{context['bot']['id']}").json()["data"]
        task_id = bot["chat_task_id"]
        card_a = _wait_pending_decision(client, task_id)

        # 第二、三条回复在 A 卡未解决时到达：落库排队，不丢弃。
        assert (
            _post_callback(client, content="第二条", msg_id="7200000000000102").status_code == 200
        )
        assert (
            _post_callback(client, content="第三条", msg_id="7200000000000103").status_code == 200
        )
        queued = _wait_queued_drafts(client, task_id, 2)
        assert [row["body"] for row in queued] == ["echo: 第二条", "echo: 第三条"]

        task = client.get(f"{PREFIX}/tasks/{task_id}").json()["data"]
        assert task["pending_decision"]["id"] == card_a["id"]
        assert all(row["status"] == "pending" for row in queued)

        allowed = client.post(
            f"{PREFIX}/tasks/{task_id}/decisions/choose",
            json={"decision_id": card_a["id"], "action": "choose", "option": "允许"},
        )
        assert allowed.status_code == 200, allowed.text
        card_b = _wait_next_pending(client, task_id, card_a["id"])
        assert card_b["meta"]["draft_id"] == queued[0]["id"]
        assert card_b["kind"] == WECOM_OUTBOUND_KIND
        assert card_b["meta"]["message_id"] == "7200000000000102"
        assert [text for _dest, text in context["sender"].messages] == ["echo: 第一条"]

        denied = client.post(
            f"{PREFIX}/tasks/{task_id}/decisions/choose",
            json={"decision_id": card_b["id"], "action": "choose", "option": "拒绝"},
        )
        assert denied.status_code == 200, denied.text
        card_c = _wait_next_pending(client, task_id, card_b["id"])
        assert card_c["meta"]["draft_id"] == queued[1]["id"]
        assert card_c["meta"]["message_id"] == "7200000000000103"
        assert [text for _dest, text in context["sender"].messages] == ["echo: 第一条"]

        allowed_c = client.post(
            f"{PREFIX}/tasks/{task_id}/decisions/choose",
            json={"decision_id": card_c["id"], "action": "choose", "option": "允许"},
        )
        assert allowed_c.status_code == 200, allowed_c.text
        assert [text for _dest, text in context["sender"].messages] == [
            "echo: 第一条",
            "echo: 第三条",
        ]
        task = client.get(f"{PREFIX}/tasks/{task_id}").json()["data"]
        assert task["pending_decision"] is None

        drafts = client.get(f"{PREFIX}/outbound-drafts", params={"channel": "wecom"}).json()["data"]
        assert [row["status"] for row in sorted(drafts, key=lambda row: row["created_at"])] == [
            "sent",
            "denied",
            "sent",
        ]


def test_outbound_option_text_is_about_sending_not_commands(
    settings: Settings, db_schema: None
) -> None:
    """BUG-I：出站卡片的描述/影响只谈发送，不复用终端命令文案。"""
    with _callback_client(settings) as (client, _container, context):
        assert _post_callback(client).status_code == 200
        bot = client.get(f"{PREFIX}/bots/{context['bot']['id']}").json()["data"]
        pending = _wait_pending_decision(client, bot["chat_task_id"])

        assert [option["label"] for option in pending["options"]] == ["允许", "拒绝"]
        allow, deny = pending["options"]
        for option in (allow, deny):
            text = f"{option['description']} {option['impact']}"
            assert "命令" not in text
            assert "终端" not in text
            assert "执行" not in text
        assert "发送" in allow["impact"]
        assert "无法撤回" in allow["impact"]
        assert "不会发送" in deny["impact"]
        assert "草稿" in deny["impact"]


def test_allow_sends_via_official_api_then_marks_sent(settings: Settings, db_schema: None) -> None:
    """允许后走真实发送端：gettoken → message/send，成功才 mark_sent。"""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(
                200,
                json={"errcode": 0, "access_token": "api-token", "expires_in": 7200},
            )
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with _callback_client(settings) as (client, container, context):
        container.wecom_bot._sender = WeComBotSender(  # noqa: SLF001 - 测试替身
            corp_id=CORP_ID,
            secret=SECRET,
            agent_id=AGENT_ID,
            client=http_client,
        )
        assert _post_callback(client).status_code == 200
        bot = client.get(f"{PREFIX}/bots/{context['bot']['id']}").json()["data"]
        pending = _wait_pending_decision(client, bot["chat_task_id"])

        allowed = client.post(
            f"{PREFIX}/tasks/{bot['chat_task_id']}/decisions/choose",
            json={"decision_id": pending["id"], "action": "choose", "option": "允许"},
        )
        assert allowed.status_code == 200, allowed.text

        draft = client.get(f"{PREFIX}/outbound-drafts/{pending['meta']['draft_id']}").json()["data"]
        assert draft["status"] == "sent"

    # 调用方传入的 client 由测试自己关闭（与 sender 自建连接池的归属区分开）
    asyncio.run(http_client.aclose())
    assert [request.url.path for request in requests] == [
        "/cgi-bin/gettoken",
        "/cgi-bin/message/send",
    ]
    payload = json.loads(requests[-1].content)
    assert payload["touser"] == USER_ID
    assert payload["agentid"] == int(AGENT_ID)
    assert payload["text"]["content"] == "echo: 你好"


def test_send_error_marks_failed_and_keeps_body(settings: Settings, db_schema: None) -> None:
    with _callback_client(settings) as (client, container, context):
        sender = _FakeSender(error=RuntimeError("企业微信炸了"))
        container.wecom_bot._sender = sender  # noqa: SLF001 - 测试替身
        assert _post_callback(client).status_code == 200
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
        failed = client.get(f"{PREFIX}/outbound-drafts/{draft_id}").json()["data"]
        assert failed["status"] == "failed"
        assert failed["body"] == "echo: 你好"
        assert failed["error"]
        assert "echo: 你好" not in failed["error"]


# --- 公开路由与发送端 ---


def test_public_callback_is_mounted_without_rest_auth(settings: Settings, db_schema: None) -> None:
    configured = settings.model_copy(
        update={
            "auth_token": "rest-token-for-wecom-tests",
            "wecom_corp_id": CORP_ID,
            "wecom_token": TOKEN,
            "wecom_encoding_aes_key": ENCODING_AES_KEY,
        }
    )
    remote = {"client": ("203.0.113.9", 41234)}

    # 主应用：REST 面 401，但频道回调只按自己的验签规则拒绝（不是 401）
    main_app = create_app(configured)
    with TestClient(main_app, **remote) as client:
        assert client.get(f"{PREFIX}/bots").status_code == 401
        callback = client.get(WEBHOOK)
        assert callback.status_code != 401
        assert callback.status_code == 403

    # Bot 节点同样免 REST 鉴权
    bot_app = create_bot_app(configured)
    with TestClient(bot_app, **remote) as client:
        assert client.get(f"{PREFIX}/bots").status_code == 401
        callback = client.get(WEBHOOK)
        assert callback.status_code != 401
        assert callback.status_code == 403


def test_wecom_bot_sender_uses_official_send_api() -> None:
    """不触网校验发送端拼装：gettoken + POST /cgi-bin/message/send。"""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/cgi-bin/gettoken":
            assert request.url.params["corpid"] == CORP_ID
            assert request.url.params["corpsecret"] == SECRET
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "errmsg": "ok",
                    "access_token": "wecom-access-token",
                    "expires_in": 7200,
                },
            )
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            sender = WeComBotSender(
                corp_id=CORP_ID,
                secret=SECRET,
                agent_id=AGENT_ID,
                client=http_client,
            )
            await sender.send_text({"touser": USER_ID}, "确认后的文本")
            await sender.aclose()

    asyncio.run(scenario())

    assert [request.url.path for request in requests] == [
        "/cgi-bin/gettoken",
        "/cgi-bin/message/send",
    ]
    send = requests[-1]
    assert send.method == "POST"
    assert send.url.params["access_token"] == "wecom-access-token"
    payload = json.loads(send.content)
    assert payload == {
        "touser": USER_ID,
        "msgtype": "text",
        "agentid": int(AGENT_ID),
        "text": {"content": "确认后的文本"},
    }


def test_sender_error_does_not_leak_secret(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(
                200,
                json={"errcode": 0, "access_token": "t", "expires_in": 7200},
            )
        return httpx.Response(200, json={"errcode": 40001, "errmsg": "invalid credential"})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            sender = WeComBotSender(
                corp_id=CORP_ID,
                secret=SECRET,
                agent_id=AGENT_ID,
                client=http_client,
            )
            with pytest.raises(RuntimeError) as excinfo:
                await sender.send_text({"touser": USER_ID}, "文本")
            assert SECRET not in str(excinfo.value)
            assert "invalid credential" in str(excinfo.value)
            await sender.aclose()

    asyncio.run(scenario())
    # httpx 会把请求 URL 打进 INFO 日志；凭据必须已脱敏，但接口路径仍可排查
    logged = caplog.text
    assert "/cgi-bin/gettoken" in logged
    assert SECRET not in logged
    assert "corpsecret=***" in logged


async def test_logs_do_not_contain_secrets_or_message_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    channel = _unit_channel()
    body_text = "绝密消息正文-9f3a"

    echostr = encrypt_message(ENCODING_AES_KEY, "echo-ok", CORP_ID)
    await channel.handle_get(_signed_query(echostr) | {"echostr": echostr})
    encrypted, raw = _encrypted_body(_text_message_xml(content=body_text))
    await channel.handle_post(raw, _signed_query(encrypted))
    await channel.aclose()

    logged = caplog.text
    assert ENCODING_AES_KEY not in logged
    assert SECRET not in logged
    assert body_text not in logged


def test_wecom_sender_downloads_media_via_official_api() -> None:
    """官方 media/get：access_token 与 media_id 都在查询串，成功时是二进制流。"""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(
                200,
                json={"errcode": 0, "access_token": "wecom-media-token", "expires_in": 7200},
            )
        return httpx.Response(
            200,
            content=b"media-body",
            headers={
                "content-type": "image/jpeg",
                "content-disposition": 'attachment; filename="wx.jpg"',
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            sender = WeComBotSender(
                corp_id=CORP_ID,
                secret=SECRET,
                agent_id=AGENT_ID,
                client=http_client,
            )
            media = await sender.download_media("MEDIA_ID_9")
            assert media.mime == "image/jpeg"
            assert media.name == "wx.jpg"
            chunks = [chunk async for chunk in media.chunks]
            assert b"".join(chunks) == b"media-body"
            await sender.aclose()

    asyncio.run(scenario())

    assert [request.url.path for request in requests] == ["/cgi-bin/gettoken", "/cgi-bin/media/get"]
    media_call = requests[-1]
    assert media_call.method == "GET"
    assert media_call.url.params["access_token"] == "wecom-media-token"
    assert media_call.url.params["media_id"] == "MEDIA_ID_9"


def test_wecom_sender_rejects_json_error_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "t", "expires_in": 7200})
        return httpx.Response(
            200,
            json={"errcode": 40007, "errmsg": "invalid media_id"},
            headers={"content-type": "application/json"},
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            sender = WeComBotSender(
                corp_id=CORP_ID, secret=SECRET, agent_id=AGENT_ID, client=http_client
            )
            with pytest.raises(RuntimeError) as excinfo:
                await sender.download_media("bad")
            assert "invalid media_id" in str(excinfo.value)
            assert SECRET not in str(excinfo.value)
            await sender.aclose()

    asyncio.run(scenario())


def test_wecom_sender_uploads_then_sends_image_and_file(tmp_path: Path) -> None:
    """官方链路：media/upload?type=image|file 拿 media_id，再按 msgtype 发送。"""
    image = tmp_path / "chart.png"
    image.write_bytes(b"\x89PNG fake")
    document = tmp_path / "report.pdf"
    document.write_bytes(b"%PDF-1.4 fake")

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "access_token": "wecom-access-token",
                    "expires_in": 7200,
                },
            )
        if request.url.path == "/cgi-bin/media/upload":
            media_type = request.url.params["type"]
            return httpx.Response(
                200,
                json={"errcode": 0, "media_id": f"media-{media_type}"},
            )
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            sender = WeComBotSender(
                corp_id=CORP_ID,
                secret=SECRET,
                agent_id=AGENT_ID,
                client=http_client,
            )
            await sender.send_media(
                {"touser": USER_ID},
                path=str(image),
                name="chart.png",
                mime="image/png",
                kind="image",
            )
            await sender.send_media(
                {"chatid": "chat-1"},
                path=str(document),
                name="report.pdf",
                mime="application/pdf",
                kind="file",
            )
            await sender.aclose()

    asyncio.run(scenario())

    assert [request.url.path for request in requests] == [
        "/cgi-bin/gettoken",
        "/cgi-bin/media/upload",
        "/cgi-bin/message/send",
        "/cgi-bin/media/upload",
        "/cgi-bin/appchat/send",
    ]
    image_upload, image_send, file_upload, file_send = requests[1:]
    assert image_upload.url.params["access_token"] == "wecom-access-token"
    assert image_upload.url.params["type"] == "image"
    assert b'name="media"' in image_upload.content
    assert b"chart.png" in image_upload.content
    assert json.loads(image_send.content) == {
        "touser": USER_ID,
        "msgtype": "image",
        "agentid": int(AGENT_ID),
        "image": {"media_id": "media-image"},
    }

    assert file_upload.url.params["type"] == "file"
    assert b"report.pdf" in file_upload.content
    assert json.loads(file_send.content) == {
        "chatid": "chat-1",
        "msgtype": "file",
        "file": {"media_id": "media-file"},
    }


def _use_workspace(container: Container, workspace: Path) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    container.bots.workspace_path = lambda _bot: workspace  # type: ignore[method-assign]
    return workspace


def test_media_only_reply_queues_attachment_then_sends_after_allow(
    settings: Settings, db_schema: None, tmp_path: Path
) -> None:
    with _callback_client(settings) as (client, container, context):
        payload = b"\x89PNG wecom"
        workspace = _use_workspace(container, tmp_path / "workspace")
        (workspace / "chart.png").write_bytes(payload)
        container.assistant = AttachingAssistant(path="chart.png", reply="")  # type: ignore[assignment]

        assert _post_callback(client).status_code == 200
        bot = client.get(f"{PREFIX}/bots/{context['bot']['id']}").json()["data"]
        pending = _wait_pending_decision(client, bot["chat_task_id"])

        assert pending["kind"] == WECOM_OUTBOUND_KIND
        attachment = pending["meta"]["attachments"][0]
        assert attachment["name"] == "chart.png"
        assert attachment["kind"] == "image"
        assert attachment["size"] == len(payload)
        assert "chart.png" in pending["context"]
        assert "image/png" in pending["context"]
        # 审批前不上传、不发送。
        assert context["sender"].messages == []
        assert context["sender"].media == []

        draft = client.get(f"{PREFIX}/outbound-drafts/{pending['meta']['draft_id']}").json()["data"]
        assert draft["body"] == ""
        assert draft["attachments"][0]["name"] == "chart.png"

        allowed = client.post(
            f"{PREFIX}/tasks/{bot['chat_task_id']}/decisions/choose",
            json={"decision_id": pending["id"], "action": "choose", "option": "允许"},
        )
        assert allowed.status_code == 200, allowed.text
        assert context["sender"].messages == []
        assert len(context["sender"].media) == 1
        assert context["sender"].media[0]["bytes"] == payload
        assert context["sender"].media[0]["kind"] == "image"
        assert (
            client.get(f"{PREFIX}/outbound-drafts/{draft['id']}").json()["data"]["status"] == "sent"
        )


def test_media_reply_denied_never_uploads_or_sends(
    settings: Settings, db_schema: None, tmp_path: Path
) -> None:
    with _callback_client(settings) as (client, container, context):
        workspace = _use_workspace(container, tmp_path / "workspace")
        (workspace / "chart.png").write_bytes(b"png")
        container.assistant = AttachingAssistant(path="chart.png", reply="")  # type: ignore[assignment]
        assert _post_callback(client).status_code == 200
        bot = client.get(f"{PREFIX}/bots/{context['bot']['id']}").json()["data"]
        pending = _wait_pending_decision(client, bot["chat_task_id"])

        denied = client.post(
            f"{PREFIX}/tasks/{bot['chat_task_id']}/decisions/choose",
            json={"decision_id": pending["id"], "action": "choose", "option": "拒绝"},
        )
        assert denied.status_code == 200, denied.text
        assert context["sender"].messages == []
        assert context["sender"].media == []
        assert (
            client.get(f"{PREFIX}/outbound-drafts/{pending['meta']['draft_id']}").json()["data"][
                "status"
            ]
            == "denied"
        )


def test_image_over_wecom_limit_fails_before_upload(
    settings: Settings, db_schema: None, tmp_path: Path
) -> None:
    with _callback_client(settings) as (client, container, context):
        workspace = _use_workspace(container, tmp_path / "workspace")
        (workspace / "huge.png").write_bytes(b"x" * (10 * 1024 * 1024 + 1))
        container.assistant = AttachingAssistant(path="huge.png", reply="")  # type: ignore[assignment]
        assert _post_callback(client).status_code == 200
        bot = client.get(f"{PREFIX}/bots/{context['bot']['id']}").json()["data"]
        pending = _wait_pending_decision(client, bot["chat_task_id"])

        allowed = client.post(
            f"{PREFIX}/tasks/{bot['chat_task_id']}/decisions/choose",
            json={"decision_id": pending["id"], "action": "choose", "option": "允许"},
        )
        assert allowed.status_code == 200, allowed.text
        # 超限：绝不调用平台上传，草稿落 failed 且错误是中文。
        assert context["sender"].media == []
        assert context["sender"].messages == []
        failed = client.get(f"{PREFIX}/outbound-drafts/{pending['meta']['draft_id']}").json()[
            "data"
        ]
        assert failed["status"] == "failed"
        assert "超过企业微信出站上限 10MB" in failed["error"]
