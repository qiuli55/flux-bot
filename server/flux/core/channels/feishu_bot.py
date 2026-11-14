"""正式飞书频道（Bot 入站 + 受审批保护的出站）。

与 `flux.core.supervisor.feishu` 的 Supervisor 入站保持独立：这里面向的是正式
Bot 的对话轮次，出站也必须经过审批闸门，而不是直接把报告发出去。

入站：

- 支持飞书 Encrypt Key 加密回调与签名校验（见 `feishu_crypto`）；
- 校验 verification token，未配置时 fail-closed；
- 只把 `im.message.receive_v1` 的文本消息转成 Bot turn，其余事件返回 200 让飞书
  不要重推；
- `message_id` 作为 `client_turn_id`，重推不会产生第二轮。

出站默认**不需要审批**：turn 完成后把正文落进 `outbound_drafts`，没有命中该用户的
出站规则时立即原子认领并走官方发送接口（草稿标 sent 作为审计记录）。只有命中规则
时才调用 `raise_approval` 挂「允许 / 拒绝」卡；拒绝、超时或进程重启时草稿留在库里，
绝不发送。规则按 Flux 云用户隔离，A 的规则永不用于 B 的出站消息。
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
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from flux.core.approval_rules.evaluator import (
    ACTION_OUTBOUND_MESSAGE,
    ApprovalAction,
    ApprovalRuleMatch,
    evaluate_approval,
    rule_meta,
)
from flux.core.channels.binding_flow import resolve_media_inbound, resolve_text_inbound
from flux.core.channels.feishu_crypto import FeishuCryptoError, decrypt_payload, verify_signature
from flux.core.channels.inbound_media import (
    DownloadedMedia,
    Transcriber,
    media_from_response,
    pick_media_name,
    safe_error,
    save_media,
    transcribe_audio,
)
from flux.core.channels.outbound_media import (
    attachment_meta,
    attachment_record,
    attachment_records,
    render_outbound_detail,
    resolve_attachment,
    validate_channel_attachment,
)
from flux.core.event.bus import Events
from flux.core.task_engine.approvals import (
    APPROVAL_ALLOW,
    outbound_approval_options,
    raise_approval,
)
from flux.core.user_scope import normalize_cloud_user_id
from flux.errors import AIOSError, NotFoundError, ValidationError
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
#: 审批卡文案里的频道名
CHANNEL_LABEL = "飞书"
#: 平台单条文本上限的安全值（与 Supervisor 通道口径一致）
MAX_TEXT_CHARS = 4000
#: 提升排队草稿时最多重试几次（并发下别的处理器可能先认领同一张卡）
_MAX_QUEUE_PROMOTIONS = 5


@dataclass(frozen=True)
class FeishuCallbackResult:
    """回调处理结果：HTTP 状态码 + 返回体（飞书约定原始 JSON，不套统一信封）。"""

    status_code: int
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _InboundMessage:
    """解析后的入站消息：文本或一条待下载的媒体。"""

    message_id: str
    chat_id: str
    #: 发送者的稳定 id（open_id → user_id → union_id）；空串 = 解析失败，按未绑定处理。
    sender_id: str = ""
    kind: str = ""  # text / image / file / audio
    text: str = ""
    resource_key: str = ""
    file_name: str = ""
    recognition: str = ""


#: 媒体类型的中文名，用于缺凭据时给用户一句能看懂的解释。
_KIND_LABELS = {"image": "图片", "file": "文件", "audio": "语音"}


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

    async def upload_image(self, *, path: str, name: str, mime: str) -> str:
        """上传图片素材，返回 image_key（官方 /open-apis/im/v1/images，上限 10MB）。"""
        token = await self._tenant_token()
        response = await self._request(
            "POST",
            "/open-apis/im/v1/images",
            headers={"Authorization": f"Bearer {token}"},
            data={"image_type": "message"},
            files={"image": (name, _read_media(path, "飞书"), mime or "application/octet-stream")},
        )
        data = _payload(response, "上传飞书图片")
        image_key = str((data.get("data") or {}).get("image_key") or "")
        if not image_key:
            raise RuntimeError("上传飞书图片失败：响应缺少 image_key")
        return image_key

    async def upload_file(self, *, path: str, name: str, mime: str) -> str:
        """上传文件素材，返回 file_key（官方 /open-apis/im/v1/files，上限 30MB）。"""
        token = await self._tenant_token()
        response = await self._request(
            "POST",
            "/open-apis/im/v1/files",
            headers={"Authorization": f"Bearer {token}"},
            data={"file_type": "stream", "file_name": name},
            files={"file": (name, _read_media(path, "飞书"), mime or "application/octet-stream")},
        )
        data = _payload(response, "上传飞书文件")
        file_key = str((data.get("data") or {}).get("file_key") or "")
        if not file_key:
            raise RuntimeError("上传飞书文件失败：响应缺少 file_key")
        return file_key

    async def send_image(self, chat_id: str, image_key: str) -> None:
        """发送图片消息（msg_type=image，content 只放 image_key）。"""
        await self._send_message(chat_id, "image", {"image_key": image_key}, label="发送飞书图片")

    async def send_file(self, chat_id: str, file_key: str) -> None:
        """发送文件消息（msg_type=file，content 只放 file_key）。"""
        await self._send_message(chat_id, "file", {"file_key": file_key}, label="发送飞书文件")

    async def send_media(self, chat_id: str, *, path: str, name: str, mime: str, kind: str) -> None:
        """按类型走官方上传 + 发送；调用方必须先按平台上限校验。"""
        if kind == "image":
            image_key = await self.upload_image(path=path, name=name, mime=mime)
            await self.send_image(chat_id, image_key)
            return
        file_key = await self.upload_file(path=path, name=name, mime=mime)
        await self.send_file(chat_id, file_key)

    async def _send_message(
        self, chat_id: str, msg_type: str, content: dict[str, Any], *, label: str
    ) -> None:
        token = await self._tenant_token()
        response = await self._request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": chat_id,
                "msg_type": msg_type,
                "content": json.dumps(content, ensure_ascii=False),
            },
        )
        _raise_for_feishu(response, label)

    async def download_media(
        self,
        *,
        message_id: str,
        file_key: str,
        resource_type: str = "file",
    ) -> DownloadedMedia:
        """按官方「获取消息中的资源文件」接口下载图片 / 文件 / 语音。

        官方 type 只接受 image / file；语音必须用 type=file（type=audio 会 400）。
        """
        token = await self._tenant_token()
        path = (
            f"/open-apis/im/v1/messages/{quote(message_id, safe='')}"
            f"/resources/{quote(file_key, safe='')}"
        )
        response = await self._send_stream(
            "GET",
            f"{self._api_base}{path}",
            params={"type": resource_type},
            headers={"Authorization": f"Bearer {token}"},
        )
        return await media_from_response(response, label="下载飞书媒体")

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
        return await self._ensure_client().request(method, f"{self._api_base}{path}", **kwargs)

    async def _send_stream(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """媒体下载不整包读进内存：拿到流式响应后交给 save_upload 边读边落盘。"""
        client = self._ensure_client()
        request = client.build_request(method, url, **kwargs)
        return await client.send(request, stream=True, follow_redirects=True)

    def _ensure_client(self) -> httpx.AsyncClient:
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
            self._client = client
            self._owns_client = True
        return client


def _read_media(path: str, channel_label: str) -> bytes:
    """读取出站附件字节；错误信息只带文件名，不回显宿主目录。"""
    target = Path(path)
    try:
        return target.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"读取出站附件失败（{channel_label}）：{target.name}") from exc


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
        transcriber: Transcriber | None = None,
        turn_wait_seconds: float = 900.0,
    ) -> None:
        self._container = container
        self._transcriber = transcriber
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
        #: 容器装配时三个频道共用一把锁；单测里的简易容器则退回频道私有锁。
        shared_lock = getattr(container, "outbound_approval_lock", None)
        self._approval_lock = (
            shared_lock if isinstance(shared_lock, asyncio.Lock) else asyncio.Lock()
        )
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
        message_id = parsed.message_id
        chat_id = parsed.chat_id

        try:
            bot = await self._container.bots.get(bot_id)
        except NotFoundError:
            logger.warning("飞书回调指向的 Bot 不存在 bot=%s", bot_id)
            return FeishuCallbackResult(404, {"error": "FLUX_FEISHU_BOT_ID 指向的 Bot 不存在"})

        async def _system_reply(text: str) -> None:
            await self._send_system_message(chat_id, text)

        if parsed.kind == "text":
            cloud_user_id = await resolve_text_inbound(
                self._container,
                channel="feishu",
                external_user_id=parsed.sender_id,
                text=parsed.text,
                reply=_system_reply,
            )
            if cloud_user_id is None:
                # 绑定码 / 未绑定提示 / 缺发送者：都只回系统消息，不建 turn。
                return FeishuCallbackResult(200, {"created": False, "bound": False})
            files: list[Any] = []
            content = parsed.text
        else:
            cloud_user_id = await resolve_media_inbound(
                self._container,
                channel="feishu",
                external_user_id=parsed.sender_id,
                reply=_system_reply,
            )
            if cloud_user_id is None:
                # 未绑定：不下载媒体、不落盘、不建 turn。
                return FeishuCallbackResult(200, {"created": False, "bound": False})
            # 重推：turn 已存在就直接确认，绝不再次下载媒体。
            existing = await self._existing_turn(bot, message_id)
            if existing is not None:
                logger.info(
                    "feishu.inbound.replay bot=%s message=%s turn=%s",
                    bot_id,
                    message_id,
                    existing.id,
                )
                return FeishuCallbackResult(200, {"created": False, "turn_id": str(existing.id)})
            try:
                files, content = await self._load_media(parsed, cloud_user_id=cloud_user_id)
            except ValidationError as exc:
                # 超限 / 空文件是永久问题：确认掉，别让平台无限重推。
                logger.warning(
                    "飞书入站媒体被拒绝 message=%s kind=%s reason=%s",
                    message_id,
                    parsed.kind,
                    exc.message,
                )
                return FeishuCallbackResult(200, {"ignored": True, "reason": exc.message})
            except Exception as exc:  # noqa: BLE001 - 下载异常不能拖垮进程
                logger.warning(
                    "飞书入站媒体下载失败 message=%s kind=%s error=%s",
                    message_id,
                    parsed.kind,
                    safe_error(exc),
                )
                return FeishuCallbackResult(502, {"error": "飞书媒体下载失败，请稍后重试"})

        try:
            turn, created = await self._container.bot_conversation.create_turn(
                bot,
                content=content,
                files=files,
                client_turn_id=message_id,
                cloud_user_id=cloud_user_id,
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
        """审批结果监听：只有「允许」才发送，同一条草稿最多发送一次。

        草稿状态迁移全部交给频道无关的 `OutboundDraftService`：认领失败说明
        草稿已被处理（重复放行 / 已拒绝 / 已取消），这里直接跳过，绝不重复发送。

        无论这次解决的是不是飞书出站卡（也可能是命令审批 / 别的频道），只要有
        决策被解决、且当前没有新的待决策项，就把排队中的下一条飞书出站回复
        提升为审批卡，保证队列逐条出队。
        """
        if event != Events.APPROVAL_RESOLVED:
            return
        if payload.get("kind") == FEISHU_OUTBOUND_KIND:
            try:
                await self._apply_outbound_result(payload)
            except Exception:  # noqa: BLE001 - 结果处理失败不能阻断后续排队卡片
                logger.exception("飞书出站审批处理失败 decision=%s", payload.get("decision_id"))
        await self._promote_queued(payload.get("task_id"))

    async def _apply_outbound_result(self, payload: dict[str, Any]) -> None:
        """按审批结果发送或拒绝一条已挂卡的飞书出站草稿。"""
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
        await self._deliver_claimed_draft(draft)

    async def _deliver_claimed_draft(self, draft: Any) -> None:
        """发送一条已被原子认领（pending → sending）的草稿并落终态。

        审批路径与「无规则自动发送」路径共用这一段：正文与附件都走同一套官方
        发送接口，失败标 failed 并保留正文，成功才 mark_sent。
        """
        service = self._container.outbound_drafts
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
                "未配置飞书应用凭据，草稿无法发送 draft=%s chat=%s",
                draft.id,
                chat_id,
            )
            return
        records = attachment_records(draft.attachments)
        # 先把每个附件还原并按平台上限校验：超限 / 类型不支持时绝不调用上传接口。
        try:
            files = [resolve_attachment(self._container.settings, record) for record in records]
            for uploaded in files:
                fresh = attachment_record(uploaded)
                validate_channel_attachment(
                    "feishu",
                    name=fresh["name"],
                    mime=fresh["mime"],
                    size=fresh["size"],
                    kind=fresh["kind"],
                )
        except ValidationError as exc:
            await service.mark_failed(draft.id, exc.message)
            logger.warning(
                "飞书出站附件校验失败 draft=%s files=%s reason=%s",
                draft.id,
                len(records),
                exc.message,
            )
            return
        except Exception as exc:  # noqa: BLE001 - 附件读取失败同样只落 failed
            await service.mark_failed(draft.id, exc)
            logger.exception("飞书出站附件准备失败 draft=%s files=%s", draft.id, len(records))
            return

        try:
            if (draft.body or "").strip():
                await sender.send_text(chat_id, draft.body)
            for uploaded in files:
                fresh = attachment_record(uploaded)
                await sender.send_media(
                    chat_id,
                    path=uploaded.path,
                    name=fresh["name"],
                    mime=fresh["mime"],
                    kind=fresh["kind"],
                )
        except Exception as exc:  # noqa: BLE001 - 发送失败只记录，不能拖垮决策接口
            await service.mark_failed(draft.id, exc)
            logger.exception(
                "飞书出站发送失败 draft=%s chat=%s hash=%s chars=%s files=%s",
                draft.id,
                chat_id,
                draft.body_hash,
                len(draft.body or ""),
                len(files),
            )
            return
        await service.mark_sent(draft.id)
        logger.info(
            "飞书出站已发送 draft=%s chat=%s hash=%s chars=%s files=%s",
            draft.id,
            chat_id,
            draft.body_hash,
            len(draft.body or ""),
            len(files),
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

    async def _existing_turn(self, bot: Any, client_turn_id: str) -> Any | None:
        finder = getattr(self._container.bot_conversation, "find_turn", None)
        if finder is None:
            return None
        return await finder(bot, client_turn_id)

    async def _send_system_message(self, chat_id: str, text: str) -> None:
        """绑定提示 / 成功确认等系统消息：直接发送，不走出站审批草稿。"""
        sender = self._sender
        if sender is None:
            logger.warning("飞书系统消息未发送：未配置应用凭据 chat=%s", chat_id)
            return
        await sender.send_text(chat_id, text)

    async def _load_media(
        self, parsed: _InboundMessage, *, cloud_user_id: str | None = None
    ) -> tuple[list[Any], str]:
        """下载一条图片/文件/语音并落盘；没有应用凭据时给中文说明而不是静默忽略。"""
        label = _KIND_LABELS[parsed.kind]
        sender = self._sender
        if sender is None or not hasattr(sender, "download_media"):
            logger.warning(
                "飞书入站媒体缺少应用凭据 message=%s kind=%s", parsed.message_id, parsed.kind
            )
            return [], f"收到{label}，但未配置飞书应用凭据，无法下载附件。"

        resource_type = "image" if parsed.kind == "image" else "file"
        media = await sender.download_media(
            message_id=parsed.message_id,
            file_key=parsed.resource_key,
            resource_type=resource_type,
        )
        stem = {"image": "image", "file": "file", "audio": "voice"}[parsed.kind]
        name = pick_media_name(
            preferred=parsed.file_name,
            downloaded=media.name,
            mime=media.mime,
            stem=stem,
            ext=".opus" if parsed.kind == "audio" else "",
        )
        uploaded = await save_media(
            self._container.settings,
            DownloadedMedia(name=name, mime=media.mime, chunks=media.chunks),
            cloud_user_id=cloud_user_id,
        )
        if parsed.kind == "audio":
            content = parsed.recognition or await transcribe_audio(self._transcriber, uploaded)
        else:
            content = ""
        logger.info(
            "飞书入站媒体已落盘 message=%s kind=%s upload=%s bytes=%s",
            parsed.message_id,
            parsed.kind,
            uploaded.id,
            uploaded.size,
        )
        return [uploaded], content

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
        """等这一轮结束；有回复就落草稿，命中出站规则才挂审批卡。

        没有命中规则时立即发送（默认零审批）；对话里已有待决策项时不再丢弃回复：
        草稿照常落库（decision_id 留空表示「排队中」），等上一张决策卡被解决后由
        `on_approval_resolved` 自动提升。
        """
        try:
            final = await self._container.bot_conversation.wait_for_turn(
                bot.id, turn.id, wait_seconds=self._turn_wait_seconds
            )
            if final.status != "completed" or final.reply_message_id is None:
                logger.info("飞书 turn 未产生可发送回复 turn=%s status=%s", turn.id, final.status)
                return
            async with self._approval_lock:
                reply = await self._container.task_repo.get_message(
                    final.task_id, final.reply_message_id
                )
                text = (reply.content or "").strip() if reply is not None else ""
                attachments = attachment_records(
                    reply.payload.get("attachments")
                    if reply is not None and isinstance(reply.payload, dict)
                    else None
                )
                # 只有附件、没有正文也是合法回复（媒体-only），不能因为 text 为空就丢。
                if not text and not attachments:
                    logger.info("飞书 turn 回复为空，不生成出站草稿 turn=%s", turn.id)
                    return

                service = self._container.outbound_drafts
                draft = await service.create(
                    channel="feishu",
                    body=text,
                    attachments=attachments,
                    dest={"chat_id": chat_id, "message_id": message_id},
                    bot_id=bot.id,
                    task_id=final.task_id,
                    turn_id=final.id,
                    cloud_user_id=getattr(final, "cloud_user_id", None),
                )
                logger.info(
                    "飞书出站回复已入队 draft=%s turn=%s task=%s hash=%s chars=%s files=%s",
                    draft.id,
                    final.id,
                    final.task_id,
                    draft.body_hash,
                    len(text),
                    len(attachments),
                )
                await self._promote_next_locked(final.task_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 后台跟踪失败只记录 id，不影响回调
            logger.exception("飞书 turn 跟踪失败 turn=%s", getattr(turn, "id", "?"))

    async def _promote_queued(self, raw_task_id: object) -> None:
        """一个决策被解决后，尝试把该对话的下一条排队回复提升成审批卡。"""
        try:
            task_id = uuid.UUID(str(raw_task_id))
        except (TypeError, ValueError):
            return
        async with self._approval_lock:
            await self._promote_next_locked(task_id)

    def _owner_cloud_user_id(self) -> str | None:
        return normalize_cloud_user_id(
            getattr(self._container.settings, "owner_cloud_user_id", None)
        )

    async def _outbound_rule(self, draft: Any) -> ApprovalRuleMatch | None:
        cloud_user_id = (
            normalize_cloud_user_id(getattr(draft, "cloud_user_id", None))
            or self._owner_cloud_user_id()
        )
        return await evaluate_approval(
            self._container,
            cloud_user_id=cloud_user_id,
            action=ApprovalAction(kind=ACTION_OUTBOUND_MESSAGE, channel="feishu"),
        )

    async def _promote_next_locked(self, task_id: uuid.UUID) -> None:
        """在已持有 `_approval_lock` 的前提下处理排队草稿。

        默认零审批：没有命中出站规则的草稿直接原子认领并发送；命中规则的才挂
        审批卡，只有「允许」后由 `on_approval_resolved` 发送。
        """
        service = getattr(self._container, "outbound_drafts", None)
        if service is None:  # pragma: no cover - 装配错误，按 fail-closed 跳过
            return

        for _ in range(_MAX_QUEUE_PROMOTIONS):
            draft = await service.next_queued(channel="feishu", task_id=task_id)
            if draft is None:
                return
            match = await self._outbound_rule(draft)
            if match is None:
                claimed = await service.claim(draft.id)
                if claimed is None:
                    continue
                await self._deliver_claimed_draft(claimed)
                continue

            try:
                task = await self._container.task_repo.get(task_id)
            except NotFoundError:
                return
            if task.pending_decision() is not None:
                # 同一对话同一时刻只挂一张卡；等这张卡被解决后会再次触发提升。
                return
            decision_id = uuid.uuid4().hex
            claimed = await service.claim_for_approval(draft.id, decision_id)
            if claimed is None:
                # 并发下别的处理器先认领了这张草稿，继续看队列里的下一条。
                continue
            try:
                task = await self._container.task_repo.get(task_id)
                if task.pending_decision() is not None:
                    await service.release_approval(claimed.id, decision_id)
                    return
                await self._raise_outbound_approval(claimed, decision_id, match)
            except Exception:  # noqa: BLE001 - 挂卡失败退回队列，不丢草稿
                await service.release_approval(claimed.id, decision_id)
                logger.exception("飞书排队出站审批提升失败 draft=%s task=%s", claimed.id, task_id)
            return

    async def _raise_outbound_approval(
        self, draft: Any, decision_id: str, match: ApprovalRuleMatch
    ) -> None:
        """为一条已认领的草稿挂审批卡；文案只谈发送，不谈终端命令。"""
        dest = draft.dest if isinstance(draft.dest, dict) else {}
        chat_id = str(dest.get("chat_id") or "").strip()
        message_id = str(dest.get("message_id") or "").strip()
        bot_id = str(draft.bot_id) if draft.bot_id is not None else ""
        turn_id = str(draft.turn_id) if draft.turn_id is not None else ""
        records = attachment_records(draft.attachments)
        detail = render_outbound_detail(
            f"准备发送到飞书会话 {chat_id}（消息 {message_id}）：",
            draft.body,
            records,
        )
        detail = f"{detail}\n\n触发规则：{match.rule.description or match.reason}"
        await raise_approval(
            self._container,
            task_id=draft.task_id,
            run_id=None,
            kind=FEISHU_OUTBOUND_KIND,
            decision_id=decision_id,
            title="飞书回复等待你确认",
            detail=detail,
            meta={
                "channel": "feishu",
                "draft_id": str(draft.id),
                "chat_id": chat_id,
                "message_id": message_id,
                "bot_id": bot_id,
                "turn_id": turn_id,
                "text_hash": draft.body_hash,
                "chars": len(draft.body or ""),
                "attachment_count": len(records),
                "attachments": attachment_meta(records),
                **rule_meta(match),
            },
            options=outbound_approval_options(CHANNEL_LABEL),
        )
        logger.info(
            "飞书出站审批已创建 draft=%s task=%s hash=%s chars=%s files=%s approval_rule=%s",
            draft.id,
            draft.task_id,
            draft.body_hash,
            len(draft.body or ""),
            len(records),
            match.rule.id,
        )


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


def _parse_message(payload: dict[str, Any]) -> _InboundMessage | FeishuCallbackResult:
    event = payload.get("event")
    event = event if isinstance(event, dict) else {}
    message = event.get("message")
    message = message if isinstance(message, dict) else {}
    # 发送者身份：优先 open_id（应用内稳定），其次 user_id / union_id。
    sender = event.get("sender")
    sender = sender if isinstance(sender, dict) else {}
    sender_ids = sender.get("sender_id")
    sender_ids = sender_ids if isinstance(sender_ids, dict) else {}
    sender_id = str(
        sender_ids.get("open_id") or sender_ids.get("user_id") or sender_ids.get("union_id") or ""
    ).strip()

    message_type = str(message.get("message_type") or "").strip().lower()
    # 其余类型（表情、位置、链接、视频等）保持 200 忽略，与既有行为一致。
    if message_type not in {"text", "image", "file", "audio"}:
        return FeishuCallbackResult(
            200, {"ignored": True, "reason": f"不支持的消息类型：{message_type or 'unknown'}"}
        )

    message_id = str(message.get("message_id") or "").strip()
    chat_id = str(message.get("chat_id") or "").strip()
    if not message_id or not chat_id:
        return FeishuCallbackResult(400, {"error": "回调缺少 message_id / chat_id"})

    content = _content_dict(message.get("content"))
    if message_type == "text":
        text = _text_content(content)
        if not text:
            return FeishuCallbackResult(200, {"ignored": True, "reason": "空文本消息"})
        return _InboundMessage(
            message_id=message_id,
            chat_id=chat_id,
            sender_id=sender_id,
            kind="text",
            text=text,
        )

    if message_type == "image":
        image_key = str(content.get("image_key") or "").strip()
        if not image_key:
            return FeishuCallbackResult(200, {"ignored": True, "reason": "图片消息缺少 image_key"})
        return _InboundMessage(
            message_id=message_id,
            chat_id=chat_id,
            sender_id=sender_id,
            kind="image",
            resource_key=image_key,
        )

    file_key = str(content.get("file_key") or "").strip()
    if not file_key:
        return FeishuCallbackResult(200, {"ignored": True, "reason": "媒体消息缺少 file_key"})
    file_name = str(content.get("file_name") or "").strip()
    if message_type == "file":
        return _InboundMessage(
            message_id=message_id,
            chat_id=chat_id,
            sender_id=sender_id,
            kind="file",
            resource_key=file_key,
            file_name=file_name,
        )

    # 飞书音频事件通常没有识别文本；若未来回调里出现 recognition 字段则直接采用。
    recognition = str(
        content.get("recognition") or content.get("speech_text") or content.get("text") or ""
    ).strip()
    return _InboundMessage(
        message_id=message_id,
        chat_id=chat_id,
        sender_id=sender_id,
        kind="audio",
        resource_key=file_key,
        file_name=file_name,
        recognition=recognition,
    )


def _content_dict(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _text_content(content: dict[str, Any]) -> str:
    text = content.get("text")
    return text.strip() if isinstance(text, str) else ""
