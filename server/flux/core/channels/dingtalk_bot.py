"""正式钉钉频道（HTTP 事件订阅入站 + 受审批保护的出站）。

v1 采用官方 SyncHTTP/HTTP 推送而不是 Stream 长连接：

- 钉钉把事件加密后 POST 到回调地址，查询串带 ``signature`` / ``timestamp`` /
  ``nonce``，请求体是 ``{"encrypt": "..."}``；
- 必须先验签再 AES 解密（见 `dingtalk_crypto`），ownerKey 是应用 AppKey；
- 无论事件是否处理，成功应答都必须是加密后的 ``"success"``，且要在约
  1500ms 内返回，所以这里只同步建 turn，回复跟踪放后台任务；
- ``check_url`` 是后台验证回调地址的特殊事件，直接回加密 success。

出站红线与飞书一致：**任何发给外部联系人的文本都不得自动发送**。turn 完成后
把待发送正文落进 `outbound_drafts`；没有命中该用户的出站规则时立即发送，命中规则
才在对话里挂审批卡；只有用户
选择「允许」后才认领草稿并调用官方机器人消息接口。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from flux.core.approval_rules.evaluator import (
    ACTION_OUTBOUND_MESSAGE,
    ApprovalAction,
    ApprovalRuleMatch,
    evaluate_approval,
    rule_meta,
)
from flux.core.channels.binding_flow import resolve_media_inbound, resolve_text_inbound
from flux.core.channels.dingtalk_crypto import (
    DingTalkCryptoError,
    build_encrypted_response,
    decrypt_event,
)
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
    "BOT_MESSAGE_TOPIC",
    "CHECK_URL_EVENT",
    "DINGTALK_OUTBOUND_KIND",
    "DingTalkBotChannel",
    "DingTalkBotSender",
    "DingTalkCallbackResult",
]

#: 钉钉 Stream/HTTP 机器人消息 topic（HTTP 事件里也可能以 headers.topic 出现）
BOT_MESSAGE_TOPIC = "/v1.0/im/bot/messages/get"
#: 官方允许的机器人消息事件类型（不同接入方式命名略有差异）
MESSAGE_EVENT_TYPES = frozenset({BOT_MESSAGE_TOPIC, "chatbot_message", "robot_message"})
#: URL 有效性验证事件
CHECK_URL_EVENT = "check_url"
#: 出站审批使用的决策 kind（ASCII，便于检索与断言）
DINGTALK_OUTBOUND_KIND = "dingtalk_outbound"
#: 审批卡文案里的频道名
CHANNEL_LABEL = "钉钉"
#: 平台单条文本上限的安全值
MAX_TEXT_CHARS = 4000
#: 提升排队草稿时最多重试几次（并发下别的处理器可能先认领同一张卡）
_MAX_QUEUE_PROMOTIONS = 5
#: 官方 OpenAPI 基址
DEFAULT_API_BASE = "https://api.dingtalk.com"


@dataclass(frozen=True)
class DingTalkCallbackResult:
    """回调处理结果：HTTP 状态码 + 返回体（成功时是官方加密 success）。"""

    status_code: int
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _InboundMessage:
    """解析后的入站消息：文本或一条待下载的媒体。"""

    message_id: str
    kind: str  # text / picture / audio / file
    dest: dict[str, str]
    text: str = ""
    download_code: str = ""
    file_name: str = ""
    recognition: str = ""


#: 媒体类型的中文名，用于缺凭据时给用户一句能看懂的解释。
_KIND_LABELS = {"picture": "图片", "audio": "语音", "file": "文件"}


class DingTalkBotSender:
    """钉钉机器人出站：企业内部应用 accessToken + 官方机器人消息接口。"""

    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        api_base: str = DEFAULT_API_BASE,
        default_robot_code: str | None = None,
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._api_base = (api_base or DEFAULT_API_BASE).rstrip("/") or DEFAULT_API_BASE
        self._default_robot_code = (default_robot_code or app_key or "").strip()
        self._timeout = max(1.0, float(timeout_seconds))
        self._client = client
        self._owns_client = client is None
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._lock = asyncio.Lock()

    async def send_text(self, dest: Mapping[str, Any], text: str) -> None:
        """按 dest 选择单聊或群聊接口发送文本，绝不静默丢弃。"""
        content = (text or "")[:MAX_TEXT_CHARS]
        msg_param = json.dumps({"content": content}, ensure_ascii=False)
        await self._send_robot(dest, "sampleText", msg_param, label="发送钉钉消息")

    async def upload_media(
        self,
        *,
        path: str,
        name: str,
        mime: str,
        media_type: str,
        robot_code: str = "",
    ) -> str:
        """上传机器人消息素材，返回 mediaId。

        官方 robot 消息文件接口：POST /v1.0/robot/messageFiles/upload（multipart）。
        保守上限：图片 10MB、文件 20MB；频道在调用前已按上限校验。
        """
        code = (robot_code or self._default_robot_code or "").strip()
        if not code:
            raise RuntimeError("钉钉上传媒体缺少 robot_code")
        token = await self._access_token()
        response = await self._request(
            "POST",
            "/v1.0/robot/messageFiles/upload",
            params={"robotCode": code},
            headers={"x-acs-dingtalk-access-token": token},
            data={"robotCode": code, "mediaType": media_type},
            files={"file": (name, _read_media(path, "钉钉"), mime or "application/octet-stream")},
        )
        data = _payload(response, "上传钉钉媒体")
        media_id = str(data.get("mediaId") or data.get("media_id") or "")
        if not media_id:
            raise RuntimeError("上传钉钉媒体失败：响应缺少 mediaId")
        return media_id

    async def send_image(self, dest: Mapping[str, Any], media_id: str) -> None:
        """发送图片：官方 msgKey=sampleImageMsg，msgParam 里的 photoURL 用 mediaId。"""
        await self._send_robot(
            dest,
            "sampleImageMsg",
            json.dumps({"photoURL": media_id}, ensure_ascii=False),
            label="发送钉钉图片",
        )

    async def send_file(self, dest: Mapping[str, Any], media_id: str, name: str) -> None:
        """发送文件：官方 msgKey=sampleFile，msgParam 为 mediaId + fileName + fileType。"""
        suffix = Path(name).suffix.lstrip(".").lower() or "file"
        await self._send_robot(
            dest,
            "sampleFile",
            json.dumps(
                {"mediaId": media_id, "fileName": name, "fileType": suffix},
                ensure_ascii=False,
            ),
            label="发送钉钉文件",
        )

    async def send_media(
        self, dest: Mapping[str, Any], *, path: str, name: str, mime: str, kind: str
    ) -> None:
        """按类型走官方上传 + 发送；调用方必须先按平台上限校验。"""
        robot_code = str(dest.get("robot_code") or self._default_robot_code or "").strip()
        media_type = "image" if kind == "image" else "file"
        media_id = await self.upload_media(
            path=path,
            name=name,
            mime=mime,
            media_type=media_type,
            robot_code=robot_code,
        )
        if kind == "image":
            await self.send_image(dest, media_id)
        else:
            await self.send_file(dest, media_id, name)

    async def _send_robot(
        self, dest: Mapping[str, Any], msg_key: str, msg_param: str, *, label: str
    ) -> None:
        """复用既有单聊 / 群聊机器人接口，只替换 msgKey / msgParam。"""
        robot_code = str(dest.get("robot_code") or self._default_robot_code).strip()
        if not robot_code:
            raise RuntimeError("钉钉发送缺少 robot_code")
        conversation_id = str(dest.get("conversation_id") or "").strip()
        staff_id = str(dest.get("staff_id") or "").strip()
        conversation_type = str(dest.get("conversation_type") or "").strip()

        token = await self._access_token()
        headers = {"x-acs-dingtalk-access-token": token}
        if conversation_type == "1" or (not conversation_id and staff_id):
            if not staff_id:
                raise RuntimeError("钉钉单聊发送缺少 staff_id")
            response = await self._request(
                "POST",
                "/v1.0/robot/oToMessages/batchSend",
                headers=headers,
                json={
                    "robotCode": robot_code,
                    "userIds": [staff_id],
                    "msgKey": msg_key,
                    "msgParam": msg_param,
                },
            )
        elif conversation_id:
            response = await self._request(
                "POST",
                "/v1.0/robot/groupMessages/send",
                headers=headers,
                json={
                    "robotCode": robot_code,
                    "openConversationId": conversation_id,
                    "msgKey": msg_key,
                    "msgParam": msg_param,
                },
            )
        else:
            raise RuntimeError("钉钉发送缺少 conversation_id / staff_id")
        _raise_for_dingtalk(response, label)

    @property
    def default_robot_code(self) -> str:
        """未从入站事件带出 robotCode 时的兜底（应用 AppKey）。"""
        return self._default_robot_code

    async def download_media(
        self,
        *,
        download_code: str,
        robot_code: str | None = None,
    ) -> DownloadedMedia:
        """官方两步下载：先换 downloadUrl，再 GET 预签名地址取字节。"""
        code = (robot_code or self._default_robot_code or "").strip()
        if not code:
            raise RuntimeError("钉钉下载媒体缺少 robot_code")
        token = await self._access_token()
        response = await self._request(
            "POST",
            "/v1.0/robot/messageFiles/download",
            headers={"x-acs-dingtalk-access-token": token},
            json={"downloadCode": download_code, "robotCode": code},
        )
        data = _payload(response, "获取钉钉媒体下载地址")
        url = str(data.get("downloadUrl") or "").strip()
        if not url:
            raise RuntimeError("钉钉媒体下载地址响应缺少 downloadUrl")
        download = await self._send_stream("GET", url)
        return await media_from_response(download, label="下载钉钉媒体")

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    async def _access_token(self) -> str:
        """缓存企业内部应用 accessToken，提前 60 秒过期。"""
        if self._token is not None and time.monotonic() < self._token_expires_at:
            return self._token
        async with self._lock:
            if self._token is not None and time.monotonic() < self._token_expires_at:
                return self._token
            response = await self._request(
                "POST",
                "/v1.0/oauth2/accessToken",
                json={"appKey": self._app_key, "appSecret": self._app_secret},
            )
            data = _payload(response, "获取钉钉 accessToken")
            token = str(data.get("accessToken") or "")
            if not token:
                raise RuntimeError("钉钉 accessToken 响应缺少令牌")
            expire = int(data.get("expireIn") or data.get("expire_in") or 7200)
            self._token = token
            self._token_expires_at = time.monotonic() + max(60, expire - 60)
            return token

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return await self._ensure_client().request(method, f"{self._api_base}{path}", **kwargs)

    async def _send_stream(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """downloadUrl 是绝对地址（预签名），且可能 302；这里直接流式 GET。"""
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
    if response.status_code >= 400:
        code = data.get("code") or data.get("errcode") or ""
        message = data.get("message") or data.get("errmsg") or ""
        raise RuntimeError(f"{label}失败：HTTP {response.status_code} code={code} {message}"[:500])
    return data


def _raise_for_dingtalk(response: httpx.Response, label: str) -> None:
    _payload(response, label)


class DingTalkBotChannel:
    """钉钉正式频道：加密回调验签解密、入站建 turn、出站走审批。"""

    def __init__(
        self,
        container: Container,
        *,
        app_key: str | None = None,
        app_secret: str | None = None,
        token: str | None = None,
        aes_key: str | None = None,
        api_base: str = DEFAULT_API_BASE,
        bot_id: str | None = None,
        sender: Any | None = None,
        transcriber: Transcriber | None = None,
        turn_wait_seconds: float = 900.0,
    ) -> None:
        self._container = container
        self._transcriber = transcriber
        self._app_key = (app_key or "").strip() or None
        self._token = (token or "").strip() or None
        self._aes_key = (aes_key or "").strip() or None
        self._bot_id = _parse_uuid(bot_id)
        if (bot_id or "").strip() and self._bot_id is None:
            logger.warning("FLUX_DINGTALK_BOT_ID 不是合法 UUID，钉钉消息事件将被拒绝")
        if sender is not None:
            self._sender = sender
        elif self._app_key and app_secret:
            self._sender = DingTalkBotSender(
                app_key=self._app_key,
                app_secret=app_secret,
                api_base=api_base,
            )
        else:
            self._sender = None
        self._background: set[asyncio.Task[Any]] = set()
        #: 同一对话任务同一时刻只挂一张出站审批卡。
        #: 容器装配时三个频道共用一把锁；单测里的简易容器则退回频道私有锁。
        shared_lock = getattr(container, "outbound_approval_lock", None)
        self._approval_lock = (
            shared_lock if isinstance(shared_lock, asyncio.Lock) else asyncio.Lock()
        )
        self._turn_wait_seconds = max(1.0, float(turn_wait_seconds))

    async def handle(
        self,
        body: bytes,
        query: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> DingTalkCallbackResult:
        """处理一次钉钉 HTTP 回调，成功时返回官方加密 success。"""
        raw = body or b""
        params = query or {}
        request_headers = headers or {}

        missing = self._missing_config()
        if missing is not None:
            return missing

        try:
            envelope = json.loads(raw or b"{}")
        except (UnicodeDecodeError, ValueError):
            return DingTalkCallbackResult(400, {"error": "请求体不是合法 JSON"})
        if not isinstance(envelope, dict):
            return DingTalkCallbackResult(400, {"error": "请求体结构异常"})
        encrypt = envelope.get("encrypt")
        if not isinstance(encrypt, str) or not encrypt:
            return DingTalkCallbackResult(400, {"error": "回调缺少有效的 encrypt 字段"})

        signature = _lookup(params, request_headers, ("msg_signature", "signature"))
        timestamp = _lookup(params, request_headers, ("timeStamp", "timestamp"))
        nonce = _lookup(params, request_headers, ("nonce",))
        try:
            payload = decrypt_event(
                token=self._token or "",
                aes_key=self._aes_key or "",
                owner_key=self._app_key or "",
                signature=signature,
                timestamp=timestamp,
                nonce=nonce,
                encrypt=encrypt,
            )
        except DingTalkCryptoError as exc:
            # 签名失败与解密失败分开给状态码，便于平台侧排查；错误信息不含密钥。
            status = 403 if "签名" in str(exc) else 400
            logger.warning("钉钉回调校验失败 status=%s reason=%s", status, exc)
            return DingTalkCallbackResult(status, {"error": str(exc)})

        event_type, topic, event_id, data = _event_info(payload)

        # 后台「验证有效性」推的就是 check_url：不需要 Bot，直接回加密 success。
        if event_type == CHECK_URL_EVENT:
            logger.info("dingtalk.check_url event=%s", event_id or "-")
            return self._success()

        if not _is_message_event(event_type, topic):
            logger.info(
                "dingtalk.event.ignored type=%s topic=%s event=%s",
                event_type or "unknown",
                topic or "-",
                event_id or "-",
            )
            return self._success()

        # 以下都是消息事件：必须知道归属 Bot，否则 fail-closed。
        bot_id = self._bot_id
        if bot_id is None:
            return DingTalkCallbackResult(
                400,
                {"error": "未配置合法的 FLUX_DINGTALK_BOT_ID，拒绝处理钉钉消息事件"},
            )

        parsed = _parse_message(data, event_id)
        if isinstance(parsed, DingTalkCallbackResult):
            # 忽略类结果也必须回加密 success，否则钉钉会持续重推。
            if parsed.status_code == 200:
                logger.info(
                    "dingtalk.inbound.ignored event=%s reason=%s",
                    event_id or "-",
                    parsed.payload.get("reason", "-"),
                )
                return self._success()
            return parsed
        message_id = parsed.message_id
        dest = parsed.dest

        try:
            bot = await self._container.bots.get(bot_id)
        except NotFoundError:
            logger.warning("钉钉回调指向的 Bot 不存在 bot=%s", bot_id)
            return DingTalkCallbackResult(404, {"error": "FLUX_DINGTALK_BOT_ID 指向的 Bot 不存在"})

        # 外部用户 id 用 senderStaffId，而不是 conversationId：群聊里同一个人
        # 在不同会话中的 staff_id 不变，绑定才不会随群迁移失效。
        external_user_id = str(dest.get("staff_id") or "").strip()

        async def _system_reply(text: str) -> None:
            await self._send_system_message(dest, text)

        if parsed.kind == "text":
            cloud_user_id = await resolve_text_inbound(
                self._container,
                channel="dingtalk",
                external_user_id=external_user_id,
                text=parsed.text,
                reply=_system_reply,
            )
            if cloud_user_id is None:
                return self._success()
            files: list[Any] = []
            content = parsed.text
        else:
            cloud_user_id = await resolve_media_inbound(
                self._container,
                channel="dingtalk",
                external_user_id=external_user_id,
                reply=_system_reply,
            )
            if cloud_user_id is None:
                return self._success()
            # 重推：turn 已存在就直接回加密 success，绝不再次下载媒体。
            existing = await self._existing_turn(bot, message_id)
            if existing is not None:
                logger.info(
                    "dingtalk.inbound.replay bot=%s message=%s turn=%s",
                    bot_id,
                    message_id,
                    existing.id,
                )
                return self._success()
            try:
                files, content = await self._load_media(parsed, cloud_user_id=cloud_user_id)
            except ValidationError as exc:
                logger.warning(
                    "钉钉入站媒体被拒绝 message=%s kind=%s reason=%s",
                    message_id,
                    parsed.kind,
                    exc.message,
                )
                return self._success()
            except Exception as exc:  # noqa: BLE001 - 下载异常不能拖垮进程
                logger.warning(
                    "钉钉入站媒体下载失败 message=%s kind=%s error=%s",
                    message_id,
                    parsed.kind,
                    safe_error(exc),
                )
                return DingTalkCallbackResult(502, {"error": "钉钉媒体下载失败，请稍后重试"})

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
                "钉钉入站建 turn 失败 bot=%s message=%s code=%s",
                bot_id,
                message_id,
                exc.code,
            )
            activity = getattr(self._container, "channel_activity", None)
            if activity is not None:
                activity.record_error("dingtalk", exc.code, exc.message)
            return DingTalkCallbackResult(
                exc.http_status,
                {"error": exc.message, "code": exc.code},
            )

        activity = getattr(self._container, "channel_activity", None)
        if activity is not None:
            activity.record_receive("dingtalk")
        if created:
            self._schedule_tracking(
                bot=bot,
                turn=turn,
                message_id=message_id,
                dest=dest,
            )
        logger.info(
            "dingtalk.inbound bot=%s message=%s turn=%s created=%s dest_keys=%s",
            bot_id,
            message_id,
            turn.id,
            created,
            sorted(dest),
        )
        # 应答必须是官方加密 success，不能等 Bot 跑完（平台限时约 1500ms）。
        return self._success()

    async def on_approval_resolved(self, event: str, payload: dict[str, Any]) -> None:
        """审批结果监听：只有「允许」才发送，同一条草稿最多发送一次。

        无论这次解决的是不是钉钉出站卡，只要有决策被解决且当前没有新的待决策
        项，就把排队中的下一条钉钉出站回复提升为审批卡。
        """
        if event != Events.APPROVAL_RESOLVED:
            return
        if payload.get("kind") == DINGTALK_OUTBOUND_KIND:
            try:
                await self._apply_outbound_result(payload)
            except Exception:  # noqa: BLE001 - 结果处理失败不能阻断后续排队卡片
                logger.exception("钉钉出站审批处理失败 decision=%s", payload.get("decision_id"))
        await self._promote_queued(payload.get("task_id"))

    async def _apply_outbound_result(self, payload: dict[str, Any]) -> None:
        """按审批结果发送或拒绝一条已挂卡的钉钉出站草稿。"""
        meta = payload.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        raw_draft_id = str(meta.get("draft_id") or "").strip()
        if not raw_draft_id:
            logger.warning("钉钉出站审批缺少 draft_id，已跳过")
            return
        try:
            draft_id = uuid.UUID(raw_draft_id)
        except ValueError:
            logger.warning("钉钉出站审批的 draft_id 非法，已跳过 draft=%s", raw_draft_id)
            return

        service = getattr(self._container, "outbound_drafts", None)
        if service is None:  # pragma: no cover - 装配错误，按 fail-closed 跳过
            logger.error("出站草稿服务未装配，审批结果已忽略 draft=%s", raw_draft_id)
            return

        if payload.get("result") != "allowed" or payload.get("chosen") != APPROVAL_ALLOW:
            await service.mark_denied(draft_id)
            logger.info(
                "dingtalk.outbound.skip decision=%s result=%s draft=%s",
                payload.get("decision_id"),
                payload.get("result"),
                raw_draft_id,
            )
            return

        draft = await service.claim(draft_id)
        if draft is None:
            logger.info("钉钉出站草稿不存在或已被处理 draft=%s", raw_draft_id)
            return
        await self._deliver_claimed_draft(draft)

    async def _deliver_claimed_draft(self, draft: Any) -> None:
        """发送一条已被原子认领的草稿；审批与「无规则自动发送」共用。"""
        service = self._container.outbound_drafts
        dest = draft.dest if isinstance(draft.dest, dict) else {}
        has_target = bool(
            str(dest.get("conversation_id") or "").strip()
            or str(dest.get("staff_id") or "").strip()
        )
        if draft.channel != "dingtalk" or not has_target:
            await service.mark_failed(draft.id, "channel_or_dest_mismatch")
            logger.error(
                "钉钉出站草稿的频道或目标不匹配 draft=%s channel=%s dest_keys=%s",
                draft.id,
                draft.channel,
                sorted(dest),
            )
            return
        sender = self._sender
        if sender is None:
            # 标 failed 但保留正文，配置好凭据后仍可在草稿箱看到。
            await service.mark_failed(draft.id, "sender_not_configured")
            logger.error(
                "未配置钉钉应用凭据，草稿无法发送 draft=%s dest_keys=%s",
                draft.id,
                sorted(dest),
            )
            return
        records = attachment_records(draft.attachments)
        # 上传前先按钉钉官方（保守）上限校验；超限 / 类型不支持时不调用上传接口。
        try:
            files = [resolve_attachment(self._container.settings, record) for record in records]
            for uploaded in files:
                fresh = attachment_record(uploaded)
                validate_channel_attachment(
                    "dingtalk",
                    name=fresh["name"],
                    mime=fresh["mime"],
                    size=fresh["size"],
                    kind=fresh["kind"],
                )
        except ValidationError as exc:
            await service.mark_failed(draft.id, exc.message)
            logger.warning(
                "钉钉出站附件校验失败 draft=%s files=%s reason=%s",
                draft.id,
                len(records),
                exc.message,
            )
            return
        except Exception as exc:  # noqa: BLE001 - 附件读取失败同样只落 failed
            await service.mark_failed(draft.id, exc)
            logger.exception("钉钉出站附件准备失败 draft=%s files=%s", draft.id, len(records))
            return

        try:
            if (draft.body or "").strip():
                await sender.send_text(dest, draft.body)
            for uploaded in files:
                fresh = attachment_record(uploaded)
                await sender.send_media(
                    dest,
                    path=uploaded.path,
                    name=fresh["name"],
                    mime=fresh["mime"],
                    kind=fresh["kind"],
                )
        except Exception as exc:  # noqa: BLE001 - 发送失败只记录，不能拖垮决策接口
            await service.mark_failed(draft.id, exc)
            logger.exception(
                "钉钉出站发送失败 draft=%s dest_keys=%s hash=%s chars=%s files=%s",
                draft.id,
                sorted(dest),
                draft.body_hash,
                len(draft.body or ""),
                len(files),
            )
            return
        await service.mark_sent(draft.id)
        logger.info(
            "钉钉出站已发送 draft=%s dest_keys=%s hash=%s chars=%s files=%s",
            draft.id,
            sorted(dest),
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

    async def _existing_turn(self, bot: Any, client_turn_id: str) -> Any | None:
        finder = getattr(self._container.bot_conversation, "find_turn", None)
        if finder is None:
            return None
        return await finder(bot, client_turn_id)

    async def _send_system_message(self, dest: dict[str, str], text: str) -> None:
        """绑定提示 / 成功确认等系统消息：直接发送，不走出站审批草稿。"""
        sender = self._sender
        if sender is None:
            logger.warning("钉钉系统消息未发送：未配置应用凭据 dest_keys=%s", sorted(dest))
            return
        await sender.send_text(dict(dest), text)

    async def _load_media(
        self, parsed: _InboundMessage, *, cloud_user_id: str | None = None
    ) -> tuple[list[Any], str]:
        """下载一条图片/语音/文件并落盘；缺凭据时给中文说明而不是静默忽略。"""
        label = _KIND_LABELS[parsed.kind]
        sender = self._sender
        if sender is None or not hasattr(sender, "download_media"):
            logger.warning(
                "钉钉入站媒体缺少应用凭据 message=%s kind=%s",
                parsed.message_id,
                parsed.kind,
            )
            return [], f"收到{label}，但未配置钉钉应用凭据，无法下载附件。"

        robot_code = (
            parsed.dest.get("robot_code") or getattr(sender, "default_robot_code", "") or ""
        ).strip()
        if not robot_code:
            logger.warning("钉钉入站媒体缺少 robotCode message=%s", parsed.message_id)
            return [], f"收到{label}，但未配置钉钉 robotCode，无法下载附件。"

        media = await sender.download_media(
            download_code=parsed.download_code,
            robot_code=robot_code,
        )
        stem = {"picture": "image", "audio": "voice", "file": "file"}[parsed.kind]
        name = pick_media_name(
            preferred=parsed.file_name,
            downloaded=media.name,
            mime=media.mime,
            stem=stem,
            ext=".amr" if parsed.kind == "audio" else "",
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
            "钉钉入站媒体已落盘 message=%s kind=%s upload=%s bytes=%s",
            parsed.message_id,
            parsed.kind,
            uploaded.id,
            uploaded.size,
        )
        return [uploaded], content

    def _success(self) -> DingTalkCallbackResult:
        return DingTalkCallbackResult(
            200,
            build_encrypted_response(
                token=self._token or "",
                aes_key=self._aes_key or "",
                owner_key=self._app_key or "",
            ),
        )

    def _missing_config(self) -> DingTalkCallbackResult | None:
        """三项缺一不可；任一缺失都拒绝，错误里只报环境变量名。"""
        if self._token is None:
            return DingTalkCallbackResult(
                400,
                {"error": "未配置钉钉签名 token，拒绝处理回调（请设置 FLUX_DINGTALK_TOKEN）"},
            )
        if self._aes_key is None:
            return DingTalkCallbackResult(
                400,
                {
                    "error": (
                        "未配置钉钉 EncodingAESKey，拒绝处理回调（请设置 FLUX_DINGTALK_AES_KEY）"
                    )
                },
            )
        if self._app_key is None:
            return DingTalkCallbackResult(
                400,
                {"error": "未配置钉钉 AppKey，拒绝处理回调（请设置 FLUX_DINGTALK_APP_KEY）"},
            )
        return None

    def _schedule_tracking(
        self,
        *,
        bot: Any,
        turn: Any,
        message_id: str,
        dest: dict[str, str],
    ) -> None:
        task = asyncio.create_task(
            self._track_turn(bot=bot, turn=turn, message_id=message_id, dest=dest),
            name="flux-dingtalk-turn",
        )
        self._background.add(task)

        def _cleanup(done: asyncio.Task[Any]) -> None:
            self._background.discard(done)

        task.add_done_callback(_cleanup)

    async def _track_turn(
        self,
        *,
        bot: Any,
        turn: Any,
        message_id: str,
        dest: dict[str, str],
    ) -> None:
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
                logger.info("钉钉 turn 未产生可发送回复 turn=%s status=%s", turn.id, final.status)
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
                if not text and not attachments:
                    logger.info("钉钉 turn 回复为空，不生成出站草稿 turn=%s", turn.id)
                    return

                service = self._container.outbound_drafts
                draft = await service.create(
                    channel="dingtalk",
                    body=text,
                    attachments=attachments,
                    dest=dict(dest),
                    bot_id=bot.id,
                    task_id=final.task_id,
                    turn_id=final.id,
                    cloud_user_id=getattr(final, "cloud_user_id", None),
                )
                logger.info(
                    "钉钉出站回复已入队 draft=%s turn=%s task=%s hash=%s chars=%s files=%s",
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
            logger.exception("钉钉 turn 跟踪失败 turn=%s", getattr(turn, "id", "?"))

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
            action=ApprovalAction(kind=ACTION_OUTBOUND_MESSAGE, channel="dingtalk"),
        )

    async def _promote_next_locked(self, task_id: uuid.UUID) -> None:
        """处理排队草稿：无规则直接发送；命中规则才挂审批卡。"""
        service = getattr(self._container, "outbound_drafts", None)
        if service is None:  # pragma: no cover - 装配错误，按 fail-closed 跳过
            return

        for _ in range(_MAX_QUEUE_PROMOTIONS):
            draft = await service.next_queued(channel="dingtalk", task_id=task_id)
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
                logger.exception("钉钉排队出站审批提升失败 draft=%s task=%s", claimed.id, task_id)
            return

    async def _raise_outbound_approval(
        self, draft: Any, decision_id: str, match: ApprovalRuleMatch
    ) -> None:
        """为一条已认领的草稿挂审批卡；文案只谈发送，不谈终端命令。"""
        dest = draft.dest if isinstance(draft.dest, dict) else {}
        target = dest.get("conversation_id") or dest.get("staff_id") or ""
        message_id = str(dest.get("message_id") or "").strip()
        bot_id = str(draft.bot_id) if draft.bot_id is not None else ""
        turn_id = str(draft.turn_id) if draft.turn_id is not None else ""
        records = attachment_records(draft.attachments)
        detail = render_outbound_detail(
            f"准备发送到钉钉会话 {target}（消息 {message_id}）：",
            draft.body,
            records,
        )
        detail = f"{detail}\n\n触发规则：{match.rule.description or match.reason}"
        await raise_approval(
            self._container,
            task_id=draft.task_id,
            run_id=None,
            kind=DINGTALK_OUTBOUND_KIND,
            decision_id=decision_id,
            title="钉钉回复等待你确认",
            detail=detail,
            meta={
                "channel": "dingtalk",
                "draft_id": str(draft.id),
                "conversation_id": dest.get("conversation_id", ""),
                "staff_id": dest.get("staff_id", ""),
                "robot_code": dest.get("robot_code", ""),
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
            "钉钉出站审批已创建 draft=%s task=%s hash=%s chars=%s files=%s approval_rule=%s",
            draft.id,
            draft.task_id,
            draft.body_hash,
            len(draft.body or ""),
            len(records),
            match.rule.id,
        )


def _lookup(
    params: Mapping[str, str],
    headers: Mapping[str, str],
    names: tuple[str, ...],
) -> str | None:
    """官方文档在查询串与 header 上的字段名不一致，按顺序兼容。"""
    for name in names:
        value = params.get(name)
        if value:
            return str(value)
    lowered = {str(key).lower(): value for key, value in headers.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value:
            return str(value)
    return None


def _event_info(payload: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    """兼容扁平旧事件与带 headers/data 的新事件，返回 (type, topic, id, data)。"""
    headers = payload.get("headers")
    headers = headers if isinstance(headers, dict) else {}
    event_type = str(
        payload.get("EventType") or payload.get("eventType") or headers.get("eventType") or ""
    ).strip()
    topic = str(headers.get("topic") or payload.get("topic") or "").strip()
    event_id = str(
        payload.get("eventId")
        or payload.get("EventId")
        or headers.get("eventId")
        or payload.get("BizId")
        or ""
    ).strip()
    data = payload.get("data")
    if isinstance(data, str) and data.strip():
        try:
            data = json.loads(data)
        except ValueError:
            data = None
    if not isinstance(data, dict):
        data = payload
    return event_type, topic, event_id, data


def _is_message_event(event_type: str, topic: str) -> bool:
    return event_type in MESSAGE_EVENT_TYPES or topic == BOT_MESSAGE_TOPIC


#: 官方入站类型（picture）与常见别名都收进来；video / 表情等保持忽略。
_MESSAGE_KINDS = {
    "text": "text",
    "picture": "picture",
    "image": "picture",
    "audio": "audio",
    "voice": "audio",
    "file": "file",
}


def _parse_message(data: dict[str, Any], event_id: str) -> _InboundMessage | DingTalkCallbackResult:
    message_type = str(
        data.get("msgtype") or data.get("msgType") or data.get("messageType") or ""
    ).strip()
    kind = _MESSAGE_KINDS.get(message_type.lower())
    if kind is None:
        return DingTalkCallbackResult(
            200,
            {"ignored": True, "reason": f"不支持的消息类型：{message_type or 'unknown'}"},
        )

    message_id = str(
        data.get("msgId")
        or data.get("msgid")
        or data.get("messageId")
        or data.get("message_id")
        or event_id
        or ""
    ).strip()
    if not message_id:
        return DingTalkCallbackResult(400, {"error": "回调缺少 msgId / eventId"})

    dest: dict[str, str] = {"message_id": message_id}
    conversation_id = str(
        data.get("conversationId")
        or data.get("conversation_id")
        or data.get("openConversationId")
        or ""
    ).strip()
    staff_id = str(
        data.get("senderStaffId") or data.get("sender_staff_id") or data.get("senderId") or ""
    ).strip()
    robot_code = str(data.get("robotCode") or data.get("robot_code") or "").strip()
    conversation_type = str(
        data.get("conversationType") or data.get("conversation_type") or ""
    ).strip()
    if conversation_id:
        dest["conversation_id"] = conversation_id
    if staff_id:
        dest["staff_id"] = staff_id
    if robot_code:
        dest["robot_code"] = robot_code
    if conversation_type:
        dest["conversation_type"] = conversation_type
    if "conversation_id" not in dest and "staff_id" not in dest:
        return DingTalkCallbackResult(400, {"error": "回调缺少 conversationId / senderStaffId"})

    if kind == "text":
        text = _text_content(data)
        if not text:
            return DingTalkCallbackResult(200, {"ignored": True, "reason": "空文本消息"})
        return _InboundMessage(message_id=message_id, kind="text", dest=dest, text=text)

    # 官方媒体消息把字段放在与 msgtype 同名的子对象里（picture / audio / file），
    # 常见别名（image / voice）也可能出现；部分网关则直接扁平化到顶层。
    content: dict[str, Any] = {}
    for key in dict.fromkeys((message_type.lower(), kind, "content")):
        candidate = _content_dict(data.get(key))
        if candidate:
            content = candidate
            break
    download_code = str(
        content.get("downloadCode")
        or content.get("pictureDownloadCode")
        or data.get("downloadCode")
        or data.get("pictureDownloadCode")
        or ""
    ).strip()
    if not download_code:
        return DingTalkCallbackResult(
            200,
            {"ignored": True, "reason": f"媒体消息缺少 downloadCode：{kind}"},
        )
    return _InboundMessage(
        message_id=message_id,
        kind=kind,
        dest=dest,
        download_code=download_code,
        file_name=str(content.get("fileName") or data.get("fileName") or "").strip(),
        recognition=str(content.get("recognition") or data.get("recognition") or "").strip(),
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


def _text_content(data: dict[str, Any]) -> str:
    text = data.get("text")
    if isinstance(text, dict):
        content = text.get("content")
        if isinstance(content, str):
            return content.strip()
    content = data.get("content")
    if isinstance(content, dict):
        value = content.get("text") or content.get("content")
        if isinstance(value, str):
            return value.strip()
    return ""


def _parse_uuid(value: str | None) -> uuid.UUID | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None
