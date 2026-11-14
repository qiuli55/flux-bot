"""正式企业微信（WeCom）频道：加密回调入站 + 受审批保护的出站。

与 Supervisor 的微信桥、飞书频道相互独立：这里面向企业微信自建应用的
「接收消息与事件」回调（官方文档 path/90930），把文本消息变成 Flux Bot 的对话
轮次；回复只落 `outbound_drafts` 并挂审批卡，用户点「允许」后才真正发送。

入站：

- GET 校验 URL：按官方约定验签（token / timestamp / nonce / echostr 字典序
  sha1），解密 echostr 后**返回明文**，不是密文也不是 JSON；
- POST 加密 XML：验签（密文参与排序）→ AES-CBC 解密 → 校验 receiveid；
- 未配置 token 或 EncodingAESKey 时拒绝一切回调（fail-closed）；
- 只处理 ``MsgType=text``，``MsgId`` 作为 ``client_turn_id`` 去重；
- 同步建 turn 后立即回 ``success``，不等待模型完成。

出站默认**不需要审批**：没有命中该用户的出站规则时，草稿立即原子认领并调用
企业微信应用消息接口（标 sent 作为审计）。命中规则才挂审批卡，只有「允许」才
发送；拒绝 / 发送失败都保留正文，绝不自动重试。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
import xml.etree.ElementTree as ET
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
from flux.core.channels.wecom_crypto import WeComCryptoError, decrypt_message, verify_signature
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
    "WECOM_OUTBOUND_KIND",
    "WeComBotChannel",
    "WeComBotSender",
    "WeComCallbackResult",
]

#: 出站审批使用的决策 kind（ASCII，便于检索与断言）
WECOM_OUTBOUND_KIND = "wecom_outbound"
#: 审批卡文案里的频道名
CHANNEL_LABEL = "企业微信"
#: 企业微信应用消息单条文本上限（官方为 2048 字节，按 UTF-8 字节保守截断）
MAX_TEXT_BYTES = 2048
#: 提升排队草稿时最多重试几次（并发下别的处理器可能先认领同一张卡）
_MAX_QUEUE_PROMOTIONS = 5
#: 回调体上限：正常消息远小于此，超限直接拒绝，避免 XML 解析消耗
_MAX_BODY_BYTES = 1_048_576
#: 官方成功响应：企业微信看到 ``success`` 或空串即认为已消费，不再重推
_SUCCESS_BODY = "success"
#: httpx 会把完整请求 URL 打到 INFO：企业微信的 access_token / corpsecret 在
#: query 里，必须在这里脱敏，否则日志会带出凭据。
_SECRET_QUERY_RE = re.compile(r"((?:access_token|corpsecret)=)[^&\s\"]+")


class _RedactSecretQuery(logging.Filter):
    """把 httpx 日志里的 access_token / corpsecret 值替换成 ``***``。"""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(_scrub(arg) for arg in args)
        elif isinstance(args, dict):
            record.args = {key: _scrub(value) for key, value in args.items()}
        return True


def _scrub(value: object) -> object:
    if isinstance(value, httpx.URL):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        return value
    scrubbed = _SECRET_QUERY_RE.sub(r"\1***", text)
    return scrubbed if scrubbed != text else value


def _install_httpx_redaction() -> None:
    """幂等安装一次；只影响 httpx 的日志记录，不改请求本身。"""
    target = logging.getLogger("httpx")
    if not any(isinstance(item, _RedactSecretQuery) for item in target.filters):
        target.addFilter(_RedactSecretQuery())


@dataclass(frozen=True)
class WeComCallbackResult:
    """回调处理结果：HTTP 状态码 + 纯文本返回体（不套统一 JSON 信封）。"""

    status_code: int
    body: str = _SUCCESS_BODY
    media_type: str = "text/plain; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _InboundMessage:
    """解析后的入站消息：文本或一条待下载的媒体。"""

    message_id: str
    from_user: str
    kind: str  # text / image / voice / file
    text: str = ""
    media_id: str = ""
    file_name: str = ""
    voice_format: str = ""
    recognition: str = ""


#: 媒体类型的中文名，用于缺凭据时给用户一句能看懂的解释。
_KIND_LABELS = {"image": "图片", "voice": "语音", "file": "文件"}


class WeComBotSender:
    """企业微信自建应用出站：access_token + 应用消息接口。

    发送目标由草稿的 ``dest`` 决定：``touser`` 走 ``/cgi-bin/message/send``，
    ``chatid`` 走 ``/cgi-bin/appchat/send``。日志与异常只带 errcode / errmsg，
    绝不回显 corpsecret / access_token。
    """

    def __init__(
        self,
        *,
        corp_id: str,
        secret: str,
        agent_id: str | int,
        api_base: str = "https://qyapi.weixin.qq.com",
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._corp_id = corp_id
        self._secret = secret
        self._agent_id = _int_or_zero(agent_id)
        self._api_base = api_base.rstrip("/") or "https://qyapi.weixin.qq.com"
        self._timeout = max(1.0, float(timeout_seconds))
        self._client = client
        self._owns_client = client is None
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._lock = asyncio.Lock()
        _install_httpx_redaction()

    async def send_text(self, dest: Mapping[str, Any], text: str) -> None:
        content = _clip_text(text or "", MAX_TEXT_BYTES)
        await self._send_message(dest, "text", {"content": content}, label="发送企业微信消息")

    async def upload_media(self, *, path: str, name: str, mime: str, media_type: str) -> str:
        """上传临时素材，返回 media_id（官方 /cgi-bin/media/upload?type=image|file）。

        官方上限：图片 10MB（JPG/PNG），文件 20MB；频道在调用前已按上限校验。
        """
        token = await self._access_token()
        response = await self._request(
            "POST",
            "/cgi-bin/media/upload",
            params={"access_token": token, "type": media_type},
            files={
                "media": (name, _read_media(path, "企业微信"), mime or "application/octet-stream")
            },
        )
        data = _payload(response, "上传企业微信媒体")
        media_id = str(data.get("media_id") or "")
        if not media_id:
            raise RuntimeError("上传企业微信媒体失败：响应缺少 media_id")
        return media_id

    async def send_image(self, dest: Mapping[str, Any], media_id: str) -> None:
        await self._send_message(dest, "image", {"media_id": media_id}, label="发送企业微信图片")

    async def send_file(self, dest: Mapping[str, Any], media_id: str) -> None:
        await self._send_message(dest, "file", {"media_id": media_id}, label="发送企业微信文件")

    async def send_media(
        self, dest: Mapping[str, Any], *, path: str, name: str, mime: str, kind: str
    ) -> None:
        """按类型走官方上传 + 发送；调用方必须先按平台上限校验。"""
        media_type = "image" if kind == "image" else "file"
        media_id = await self.upload_media(path=path, name=name, mime=mime, media_type=media_type)
        if kind == "image":
            await self.send_image(dest, media_id)
        else:
            await self.send_file(dest, media_id)

    async def _send_message(
        self,
        dest: Mapping[str, Any],
        msgtype: str,
        body: dict[str, Any],
        *,
        label: str,
    ) -> None:
        """按 dest 选择应用消息 / 群聊接口，token 只放查询串（httpx 日志已脱敏）。"""
        path, payload = self._message_target(dest, msgtype, body)
        token = await self._access_token()
        response = await self._request("POST", path, params={"access_token": token}, json=payload)
        _payload(response, label)

    def _message_target(
        self, dest: Mapping[str, Any], msgtype: str, body: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        touser = str(dest.get("touser") or "").strip()
        chatid = str(dest.get("chatid") or "").strip()
        if not touser and not chatid:
            raise RuntimeError("企业微信出站草稿缺少 touser / chatid")
        if chatid:
            return "/cgi-bin/appchat/send", {
                "chatid": chatid,
                "msgtype": msgtype,
                msgtype: body,
            }
        return "/cgi-bin/message/send", {
            "touser": touser,
            "msgtype": msgtype,
            "agentid": self._agent_id,
            msgtype: body,
        }

    async def download_media(self, media_id: str) -> DownloadedMedia:
        """按官方 media/get 下载临时素材（MediaId 有效 3 天）。

        成功时是二进制流；失败时企微返回 ``{errcode, errmsg}`` JSON，交给
        `media_from_response` 识别并抛清晰错误。access_token 只放在查询串，
        httpx 日志已由本模块的过滤器脱敏。
        """
        token = await self._access_token()
        response = await self._send_stream(
            "GET",
            f"{self._api_base}/cgi-bin/media/get",
            params={"access_token": token, "media_id": media_id},
        )
        return await media_from_response(response, label="下载企业微信媒体")

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    async def _access_token(self) -> str:
        """缓存 access_token，提前 60 秒过期，避免边界上刚好失效。"""
        if self._token is not None and time.monotonic() < self._token_expires_at:
            return self._token
        async with self._lock:
            if self._token is not None and time.monotonic() < self._token_expires_at:
                return self._token
            response = await self._request(
                "GET",
                "/cgi-bin/gettoken",
                params={"corpid": self._corp_id, "corpsecret": self._secret},
            )
            data = _payload(response, "获取企业微信 access_token")
            token = str(data.get("access_token") or "")
            if not token:
                raise RuntimeError("企业微信 access_token 响应缺少令牌")
            expires_in = int(data.get("expires_in") or 7200)
            self._token = token
            self._token_expires_at = time.monotonic() + max(60, expires_in - 60)
            return token

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return await self._ensure_client().request(method, f"{self._api_base}{path}", **kwargs)

    async def _send_stream(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """媒体下载保持流式，落盘时由 save_upload 逐块检查 50MB 上限。"""
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


def _int_or_zero(value: str | int | None) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _payload(response: httpx.Response, label: str) -> dict[str, Any]:
    """解析企业微信响应；errcode 非 0 或 HTTP 失败都抛 RuntimeError。"""
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{label}失败：响应不是 JSON（HTTP {response.status_code}）") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{label}失败：响应结构异常（HTTP {response.status_code}）")
    errcode = data.get("errcode")
    if response.status_code >= 400 or errcode not in (0, None):
        errmsg = data.get("errmsg") or ""
        raise RuntimeError(
            f"{label}失败：HTTP {response.status_code} errcode={errcode} {errmsg}"[:500]
        )
    return data


class WeComBotChannel:
    """企业微信正式频道：回调验签/解密、入站建 turn、出站走审批。"""

    def __init__(
        self,
        container: Container,
        *,
        corp_id: str | None = None,
        agent_id: str | None = None,
        secret: str | None = None,
        token: str | None = None,
        encoding_aes_key: str | None = None,
        api_base: str = "https://qyapi.weixin.qq.com",
        bot_id: str | None = None,
        sender: Any | None = None,
        transcriber: Transcriber | None = None,
        turn_wait_seconds: float = 900.0,
    ) -> None:
        self._container = container
        self._transcriber = transcriber
        self._corp_id = (corp_id or "").strip() or None
        self._agent_id = (str(agent_id).strip() if agent_id is not None else "") or None
        self._token = (token or "").strip() or None
        self._encoding_aes_key = (encoding_aes_key or "").strip() or None
        self._bot_id = _parse_uuid(bot_id)
        if (bot_id or "").strip() and self._bot_id is None:
            logger.warning("FLUX_WECOM_BOT_ID 不是合法 UUID，企业微信消息事件将被拒绝")
        if sender is not None:
            self._sender = sender
        elif self._corp_id and secret and self._agent_id:
            self._sender = WeComBotSender(
                corp_id=self._corp_id,
                secret=secret,
                agent_id=self._agent_id,
                api_base=api_base,
            )
        else:
            self._sender = None
        self._background: set[asyncio.Task[Any]] = set()
        #: 同一对话任务同一时刻只挂一张出站审批卡，与飞书频道同口径。
        #: 容器装配时三个频道共用一把锁；单测里的简易容器则退回频道私有锁。
        shared_lock = getattr(container, "outbound_approval_lock", None)
        self._approval_lock = (
            shared_lock if isinstance(shared_lock, asyncio.Lock) else asyncio.Lock()
        )
        self._turn_wait_seconds = max(1.0, float(turn_wait_seconds))

    # --- 回调入口 ---

    @property
    def configured(self) -> bool:
        """token 与 EncodingAESKey 缺一不可；未配置时拒绝一切回调。"""
        return self._token is not None and self._encoding_aes_key is not None

    async def handle_get(self, query: Mapping[str, str]) -> WeComCallbackResult:
        """校验 URL（VerifyURL）：验签 → 解密 echostr → 原样返回明文。"""
        if not self.configured:
            logger.warning("企业微信回调未配置 token / EncodingAESKey，已拒绝校验请求")
            return WeComCallbackResult(400, "未配置企业微信回调 token 或 EncodingAESKey，拒绝处理")

        signature = _query(query, "msg_signature")
        timestamp = _query(query, "timestamp")
        nonce = _query(query, "nonce")
        echostr = _query(query, "echostr")
        if not signature or not timestamp or not nonce or not echostr:
            logger.warning("企业微信校验请求缺少 msg_signature / timestamp / nonce / echostr")
            return WeComCallbackResult(403, "企业微信回调签名参数不完整")

        if not verify_signature(
            token=self._token,
            timestamp=timestamp,
            nonce=nonce,
            encrypted=echostr,
            signature=signature,
        ):
            logger.warning("企业微信校验请求签名不匹配，已拒绝")
            return WeComCallbackResult(403, "企业微信回调签名校验失败")

        try:
            plaintext, _receiveid = decrypt_message(
                self._encoding_aes_key or "",
                echostr,
                expected_receiveid=self._corp_id,
            )
        except WeComCryptoError as exc:
            logger.warning("企业微信 echostr 解密失败：%s", exc)
            return WeComCallbackResult(400, "企业微信 echostr 解密失败")
        logger.info("wecom.verify ok chars=%s", len(plaintext))
        return WeComCallbackResult(200, plaintext)

    async def handle_post(self, body: bytes, query: Mapping[str, str]) -> WeComCallbackResult:
        """接收消息与事件：验签/解密后同步建 turn，立即回 success。"""
        if not self.configured:
            logger.warning("企业微信回调未配置 token / EncodingAESKey，已拒绝消息回调")
            return WeComCallbackResult(400, "未配置企业微信回调 token 或 EncodingAESKey，拒绝处理")

        raw = body or b""
        if len(raw) > _MAX_BODY_BYTES:
            return WeComCallbackResult(400, "企业微信回调体过大")
        outer = _parse_xml(raw)
        if outer is None:
            return WeComCallbackResult(400, "企业微信回调不是合法 XML")
        encrypted = _xml_text(outer, "Encrypt")
        if not encrypted:
            return WeComCallbackResult(400, "企业微信回调缺少 Encrypt 字段")

        signature = _query(query, "msg_signature")
        timestamp = _query(query, "timestamp")
        nonce = _query(query, "nonce")
        if not signature or not timestamp or not nonce:
            logger.warning("企业微信消息回调缺少签名参数")
            return WeComCallbackResult(403, "企业微信回调签名参数不完整")
        if not verify_signature(
            token=self._token,
            timestamp=timestamp,
            nonce=nonce,
            encrypted=encrypted,
            signature=signature,
        ):
            logger.warning("企业微信消息回调签名不匹配，已拒绝")
            return WeComCallbackResult(403, "企业微信回调签名校验失败")

        try:
            plaintext, _receiveid = decrypt_message(
                self._encoding_aes_key or "",
                encrypted,
                expected_receiveid=self._corp_id,
            )
        except WeComCryptoError as exc:
            logger.warning("企业微信消息回调解密失败：%s", exc)
            return WeComCallbackResult(400, "企业微信消息回调解密失败")

        message = _parse_xml(plaintext.encode("utf-8"))
        if message is None:
            return WeComCallbackResult(400, "企业微信消息明文不是合法 XML")

        msg_type = _xml_text(message, "MsgType").lower()
        # 视频 / 位置 / 链接 / 事件等保持 200 忽略，与既有行为一致。
        if msg_type not in {"text", "image", "voice", "file"}:
            logger.info("wecom.inbound.ignored type=%s", msg_type or "unknown")
            return WeComCallbackResult(200, _SUCCESS_BODY)

        message_id = _xml_text(message, "MsgId")
        from_user = _xml_text(message, "FromUserName")
        if not message_id or not from_user:
            return WeComCallbackResult(400, "企业微信消息缺少 MsgId / FromUserName")

        if msg_type == "text":
            text = _xml_text(message, "Content")
            if not text:
                logger.info("wecom.inbound.ignored type=text reason=empty")
                return WeComCallbackResult(200, _SUCCESS_BODY)
            parsed = _InboundMessage(
                message_id=message_id, from_user=from_user, kind="text", text=text
            )
        else:
            parsed = _parse_media_message(message, msg_type, message_id, from_user)
            if isinstance(parsed, WeComCallbackResult):
                return parsed

        # 以下都要知道这条频道归属哪个 Bot，否则 fail-closed。
        bot_id = self._bot_id
        if bot_id is None:
            return WeComCallbackResult(
                400, "未配置合法的 FLUX_WECOM_BOT_ID，拒绝处理企业微信消息事件"
            )

        try:
            bot = await self._container.bots.get(bot_id)
        except NotFoundError:
            logger.warning("企业微信回调指向的 Bot 不存在 bot=%s", bot_id)
            return WeComCallbackResult(404, "FLUX_WECOM_BOT_ID 指向的 Bot 不存在")

        async def _system_reply(text: str) -> None:
            await self._send_system_message(from_user, text)

        if parsed.kind == "text":
            cloud_user_id = await resolve_text_inbound(
                self._container,
                channel="wecom",
                external_user_id=parsed.from_user,
                text=parsed.text,
                reply=_system_reply,
            )
            if cloud_user_id is None:
                return WeComCallbackResult(200, _SUCCESS_BODY)
            files: list[Any] = []
            content = parsed.text
        else:
            cloud_user_id = await resolve_media_inbound(
                self._container,
                channel="wecom",
                external_user_id=parsed.from_user,
                reply=_system_reply,
            )
            if cloud_user_id is None:
                return WeComCallbackResult(200, _SUCCESS_BODY)
            # 重推：turn 已存在就直接回 success，绝不再次下载媒体。
            existing = await self._existing_turn(bot, message_id)
            if existing is not None:
                logger.info(
                    "wecom.inbound.replay bot=%s message=%s turn=%s",
                    bot_id,
                    message_id,
                    existing.id,
                )
                return WeComCallbackResult(200, _SUCCESS_BODY)
            try:
                files, content = await self._load_media(parsed, cloud_user_id=cloud_user_id)
            except ValidationError as exc:
                logger.warning(
                    "企业微信入站媒体被拒绝 message=%s kind=%s reason=%s",
                    message_id,
                    parsed.kind,
                    exc.message,
                )
                return WeComCallbackResult(200, _SUCCESS_BODY)
            except Exception as exc:  # noqa: BLE001 - 下载异常不能拖垮进程
                logger.warning(
                    "企业微信入站媒体下载失败 message=%s kind=%s error=%s",
                    message_id,
                    parsed.kind,
                    safe_error(exc),
                )
                return WeComCallbackResult(502, "企业微信媒体下载失败，请稍后重试")

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
                "企业微信入站建 turn 失败 bot=%s message=%s code=%s",
                bot_id,
                message_id,
                exc.code,
            )
            activity = getattr(self._container, "channel_activity", None)
            if activity is not None:
                activity.record_error("wecom", exc.code, exc.message)
            return WeComCallbackResult(exc.http_status, f"{exc.message}（{exc.code}）")

        activity = getattr(self._container, "channel_activity", None)
        if activity is not None:
            activity.record_receive("wecom")
        if created:
            self._schedule_tracking(bot=bot, turn=turn, touser=from_user, message_id=message_id)
        logger.info(
            "wecom.inbound bot=%s message=%s turn=%s created=%s",
            bot_id,
            message_id,
            turn.id,
            created,
        )
        return WeComCallbackResult(200, _SUCCESS_BODY)

    # --- 审批结果 ---

    async def on_approval_resolved(self, event: str, payload: dict[str, Any]) -> None:
        """审批结果监听：只有「允许」才发送，同一条草稿最多发送一次。

        无论这次解决的是不是企业微信出站卡，只要有决策被解决且当前没有新的
        待决策项，就把排队中的下一条企业微信出站回复提升为审批卡。
        """
        if event != Events.APPROVAL_RESOLVED:
            return
        if payload.get("kind") == WECOM_OUTBOUND_KIND:
            try:
                await self._apply_outbound_result(payload)
            except Exception:  # noqa: BLE001 - 结果处理失败不能阻断后续排队卡片
                logger.exception("企业微信出站审批处理失败 decision=%s", payload.get("decision_id"))
        await self._promote_queued(payload.get("task_id"))

    async def _apply_outbound_result(self, payload: dict[str, Any]) -> None:
        """按审批结果发送或拒绝一条已挂卡的企业微信出站草稿。"""
        meta = payload.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        raw_draft_id = str(meta.get("draft_id") or "").strip()
        if not raw_draft_id:
            logger.warning("企业微信出站审批缺少 draft_id，已跳过")
            return
        try:
            draft_id = uuid.UUID(raw_draft_id)
        except ValueError:
            logger.warning("企业微信出站审批的 draft_id 非法，已跳过 draft=%s", raw_draft_id)
            return

        service = getattr(self._container, "outbound_drafts", None)
        if service is None:  # pragma: no cover - 装配错误，按 fail-closed 跳过
            logger.error("出站草稿服务未装配，审批结果已忽略 draft=%s", raw_draft_id)
            return

        if payload.get("result") != "allowed" or payload.get("chosen") != APPROVAL_ALLOW:
            await service.mark_denied(draft_id)
            logger.info(
                "wecom.outbound.skip decision=%s result=%s draft=%s",
                payload.get("decision_id"),
                payload.get("result"),
                raw_draft_id,
            )
            return

        draft = await service.claim(draft_id)
        if draft is None:
            logger.info("企业微信出站草稿不存在或已被处理 draft=%s", raw_draft_id)
            return
        await self._deliver_claimed_draft(draft)

    async def _deliver_claimed_draft(self, draft: Any) -> None:
        """发送一条已被原子认领的草稿；审批与「无规则自动发送」共用。"""
        service = self._container.outbound_drafts
        dest = draft.dest if isinstance(draft.dest, dict) else {}
        touser = str(dest.get("touser") or "").strip()
        chatid = str(dest.get("chatid") or "").strip()
        if draft.channel != "wecom" or not (touser or chatid):
            await service.mark_failed(draft.id, "channel_or_dest_mismatch")
            logger.error(
                "企业微信出站草稿的频道或目标不匹配 draft=%s channel=%s dest_keys=%s",
                draft.id,
                draft.channel,
                sorted(dest),
            )
            return
        sender = self._sender
        if sender is None:
            # 不是「取走即丢」：标 failed 并保留正文，配置好凭据后仍可看到。
            await service.mark_failed(draft.id, "sender_not_configured")
            logger.error(
                "未配置企业微信应用凭据，草稿无法发送 draft=%s dest_keys=%s",
                draft.id,
                sorted(dest),
            )
            return
        records = attachment_records(draft.attachments)
        # 上传前先按企业微信官方上限校验；超限 / 类型不支持时绝不调用上传接口。
        try:
            files = [resolve_attachment(self._container.settings, record) for record in records]
            for uploaded in files:
                fresh = attachment_record(uploaded)
                validate_channel_attachment(
                    "wecom",
                    name=fresh["name"],
                    mime=fresh["mime"],
                    size=fresh["size"],
                    kind=fresh["kind"],
                )
        except ValidationError as exc:
            await service.mark_failed(draft.id, exc.message)
            logger.warning(
                "企业微信出站附件校验失败 draft=%s files=%s reason=%s",
                draft.id,
                len(records),
                exc.message,
            )
            return
        except Exception as exc:  # noqa: BLE001 - 附件读取失败同样只落 failed
            await service.mark_failed(draft.id, exc)
            logger.exception("企业微信出站附件准备失败 draft=%s files=%s", draft.id, len(records))
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
                "企业微信出站发送失败 draft=%s dest_keys=%s hash=%s chars=%s files=%s",
                draft.id,
                sorted(dest),
                draft.body_hash,
                len(draft.body or ""),
                len(files),
            )
            return
        await service.mark_sent(draft.id)
        logger.info(
            "企业微信出站已发送 draft=%s dest_keys=%s hash=%s chars=%s files=%s",
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

    async def _send_system_message(self, from_user: str, text: str) -> None:
        """绑定提示 / 成功确认等系统消息：直接发送，不走出站审批草稿。"""
        sender = self._sender
        if sender is None:
            logger.warning("企业微信系统消息未发送：未配置应用凭据 user=%s", from_user)
            return
        await sender.send_text({"touser": from_user}, text)

    async def _load_media(
        self, parsed: _InboundMessage, *, cloud_user_id: str | None = None
    ) -> tuple[list[Any], str]:
        """下载一条图片/语音/文件并落盘；缺凭据时给中文说明而不是静默忽略。"""
        label = _KIND_LABELS[parsed.kind]
        sender = self._sender
        if sender is None or not hasattr(sender, "download_media"):
            logger.warning(
                "企业微信入站媒体缺少应用凭据 message=%s kind=%s",
                parsed.message_id,
                parsed.kind,
            )
            return [], f"收到{label}，但未配置企业微信应用凭据，无法下载附件。"

        media = await sender.download_media(parsed.media_id)
        stem = {"image": "image", "voice": "voice", "file": "file"}[parsed.kind]
        ext = f".{parsed.voice_format}" if parsed.kind == "voice" and parsed.voice_format else ""
        name = pick_media_name(
            preferred=parsed.file_name,
            downloaded=media.name,
            mime=media.mime,
            stem=stem,
            ext=ext or (".amr" if parsed.kind == "voice" else ""),
        )
        uploaded = await save_media(
            self._container.settings,
            DownloadedMedia(name=name, mime=media.mime, chunks=media.chunks),
            cloud_user_id=cloud_user_id,
        )
        if parsed.kind == "voice":
            content = parsed.recognition or await transcribe_audio(self._transcriber, uploaded)
        else:
            content = ""
        logger.info(
            "企业微信入站媒体已落盘 message=%s kind=%s upload=%s bytes=%s",
            parsed.message_id,
            parsed.kind,
            uploaded.id,
            uploaded.size,
        )
        return [uploaded], content

    def _schedule_tracking(self, *, bot: Any, turn: Any, touser: str, message_id: str) -> None:
        task = asyncio.create_task(
            self._track_turn(bot=bot, turn=turn, touser=touser, message_id=message_id),
            name="flux-wecom-turn",
        )
        self._background.add(task)

        def _cleanup(done: asyncio.Task[Any]) -> None:
            self._background.discard(done)

        task.add_done_callback(_cleanup)

    async def _track_turn(self, *, bot: Any, turn: Any, touser: str, message_id: str) -> None:
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
                logger.info(
                    "企业微信 turn 未产生可发送回复 turn=%s status=%s", turn.id, final.status
                )
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
                    logger.info("企业微信 turn 回复为空，不生成出站草稿 turn=%s", turn.id)
                    return

                service = self._container.outbound_drafts
                draft = await service.create(
                    channel="wecom",
                    body=text,
                    attachments=attachments,
                    dest={"touser": touser, "message_id": message_id},
                    bot_id=bot.id,
                    task_id=final.task_id,
                    turn_id=final.id,
                    cloud_user_id=getattr(final, "cloud_user_id", None),
                )
                logger.info(
                    "企业微信出站回复已入队 draft=%s turn=%s task=%s hash=%s chars=%s files=%s",
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
            logger.exception("企业微信 turn 跟踪失败 turn=%s", getattr(turn, "id", "?"))

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
            action=ApprovalAction(kind=ACTION_OUTBOUND_MESSAGE, channel="wecom"),
        )

    async def _promote_next_locked(self, task_id: uuid.UUID) -> None:
        """处理排队草稿：无规则直接发送；命中规则才挂审批卡。"""
        service = getattr(self._container, "outbound_drafts", None)
        if service is None:  # pragma: no cover - 装配错误，按 fail-closed 跳过
            return

        for _ in range(_MAX_QUEUE_PROMOTIONS):
            draft = await service.next_queued(channel="wecom", task_id=task_id)
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
                logger.exception(
                    "企业微信排队出站审批提升失败 draft=%s task=%s", claimed.id, task_id
                )
            return

    async def _raise_outbound_approval(
        self, draft: Any, decision_id: str, match: ApprovalRuleMatch
    ) -> None:
        """为一条已认领的草稿挂审批卡；文案只谈发送，不谈终端命令。"""
        dest = draft.dest if isinstance(draft.dest, dict) else {}
        touser = str(dest.get("touser") or "").strip()
        message_id = str(dest.get("message_id") or "").strip()
        bot_id = str(draft.bot_id) if draft.bot_id is not None else ""
        turn_id = str(draft.turn_id) if draft.turn_id is not None else ""
        records = attachment_records(draft.attachments)
        detail = render_outbound_detail(
            f"准备发送到企业微信成员 {touser}（消息 {message_id}）：",
            draft.body,
            records,
        )
        detail = f"{detail}\n\n触发规则：{match.rule.description or match.reason}"
        await raise_approval(
            self._container,
            task_id=draft.task_id,
            run_id=None,
            kind=WECOM_OUTBOUND_KIND,
            decision_id=decision_id,
            title="企业微信回复等待你确认",
            detail=detail,
            meta={
                "channel": "wecom",
                "draft_id": str(draft.id),
                "touser": touser,
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
            "企业微信出站审批已创建 draft=%s task=%s hash=%s chars=%s files=%s approval_rule=%s",
            draft.id,
            draft.task_id,
            draft.body_hash,
            len(draft.body or ""),
            len(records),
            match.rule.id,
        )


def _query(query: Mapping[str, str], name: str) -> str:
    value = query.get(name)
    return value.strip() if isinstance(value, str) else ""


def _parse_uuid(value: str | None) -> uuid.UUID | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _parse_xml(raw: bytes) -> ET.Element | None:
    """解析回调 XML；拒绝 DTD / 实体声明，避免实体膨胀消耗。"""
    lowered = raw.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        return None
    try:
        return ET.fromstring(raw)
    except (ET.ParseError, ValueError, LookupError):
        return None


def _xml_text(root: ET.Element, tag: str) -> str:
    value = root.findtext(tag)
    if not isinstance(value, str):
        return ""
    # 企业微信 XML 常用 CDATA；ElementTree 已还原为文本，这里只去首尾空白。
    return value.strip()


def _parse_media_message(
    message: ET.Element,
    msg_type: str,
    message_id: str,
    from_user: str,
) -> _InboundMessage | WeComCallbackResult:
    """解析 image / voice / file 消息；缺 MediaId 属于永久问题，确认后忽略。"""
    media_id = _xml_text(message, "MediaId")
    if not media_id:
        logger.info("wecom.inbound.ignored type=%s reason=missing_media_id", msg_type or "unknown")
        return WeComCallbackResult(200, _SUCCESS_BODY)
    if msg_type == "image":
        return _InboundMessage(
            message_id=message_id,
            from_user=from_user,
            kind="image",
            media_id=media_id,
        )
    if msg_type == "voice":
        return _InboundMessage(
            message_id=message_id,
            from_user=from_user,
            kind="voice",
            media_id=media_id,
            voice_format=_voice_format(_xml_text(message, "Format")),
            recognition=_xml_text(message, "Recognition"),
        )
    return _InboundMessage(
        message_id=message_id,
        from_user=from_user,
        kind="file",
        media_id=media_id,
        file_name=_xml_text(message, "FileName"),
    )


def _voice_format(raw: str) -> str:
    """语音格式只保留安全扩展名（amr / speex），避免拼进文件名时带奇怪字符。"""
    value = raw.strip().lower().lstrip(".")
    return value if re.fullmatch(r"[a-z0-9]{1,8}", value) else "amr"


def _clip_text(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节数截断，且不把多字节字符切成半个。"""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", "ignore")
