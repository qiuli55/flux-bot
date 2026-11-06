"""微信入站桥：接收 OpenClaw 插件转来的微信消息，返回 Flux 的回复。

传输层（登录、长连接、收发微信）在 OpenClaw + 微信 ClawBot 里；Flux 只做大脑。
所以这里没有公网 webhook、没有微信协议实现，只有一个本机 HTTP 约定：

    POST /api/v1/supervisor/channels/wechat
    Authorization: Bearer <FLUX_SUPERVISOR_WECHAT_BRIDGE_TOKEN>
    {"message_id": "...", "session_key": "...", "sender_id": "wxid_...", "text": "..."}

    200 {"handled": true, "reply": {"text": "..."}}
    200 {"handled": true, "duplicate": true}      # 重试，回复已在处理/已回放
    4xx / 5xx                                      # 插件不认领，交回 OpenClaw 原生模型

全部 fail-closed：未配置令牌拒绝一切回调，白名单为空拒绝所有人。
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from flux.core.supervisor.wechat_assistant import WechatAssistant
from flux.logging import get_logger

logger = get_logger(__name__)

__all__ = ["WechatBridge", "WechatResult"]


@dataclass(frozen=True)
class WechatResult:
    status_code: int
    payload: dict[str, Any] = field(default_factory=dict)


class WechatBridge:
    def __init__(
        self,
        assistant: WechatAssistant,
        *,
        bridge_token: str | None,
        allow_from: Collection[str],
        source_key: str = "wechat",
    ) -> None:
        self.assistant = assistant
        self._token = (bridge_token or "").strip()
        self._allow_from = {item for item in allow_from if item}
        self._source_key = source_key

    async def handle(self, body: bytes, authorization: str | None) -> WechatResult:
        if not self._token:
            return WechatResult(400, {"handled": False, "error": "微信入站令牌未配置"})
        if not _token_matches(self._token, authorization):
            return WechatResult(403, {"handled": False, "error": "微信入站令牌无效"})

        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return WechatResult(400, {"handled": False, "error": "请求体不是合法 JSON"})
        if not isinstance(data, dict):
            return WechatResult(400, {"handled": False, "error": "请求体结构异常"})

        message_id = _text_field(data, "message_id", "messageId")
        session_key = _text_field(data, "session_key", "sessionKey")
        sender_id = _text_field(data, "sender_id", "senderId")
        text = _text_field(data, "text", "cleanedBody") or _text_field(data, "body")
        if not message_id or not session_key or not sender_id or not text:
            return WechatResult(
                400,
                {"handled": False, "error": "缺少 message_id / session_key / sender_id / text"},
            )

        if not self._allow_from:
            return WechatResult(403, {"handled": False, "error": "微信白名单未配置"})
        if sender_id not in self._allow_from:
            logger.warning("拒绝非白名单微信消息 sender_id=%s", sender_id)
            return WechatResult(403, {"handled": False, "error": "发送者不在白名单"})

        try:
            reply = await self.assistant.respond(
                session_key=session_key,
                sender_id=sender_id,
                message_id=message_id,
                text=text,
            )
        except Exception:
            # 不把内部异常细节回给插件；插件据此不认领，交回 OpenClaw 原生模型
            logger.exception(
                "微信助理处理失败 session_key=%s message_id=%s", session_key, message_id
            )
            return WechatResult(503, {"handled": False, "error": "助理暂时不可用"})

        payload: dict[str, Any] = {"handled": True}
        if reply.duplicate:
            payload["duplicate"] = True
        if reply.text:
            payload["reply"] = {"text": reply.text}
        if reply.task_id is not None:
            payload["task_id"] = str(reply.task_id)
        if reply.session_id is not None:
            payload["session_id"] = str(reply.session_id)
        return WechatResult(200, payload)


def _token_matches(expected: str, authorization: str | None) -> bool:
    raw = (authorization or "").strip()
    if not raw:
        return False
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    return bool(raw) and hmac.compare_digest(expected, raw)


def _text_field(data: dict[str, Any], *names: str) -> str:
    for name in names:
        value = data.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
