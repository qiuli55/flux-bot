"""正式飞书频道（Bot 入站 + 受审批保护的出站）。

与 `flux.core.supervisor.feishu` 的 Supervisor 入站保持独立：这里面向的是正式
Bot 的对话轮次，出站也必须经过审批闸门，而不是直接把报告发出去。

入站：

- 支持飞书 Encrypt Key 加密回调与签名校验（见 `feishu_crypto`）；
- 校验 verification token，未配置时 fail-closed；
- 只把 `im.message.receive_v1` 的文本消息转成 Bot turn，其余事件返回 200 让飞书
  不要重推；
- `message_id` 作为 `client_turn_id`，重推不会产生第二轮。

出站红线：**任何发给外部联系人的文本都不得自动发送**。turn 完成后只把待发送
正文落进 `outbound_drafts`（pending），并调用 `raise_approval` 在对话里挂一张
「允许 / 拒绝」卡。只有用户选择「允许」后，`on_approval_resolved` 才会原子认领
草稿并真正调用飞书发送接口；拒绝、超时或进程重启时草稿仍留在库里，绝不自动发送。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from flux.core.channels.feishu_crypto import FeishuCryptoError, decrypt_payload, verify_signature
from flux.core.event.bus import Events
from flux.core.task_engine.approvals import APPROVAL_ALLOW, raise_approval
from flux.errors import AIOSError, NotFoundError
from flux.logging import get_logger

if TYPE_CHECKING:
    from flux.container import Container

logger = get_logger(__name__)

__all__ = [
    "FEISHU_OUTBOUND_KIND",
    "FeishuBotChannel",
    "FeishuBotSender",
    "FeishuCallbackResult",
    "MESSAGE_EVENT_TYPE",
]

#: 飞书 v2 事件回调的消息事件类型
MESSAGE_EVENT_TYPE = "im.message.receive_v1"
#: 出站审批使用的决策 kind（ASCII，便于检索与断言）
FEISHU_OUTBOUND_KIND = "feishu_outbound"
#: 平台单条文本上限的安全值（与 Supervisor 通道口径一致）
MAX_TEXT_CHARS = 4000
#: 审批卡里展示的草稿摘录上限
_DETAIL_TEXT_CHARS = 1500


@dataclass(frozen=True)
class FeishuCallbackResult:
    """回调处理结果：HTTP 状态码 + 返回体（飞书约定原始 JSON，不套统一信封）。"""

    status_code: int
    payload: dict[str, Any] = field(default_factory=dict)


class FeishuBotSender:
    """飞书自建应用出站：tenant_access_token + 发文本消息。

    与 Supervisor 的 `FeishuChannel` 实现相似但刻意分开，避免两条产品线互相牵连。
    """

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        api_base: str = "https://open.feishu.cn",
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._api_base = api_base.rstrip("/") or "https://open.feishu.cn"
        self._timeout = max(1.0, float(timeout_seconds))
        self._client = client
        self._owns_client = client is None
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._lock = asyncio.Lock()

    async def send_text(self, chat_id: str, text: str) -> None:
        token = await self._tenant_token()
        response = await self._request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": (text or "")[:MAX_TEXT_CHARS]}, ensure_ascii=False),
            },
        )
        _raise_for_feishu(response, "发送飞书消息")

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    async def _tenant_token(self) -> str:
        """缓存 tenant_access_token，提前 60 秒过期，避免边界上刚好失效。"""
        if self._token is not None and time.monotonic() < self._token_expires_at:
            return self._token
        async with self._lock:
            if self._token is not None and time.monotonic() < self._token_expires_at:
                return self._token
            response = await self._request(
                "POST",
                "/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
            data = _payload(response, "获取飞书 tenant_access_token")
            token = str(data.get("tenant_access_token") or "")
            if not token:
                raise RuntimeError("飞书 tenant_access_token 响应缺少令牌")
            expire = int(data.get("expire") or 7200)
            self._token = token
            self._token_expires_at = time.monotonic() + max(60, expire - 60)
            return token

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
            self._client = client
            self._owns_client = True
        return await client.request(method, f"{self._api_base}{path}", **kwargs)


def _payload(response: httpx.Response, label: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{label}失败：响应不是 JSON（HTTP {response.status_code}）") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{label}失败：响应结构异常（HTTP {response.status_code}）")
    code = data.get("code")
    if response.status_code >= 400 or code not in (0, None):
        message = data.get("msg") or data.get("message") or ""
        raise RuntimeError(f"{label}失败：HTTP {response.status_code} code={code} {message}"[:500])
    return data


def _raise_for_feishu(response: httpx.Response, label: str) -> None:
    _payload(response, label)


class FeishuBotChannel:
    """飞书正式频道：加密回调验签、入站建 turn、出站走审批。"""

    def __init__(
        self,
        container: Container,
        *,
        app_id: str | None = None,
        app_secret: str | None = None,
        verification_token: str | None = None,
        encrypt_key: str | None = None,
        api_base: str = "https://open.feishu.cn",
        bot_id: str | None = None,
        sender: Any | None = None,
        turn_wait_seconds: float = 900.0,
    ) -> None:
        self._container = container
        self._verification_token = (verification_token or "").strip() or None
        self._encrypt_key = (encrypt_key or "").strip() or None
        self._bot_id = _parse_uuid(bot_id)
        if (bot_id or "").strip() and self._bot_id is None:
            logger.warning("FLUX_FEISHU_BOT_ID 不是合法 UUID，飞书消息事件将被拒绝")
        if sender is not None:
            self._sender = sender
        elif app_id and app_secret:
            self._sender = FeishuBotSender(
                app_id=app_id,
                app_secret=app_secret,
                api_base=api_base,
            )
        else:
            self._sender = None
        self._background: set[asyncio.Task[Any]] = set()
        #: 同一对话任务同一时刻只挂一张出站审批卡，避免连续两轮各挂一张后
        #: `pending_decision()` 只认最后一张，前一张永远无法被选中。
        self._approval_lock = asyncio.Lock()
        self._turn_wait_seconds = max(1.0, float(turn_wait_seconds))

    async def handle(
        self, body: bytes, headers: Mapping[str, str] | None = None
    ) -> FeishuCallbackResult:
        """处理一次飞书回调，返回平台约定的状态码与 JSON 返回体。"""
        raw = body or b""
        request_headers = headers or {}

        # 配了 Encrypt Key 就等于启用了签名校验：缺头、错签名一律拒绝。
        if self._encrypt_key is not None and not self._signature_ok(request_headers, raw):
            logger.warning("飞书回调签名校验失败，已拒绝")
            return FeishuCallbackResult(403, {"error": "飞书回调签名校验失败"})

        try:
            envelope = json.loads(raw or b"{}")
        except (UnicodeDecodeError, ValueError):
            return FeishuCallbackResult(400, {"error": "请求体不是合法 JSON"})
        if not isinstance(envelope, dict):
            return FeishuCallbackResult(400, {"error": "请求体结构异常"})

        if "encrypt" in envelope:
            if self._encrypt_key is None:
                return FeishuCallbackResult(
                    400,
                    {
                        "error": (
                            "收到加密回调，但未配置 FLUX_FEISHU_ENCRYPT_KEY，"
                            "无法解密；请补齐配置或关闭飞书加密"
                        )
                    },
                )
            encrypted = envelope.get("encrypt")
            if not isinstance(encrypted, str) or not encrypted:
                return FeishuCallbackResult(400, {"error": "加密回调缺少有效的 encrypt 字段"})
            try:
                payload = decrypt_payload(self._encrypt_key, encrypted)
            except FeishuCryptoError as exc:
                logger.warning("飞书加密回调解密失败：%s", exc)
                return FeishuCallbackResult(400, {"error": str(exc)})
        else:
            payload = envelope

        header = payload.get("header")
        header = header if isinstance(header, dict) else {}

        failed = self._verify_token(header.get("token") or payload.get("token"))
        if failed is not None:
            return failed

        challenge = payload.get("challenge")
        if payload.get("type") == "url_verification" or challenge is not None:
            if not isinstance(challenge, str) or not challenge:
                return FeishuCallbackResult(400, {"error": "url_verification 缺少 challenge"})
            return FeishuCallbackResult(200, {"challenge": challenge})

        # 以下都是非 challenge 事件：必须知道这条频道归属哪个 Bot，否则 fail-closed。
        bot_id = self._bot_id
        if bot_id is None:
            return FeishuCallbackResult(
                400,
                {"error": ("未配置合法的 FLUX_FEISHU_BOT_ID，拒绝处理飞书消息事件")},
            )

        event_type = str(header.get("event_type") or "")
        if event_type != MESSAGE_EVENT_TYPE:
            return FeishuCallbackResult(
                200, {"ignored": True, "reason": f"不支持的事件类型：{event_type or 'unknown'}"}
            )

        parsed = _parse_message(payload)
        if isinstance(parsed, FeishuCallbackResult):
            return parsed
        message_id, chat_id, text = parsed

        try:
            bot = await self._container.bots.get(bot_id)
        except NotFoundError:
            logger.warning("飞书回调指向的 Bot 不存在 bot=%s", bot_id)
            return FeishuCallbackResult(404, {"error": "FLUX_FEISHU_BOT_ID 指向的 Bot 不存在"})

        try:
            turn, created = await self._container.bot_conversation.create_turn(
                bot, content=text, client_turn_id=message_id
            )
        except AIOSError as exc:
            logger.warning(
                "飞书入站建 turn 失败 bot=%s message=%s code=%s",
                bot_id,
                message_id,
                exc.code,
            )
            activity = getattr(self._container, "channel_activity", None)
            if activity is not None:
                activity.record_error("feishu", exc.code, exc.message)
            return FeishuCallbackResult(
                exc.http_status,
                {"error": exc.message, "code": exc.code},
            )

        activity = getattr(self._container, "channel_activity", None)
        if activity is not None:
            activity.record_receive("feishu")
        if created:
            self._schedule_tracking(bot=bot, turn=turn, chat_id=chat_id, message_id=message_id)
        logger.info(
            "feishu.inbound bot=%s message=%s turn=%s created=%s",
            bot_id,
            message_id,
            turn.id,
            created,
        )
        return FeishuCallbackResult(200, {"created": created, "turn_id": str(turn.id)})

    async def on_approval_resolved(self, event: str, payload: dict[str, Any]) -> None:
        """审批结果监听：只有「允许」才发送，且同一条草稿最多发送一次。

        草稿状态迁移全部交给频道无关的 `OutboundDraftService`：认领失败说明
        草稿已被处理（重复放行 / 已拒绝 / 已取消），这里直接跳过，绝不重复发送。
        """
        if event != Events.APPROVAL_RESOLVED:
            return
        if payload.get("kind") != FEISHU_OUTBOUND_KIND:
            return

        meta = payload.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        raw_draft_id = str(meta.get("draft_id") or "").strip()
        if not raw_draft_id:
            logger.warning("飞书出站审批缺少 draft_id，已跳过")
            return
        try:
            draft_id = uuid.UUID(raw_draft_id)
        except ValueError:
            logger.warning("飞书出站审批的 draft_id 非法，已跳过 draft=%s", raw_draft_id)
            return

        service = getattr(self._container, "outbound_drafts", None)
        if service is None:  # pragma: no cover - 装配错误，按 fail-closed 跳过
            logger.error("出站草稿服务未装配，审批结果已忽略 draft=%s", raw_draft_id)
            return

        if payload.get("result") != "allowed" or payload.get("chosen") != APPROVAL_ALLOW:
            await service.mark_denied(draft_id)
            logger.info(
                "feishu.outbound.skip decision=%s result=%s draft=%s",
                payload.get("decision_id"),
                payload.get("result"),
                raw_draft_id,
            )
            return

        draft = await service.claim(draft_id)
        if draft is None:
            logger.info("飞书出站草稿不存在或已被处理 draft=%s", raw_draft_id)
            return
        dest = draft.dest if isinstance(draft.dest, dict) else {}
        chat_id = str(dest.get("chat_id") or "").strip()
        if draft.channel != "feishu" or not chat_id:
            await service.mark_failed(draft.id, "channel_or_dest_mismatch")
            logger.error(
                "飞书出站草稿的频道或目标不匹配 draft=%s channel=%s dest_keys=%s",
                draft.id,
                draft.channel,
                sorted(dest),
            )
            return
        sender = self._sender
        if sender is None:
            # 不是「取走即丢」：标 failed 并保留正文，配置好凭据后仍可在草稿箱看到。
            await service.mark_failed(draft.id, "sender_not_configured")
            logger.error(
                "未配置飞书应用凭据，审批通过的草稿无法发送 draft=%s chat=%s",
                draft.id,
                chat_id,
            )
            return
        try:
            await sender.send_text(chat_id, draft.body)
        except Exception as exc:  # noqa: BLE001 - 发送失败只记录，不能拖垮决策接口
            await service.mark_failed(draft.id, exc)
            logger.exception(
                "飞书出站发送失败 draft=%s chat=%s hash=%s chars=%s",
                draft.id,
                chat_id,
                draft.body_hash,
                len(draft.body or ""),
            )
            return
        await service.mark_sent(draft.id)
        logger.info(
            "飞书出站已发送 draft=%s chat=%s hash=%s chars=%s",
            draft.id,
            chat_id,
            draft.body_hash,
            len(draft.body or ""),
        )

    async def aclose(self) -> None:
        tasks = [task for task in self._background if not task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._background.clear()
        close = getattr(self._sender, "aclose", None)
        if close is not None:
            await close()

    # --- 内部 ---

    def _signature_ok(self, headers: Mapping[str, str], body: bytes) -> bool:
        return verify_signature(
            encrypt_key=self._encrypt_key or "",
            timestamp=_header(headers, "X-Lark-Request-Timestamp"),
            nonce=_header(headers, "X-Lark-Request-Nonce"),
            body=body,
            signature=_header(headers, "X-Lark-Signature"),
        )

    def _verify_token(self, token: object) -> FeishuCallbackResult | None:
        if self._verification_token is None:
            return FeishuCallbackResult(
                400,
                {
                    "error": (
                        "未配置飞书 verification token，拒绝处理回调"
                        "（请设置 FLUX_FEISHU_VERIFICATION_TOKEN）"
                    )
                },
            )
        if not isinstance(token, str) or not hmac.compare_digest(token, self._verification_token):
            logger.warning("飞书回调 verification token 不匹配，已拒绝")
            return FeishuCallbackResult(403, {"error": "verification token 不匹配"})
        return None

    def _schedule_tracking(self, *, bot: Any, turn: Any, chat_id: str, message_id: str) -> None:
        task = asyncio.create_task(
            self._track_turn(bot=bot, turn=turn, chat_id=chat_id, message_id=message_id),
            name="flux-feishu-turn",
        )
        self._background.add(task)

        def _cleanup(done: asyncio.Task[Any]) -> None:
            self._background.discard(done)

        task.add_done_callback(_cleanup)

    async def _track_turn(self, *, bot: Any, turn: Any, chat_id: str, message_id: str) -> None:
        """等这一轮结束；有回复就生成草稿并挂审批，绝不直接发送。"""
        try:
            final = await self._container.bot_conversation.wait_for_turn(
                bot.id, turn.id, wait_seconds=self._turn_wait_seconds
            )
            if final.status != "completed" or final.reply_message_id is None:
                logger.info("飞书 turn 未产生可发送回复 turn=%s status=%s", turn.id, final.status)
                return
            async with self._approval_lock:
                task = await self._container.task_repo.get(final.task_id)
                if task.pending_decision() is not None:
                    logger.info(
                        "对话里已有待决策项，跳过新增飞书出站审批 turn=%s task=%s",
                        turn.id,
                        final.task_id,
                    )
                    return
                reply = await self._container.task_repo.get_message(
                    final.task_id, final.reply_message_id
                )
                text = (reply.content or "").strip() if reply is not None else ""
                if not text:
                    logger.info("飞书 turn 回复为空，不生成出站草稿 turn=%s", turn.id)
                    return

                service = self._container.outbound_drafts
                # 先生成审批 id 并写进草稿，保证审批卡出现的瞬间两边已经能互相定位。
                decision_id = uuid.uuid4().hex
                draft = await service.create(
                    channel="feishu",
                    body=text,
                    dest={"chat_id": chat_id, "message_id": message_id},
                    bot_id=bot.id,
                    task_id=final.task_id,
                    turn_id=final.id,
                    decision_id=decision_id,
                )
                await raise_approval(
                    self._container,
                    task_id=final.task_id,
                    run_id=None,
                    kind=FEISHU_OUTBOUND_KIND,
                    decision_id=decision_id,
                    title="飞书回复等待你确认",
                    detail=(
                        f"准备发送到飞书会话 {chat_id}（消息 {message_id}）：\n\n{_excerpt(text)}"
                    ),
                    meta={
                        "channel": "feishu",
                        "draft_id": str(draft.id),
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "bot_id": str(bot.id),
                        "turn_id": str(final.id),
                        "text_hash": draft.body_hash,
                        "chars": len(text),
                    },
                )
                logger.info(
                    "飞书出站审批已创建 draft=%s turn=%s task=%s hash=%s chars=%s",
                    draft.id,
                    final.id,
                    final.task_id,
                    draft.body_hash,
                    len(text),
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 后台跟踪失败只记录 id，不影响回调
            logger.exception("飞书 turn 跟踪失败 turn=%s", getattr(turn, "id", "?"))


def _header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return value
    return None


def _parse_uuid(value: str | None) -> uuid.UUID | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _excerpt(text: str) -> str:
    if len(text) <= _DETAIL_TEXT_CHARS:
        return text
    return text[:_DETAIL_TEXT_CHARS] + "…"


def _parse_message(
    payload: dict[str, Any],
) -> tuple[str, str, str] | FeishuCallbackResult:
    event = payload.get("event")
    event = event if isinstance(event, dict) else {}
    message = event.get("message")
    message = message if isinstance(message, dict) else {}

    message_type = str(message.get("message_type") or "")
    if message_type != "text":
        return FeishuCallbackResult(
            200, {"ignored": True, "reason": f"不支持的消息类型：{message_type or 'unknown'}"}
        )

    message_id = str(message.get("message_id") or "").strip()
    chat_id = str(message.get("chat_id") or "").strip()
    if not message_id or not chat_id:
        return FeishuCallbackResult(400, {"error": "回调缺少 message_id / chat_id"})

    text = _text_content(message.get("content"))
    if not text:
        return FeishuCallbackResult(200, {"ignored": True, "reason": "空文本消息"})
    return message_id, chat_id, text


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
