"""飞书事件回调：验签 → 解析 → 幂等入库。

红线：外部内容只是**数据**，不是指令。这里只把消息文本原样落成 trigger，
不做任何解释、不拼接进系统提示；后续判断层按只读事实处理。

安全策略全部 fail-closed：

- 未配置 verification token 时拒绝一切回调（不给"忘了配就是开放"的机会）；
- token 不匹配返回 403；
- 飞书开启了 Encrypt Key 的加密回调直接拒绝——本仓库不引入解密依赖，
  需要在飞书后台关闭加密（错误信息会写明）。

只处理 `im.message.receive_v1` 的文本消息；其它事件返回 200 忽略，
避免飞书因为非 2xx 反复重推。
"""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass, field
from typing import Any

from flux.core.supervisor.service import SupervisorService
from flux.logging import get_logger

logger = get_logger(__name__)

__all__ = ["FeishuWebhook", "WebhookResult"]

#: 飞书 v2 事件回调的消息事件类型
MESSAGE_EVENT_TYPE = "im.message.receive_v1"


@dataclass(frozen=True)
class WebhookResult:
    """回调处理结果：HTTP 状态码 + 返回体。"""

    status_code: int
    payload: dict[str, Any] = field(default_factory=dict)


class FeishuWebhook:
    def __init__(
        self,
        service: SupervisorService,
        *,
        source_key: str = "feishu",
        verification_token: str | None = None,
        encrypt_key: str | None = None,
        actor_platform: str = "feishu",
    ) -> None:
        self._service = service
        self._source_key = source_key
        self._verification_token = (verification_token or "").strip() or None
        self._encrypt_key = (encrypt_key or "").strip() or None
        self._actor_platform = actor_platform

    async def handle(self, body: bytes) -> WebhookResult:
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return WebhookResult(400, {"error": "请求体不是合法 JSON"})
        if not isinstance(payload, dict):
            return WebhookResult(400, {"error": "请求体结构异常"})

        if "encrypt" in payload:
            # 加密回调需要 AES 解密依赖；本仓库不引入，明确要求关闭加密。
            return WebhookResult(
                400,
                {
                    "error": (
                        "暂不支持飞书加密回调（encrypt），请在飞书开放平台关闭 Encrypt Key 后重试"
                    ),
                    "encrypt_key_configured": self._encrypt_key is not None,
                },
            )

        header = payload.get("header")
        header = header if isinstance(header, dict) else {}
        token = header.get("token") or payload.get("token")
        verified = self._verify_token(token)
        if verified is not None:
            return verified

        challenge = payload.get("challenge")
        if payload.get("type") == "url_verification" or challenge is not None:
            if not isinstance(challenge, str) or not challenge:
                return WebhookResult(400, {"error": "url_verification 缺少 challenge"})
            return WebhookResult(200, {"challenge": challenge})

        event_type = str(header.get("event_type") or "")
        if event_type != MESSAGE_EVENT_TYPE:
            return WebhookResult(
                200, {"ignored": True, "reason": f"不支持的事件类型：{event_type or 'unknown'}"}
            )

        parsed = _parse_message(payload, header)
        if isinstance(parsed, WebhookResult):
            return parsed
        external_id, actor_id, thread_key, content = parsed

        data = await self._service.ingest_event(
            source_key=self._source_key,
            external_id=external_id,
            event_type="message",
            actor_id=actor_id,
            actor_platform=self._actor_platform,
            thread_key=thread_key,
            content=content,
            raw=_sanitized_raw(payload, header),
        )
        return WebhookResult(
            200,
            {
                "created": data["created"],
                "trigger_id": data["trigger"]["id"],
            },
        )

    def _verify_token(self, token: object) -> WebhookResult | None:
        if self._verification_token is None:
            return WebhookResult(
                400,
                {
                    "error": (
                        "未配置飞书 verification token，拒绝处理回调"
                        "（请设置 FLUX_SUPERVISOR_FEISHU_VERIFICATION_TOKEN）"
                    )
                },
            )
        if not isinstance(token, str) or not hmac.compare_digest(token, self._verification_token):
            logger.warning("飞书回调 verification token 不匹配，已拒绝")
            return WebhookResult(403, {"error": "verification token 不匹配"})
        return None


def _parse_message(
    payload: dict[str, Any], header: dict[str, Any]
) -> tuple[str, str, str, str] | WebhookResult:
    event = payload.get("event")
    event = event if isinstance(event, dict) else {}
    message = event.get("message")
    message = message if isinstance(message, dict) else {}
    sender = event.get("sender")
    sender = sender if isinstance(sender, dict) else {}
    sender_id = sender.get("sender_id")
    sender_id = sender_id if isinstance(sender_id, dict) else {}

    external_id = str(header.get("event_id") or message.get("message_id") or "").strip()
    if not external_id:
        return WebhookResult(400, {"error": "回调缺少 event_id / message_id"})

    if str(message.get("message_type") or "") != "text":
        return WebhookResult(
            200,
            {"ignored": True, "reason": f"不支持的消息类型：{message.get('message_type')}"},
        )

    content = _text_content(message.get("content"))
    if not content:
        return WebhookResult(200, {"ignored": True, "reason": "空文本消息"})

    actor_id = str(sender_id.get("open_id") or "").strip()
    thread_key = str(message.get("chat_id") or "").strip()
    return external_id, actor_id, thread_key, content


def _text_content(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    text = data.get("text")
    return text.strip() if isinstance(text, str) else ""


def _sanitized_raw(payload: dict[str, Any], header: dict[str, Any]) -> dict[str, Any]:
    """审计用原始请求体：去掉 token，避免把凭据写进数据库。"""
    safe_header = {key: value for key, value in header.items() if key != "token"}
    return {"header": safe_header, "event": payload.get("event") or {}}
