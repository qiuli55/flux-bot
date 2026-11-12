"""外部例程触发：HMAC 验签、事件名归一与事件总线投递。

两个入口共用同一套签名规则（`sha256=<hex>`，对原始请求体做 HMAC-SHA256）：

- 通用 Webhook：`X-Flux-Signature`，密钥 `FLUX_ROUTINE_WEBHOOK_SECRET`；
  事件名限定在 `webhook.received` / `webhook.<slug>` 前缀族内。
- GitHub Webhook：`X-Hub-Signature-256`，密钥 `FLUX_GITHUB_WEBHOOK_SECRET`；
  `X-GitHub-Event` 映射为 `github.<event>`。

验签通过后事件统一走 `EventBus.publish`，由 `BotService.handle_event` 消费。
这样将来把进程内总线换成 Redis pub/sub 时，外部触发不需要另写投递链路。
密钥未配置时一律 fail-closed（503），绝不存在“忘了配就是开放”。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from flux.core.event.bus import WILDCARD, Events
from flux.enums import BotStatus
from flux.errors import NotFoundError
from flux.logging import get_logger

if TYPE_CHECKING:
    from flux.container import Container

logger = get_logger(__name__)

#: 请求头（Starlette 查找请求头时不区分大小写）
SIGNATURE_HEADER = "X-Flux-Signature"
GITHUB_SIGNATURE_HEADER = "X-Hub-Signature-256"
GITHUB_EVENT_HEADER = "X-GitHub-Event"
GITHUB_DELIVERY_HEADER = "X-GitHub-Delivery"

#: 事件名前缀族
WEBHOOK_EVENT_PREFIX = "webhook."
GITHUB_EVENT_PREFIX = "github."
WEBHOOK_RECEIVED_EVENT = Events.WEBHOOK_RECEIVED
GITHUB_PING_EVENT = f"{GITHUB_EVENT_PREFIX}ping"

#: 载荷里的内部定向键（不参与摘要，也不属于调用方业务字段）
TARGET_BOT_KEY = "_flux_target_bot_id"
TARGET_ROUTINE_KEY = "_flux_target_routine_id"

#: 单个事件名与摘要的长度上限
_MAX_EVENT_CHARS = 64
_MAX_SUMMARY_CHARS = 512
#: GitHub delivery 去重缓存的容量（进程内即可，重启后重复投递最坏是多触发一次）
_DELIVERY_CACHE_MAX = 512

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_GITHUB_EVENT_RE = re.compile(r"^[a-z0-9_]+$")
_HEX_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SHA256_PREFIX = "sha256="

#: Events 里声明的静态事件名
_STATIC_EVENTS = frozenset(
    value for value in vars(Events).values() if isinstance(value, str) and value != WILDCARD
)

#: 摘要优先取这些可读字段，取不到再退回 JSON 片段
_SUMMARY_KEYS = ("summary", "description", "title", "name", "path", "batch_id", "task_id")


@dataclass(frozen=True, slots=True)
class HookResult:
    """一个 Webhook 处理结果；路由层只负责原样返回。"""

    status_code: int
    payload: dict[str, Any]


def sign_body(secret: str, body: bytes) -> str:
    """按 GitHub 兼容格式生成签名（测试与调用方自检可用）。"""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{_SHA256_PREFIX}{digest}"


def verify_body_signature(secret: str | None, body: bytes, header: str | None) -> bool:
    """校验 `sha256=<hex>` 签名；密钥为空或格式不对都返回 False（fail-closed）。"""
    if not secret or not secret.strip() or not header:
        return False
    value = header.strip()
    if not value.startswith(_SHA256_PREFIX):
        return False
    provided = value[len(_SHA256_PREFIX) :]
    if not _HEX_SHA256_RE.fullmatch(provided):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided.lower())


def is_webhook_event_name(event: str) -> bool:
    """`webhook.received` 或 `webhook.<slug>`；slug 有字符集与长度约束。"""
    if not event.startswith(WEBHOOK_EVENT_PREFIX):
        return False
    slug = event[len(WEBHOOK_EVENT_PREFIX) :]
    if not slug or len(slug) > _MAX_EVENT_CHARS:
        return False
    return bool(_SLUG_RE.fullmatch(slug))


def is_github_event_name(event: str) -> bool:
    """`github.<event>`，event 对应官方 X-GitHub-Event 的字符集。"""
    if not event.startswith(GITHUB_EVENT_PREFIX):
        return False
    name = event[len(GITHUB_EVENT_PREFIX) :]
    if not name or len(name) > _MAX_EVENT_CHARS:
        return False
    return bool(_GITHUB_EVENT_RE.fullmatch(name))


def is_external_event_name(event: str) -> bool:
    return is_webhook_event_name(event) or is_github_event_name(event)


def is_known_event(event: str) -> bool:
    """Bot / 例程可订阅的事件名：Events 常量或受控的外部触发前缀族。"""
    return event in _STATIC_EVENTS or is_external_event_name(event)


def github_event_name(header: str | None) -> str | None:
    """把 `X-GitHub-Event` 映射成 `github.<event>`；不合法返回 None。"""
    candidate = (header or "").strip()
    if not candidate or len(candidate) > _MAX_EVENT_CHARS:
        return None
    if not _GITHUB_EVENT_RE.fullmatch(candidate):
        return None
    return f"{GITHUB_EVENT_PREFIX}{candidate}"


def summarize_payload(payload: dict[str, Any], *, limit: int = _MAX_SUMMARY_CHARS) -> str:
    """取一个短摘要：优先可读字段，否则退到紧凑 JSON。"""
    for key in _SUMMARY_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:limit]
    if not payload:
        return ""
    try:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        text = str(payload)
    return text[:limit]


def _nested_text(payload: dict[str, Any], *keys: str) -> str:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return current.strip() if isinstance(current, str) else ""


def summarize_github_payload(event: str, payload: dict[str, Any]) -> str:
    """GitHub 载荷的可读摘要（不解析嵌套太深，够日志与指令识别即可）。"""
    pieces: list[str] = []
    repo = _nested_text(payload, "repository", "full_name")
    if repo:
        pieces.append(repo)
    action = payload.get("action")
    if isinstance(action, str) and action.strip():
        pieces.append(action.strip())
    sender = _nested_text(payload, "sender", "login")
    if sender:
        pieces.append(f"by {sender}")

    number = payload.get("number")
    if isinstance(number, int):
        pieces.append(f"#{number}")
    title = ""
    for container_key in ("pull_request", "issue", "release", "workflow_run"):
        title = _nested_text(payload, container_key, "title")
        if title:
            break
    if not title:
        title = _nested_text(payload, "release", "name") or _nested_text(
            payload, "workflow_run", "name"
        )
    if title:
        pieces.append(title)

    if event == Events.GITHUB_PUSH:
        ref = payload.get("ref")
        if isinstance(ref, str) and ref.strip():
            pieces.append(ref.strip())
        commits = payload.get("commits")
        if isinstance(commits, list) and commits:
            pieces.append(f"{len(commits)} commits")
    elif event == Events.GITHUB_RELEASE:
        tag = payload.get("release")
        tag_name = _nested_text(tag, "tag_name") if isinstance(tag, dict) else ""
        if tag_name:
            pieces.append(tag_name)
    elif event == Events.GITHUB_WORKFLOW_RUN:
        conclusion = _nested_text(payload, "workflow_run", "conclusion")
        if conclusion:
            pieces.append(conclusion)

    if pieces:
        return " ".join(pieces)[:_MAX_SUMMARY_CHARS]
    return summarize_payload(payload)


def _error(status_code: int, code: str, message: str) -> HookResult:
    return HookResult(status_code, {"error": message, "code": code})


def _parse_uuid(value: Any) -> uuid.UUID | None:
    """可选的 UUID 定向参数；空值表示未指定，格式非法抛 ValueError。"""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("target id must be a UUID string")
    return uuid.UUID(value.strip())


class WebhookTriggerService:
    """Webhook 验签、定向匹配与事件投递。"""

    def __init__(self, container: Container) -> None:
        self._container = container
        #: GitHub delivery id 的进程内短缓存，避免平台重投导致重复触发
        self._seen_deliveries: set[str] = set()
        self._delivery_order: deque[str] = deque()

    # --- 通用 Webhook ---

    async def handle_routine_hook(self, body: bytes, signature: str | None) -> HookResult:
        secret = self._routine_secret()
        if secret is None:
            logger.warning("FLUX_ROUTINE_WEBHOOK_SECRET 未配置，拒绝例程 Webhook")
            return _error(503, "webhook_not_configured", "routine webhook secret is not configured")
        if not verify_body_signature(secret, body, signature):
            logger.warning("例程 Webhook 签名校验失败")
            return _error(401, "invalid_signature", "invalid webhook signature")

        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error(400, "invalid_json", "request body must be valid JSON")
        if not isinstance(data, dict):
            return _error(400, "invalid_json", "request body must be a JSON object")

        event = data.get("event")
        if not isinstance(event, str) or not is_webhook_event_name(event):
            return _error(
                400,
                "invalid_event",
                "event must be webhook.received or webhook.<slug>",
            )
        payload = data.get("payload", {})
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return _error(400, "invalid_payload", "payload must be a JSON object")

        try:
            target_bot = _parse_uuid(data.get("bot_id"))
            target_routine = _parse_uuid(data.get("routine_id"))
        except ValueError:
            return _error(400, "invalid_target", "bot_id and routine_id must be UUIDs")

        try:
            bots_matched, routines_matched = await self._match_counts(
                event, target_bot, target_routine
            )
        except NotFoundError:
            return _error(404, "target_not_found", "target bot or routine was not found")

        published = dict(payload)
        summary = summarize_payload(published)
        if summary and not isinstance(published.get("summary"), str):
            published["summary"] = summary
        if target_bot is not None:
            published[TARGET_BOT_KEY] = str(target_bot)
        if target_routine is not None:
            published[TARGET_ROUTINE_KEY] = str(target_routine)

        await self._container.bus.publish(event, published)
        logger.info(
            "例程 Webhook 已接受 event=%s bots=%s routines=%s",
            event,
            bots_matched,
            routines_matched,
        )
        return HookResult(
            200,
            {
                "accepted": True,
                "event": event,
                "bots_matched": bots_matched,
                "routines_matched": routines_matched,
            },
        )

    # --- GitHub Webhook ---

    async def handle_github_hook(
        self,
        body: bytes,
        signature: str | None,
        event_header: str | None,
        delivery_header: str | None,
    ) -> HookResult:
        secret = self._github_secret()
        if secret is None:
            logger.warning("FLUX_GITHUB_WEBHOOK_SECRET 未配置，拒绝 GitHub Webhook")
            return _error(503, "webhook_not_configured", "github webhook secret is not configured")
        if not verify_body_signature(secret, body, signature):
            logger.warning("GitHub Webhook 签名校验失败")
            return _error(401, "invalid_signature", "invalid webhook signature")

        delivery = (delivery_header or "").strip()
        event = github_event_name(event_header)
        if event is None:
            # 签名已通过，属于平台侧数据问题：回 2xx 避免 GitHub 无限重试。
            logger.warning("GitHub Webhook 缺少合法 X-GitHub-Event（delivery=%s）", delivery)
            return HookResult(
                200,
                {"accepted": False, "ignored": True, "error": "invalid event header"},
            )
        if event == GITHUB_PING_EVENT:
            logger.info("GitHub Webhook ping（delivery=%s）", delivery)
            return HookResult(200, {"accepted": True, "event": event, "ignored": True})
        if delivery and not self._remember_delivery(delivery):
            logger.info("GitHub Webhook 重复投递 event=%s delivery=%s", event, delivery)
            return HookResult(
                200,
                {
                    "accepted": True,
                    "event": event,
                    "duplicate": True,
                    "bots_matched": 0,
                    "routines_matched": 0,
                },
            )

        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # 签名已通过：不因载荷格式让 GitHub 重试。
            logger.warning("GitHub Webhook 载荷不是合法 JSON（delivery=%s）", delivery)
            return HookResult(
                200,
                {"accepted": False, "event": event, "ignored": True, "error": "invalid JSON body"},
            )
        payload = data if isinstance(data, dict) else {}

        published = dict(payload)
        summary = summarize_github_payload(event, published)
        if summary:
            published["summary"] = summary

        try:
            bots_matched, routines_matched = await self._match_counts(event, None, None)
            await self._container.bus.publish(event, published)
        except Exception:  # noqa: BLE001 - 签名已通过，应用层失败不应让平台重试
            logger.exception("GitHub Webhook 投递失败 event=%s delivery=%s", event, delivery)
            return HookResult(
                200,
                {"accepted": False, "event": event, "error": "dispatch failed"},
            )

        logger.info(
            "GitHub Webhook 已接受 event=%s delivery=%s bots=%s routines=%s",
            event,
            delivery,
            bots_matched,
            routines_matched,
        )
        return HookResult(
            200,
            {
                "accepted": True,
                "event": event,
                "bots_matched": bots_matched,
                "routines_matched": routines_matched,
            },
        )

    # --- 内部 ---

    def _routine_secret(self) -> str | None:
        secret = (self._container.settings.routine_webhook_secret or "").strip()
        return secret or None

    def _github_secret(self) -> str | None:
        secret = (self._container.settings.github_webhook_secret or "").strip()
        return secret or None

    def _remember_delivery(self, delivery_id: str) -> bool:
        """返回 True 表示首次见到；False 表示重复投递。"""
        if delivery_id in self._seen_deliveries:
            return False
        self._seen_deliveries.add(delivery_id)
        self._delivery_order.append(delivery_id)
        while len(self._delivery_order) > _DELIVERY_CACHE_MAX:
            oldest = self._delivery_order.popleft()
            self._seen_deliveries.discard(oldest)
        return True

    async def _match_counts(
        self,
        event: str,
        target_bot: uuid.UUID | None,
        target_routine: uuid.UUID | None,
    ) -> tuple[int, int]:
        """响应里的匹配数：只数「会被这次投递考虑」的 Bot 与启用例程。"""
        bots = self._container.bots
        if target_routine is not None:
            routine = await bots.get_routine(target_routine)
            if target_bot is not None and routine.bot_id != target_bot:
                raise NotFoundError(
                    f"例程 {target_routine} 不属于 Bot {target_bot}",
                    details={"routine_id": str(target_routine), "bot_id": str(target_bot)},
                )
            bot = await bots.get(routine.bot_id)
            eligible = bool(routine.enabled) and bot.status == BotStatus.ACTIVE.value
            return (1, 1) if eligible else (0, 0)
        if target_bot is not None:
            bot = await bots.get(target_bot)
            return (1, 0) if bot.status == BotStatus.ACTIVE.value else (0, 0)

        active = await bots.list(status=BotStatus.ACTIVE.value, limit=200)
        bot_count = sum(1 for bot in active if event in (bot.event_triggers or []))
        routine_count = 0
        for bot in active:
            for routine in await bots.list_routines(bot.id):
                if routine.enabled and event in (routine.event_triggers or []):
                    routine_count += 1
        return bot_count, routine_count
