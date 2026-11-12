"""云桌面会话票据（per-bot desktop isolation）。

手机只拿到「某一台 Bot 的桌面会话」所需的最小信息。桌面 profile 由服务端
计算为 ``bot`` + Bot UUID 的前 8 个十六进制字符——同一台 Bot 的桌面配置在不同
设备/重复登录之间保持一致，不同 Bot 之间彼此隔离。

票据是紧凑的 HMAC-SHA256 签名串：

    base64url(JSON payload) + "." + base64url(HMAC-SHA256(payload_bytes, secret))

payload 至少包含 ``bot_id`` / ``profile`` / ``principal``（设备会话另有
``device_id``）/ ``exp``（Unix 秒）/ ``nonce``。桌面网关应只信任能验签且未过期的
票据，而不是把长期 ``gateway_token`` 发给手机。验签方式见 README「云桌面入口与会话」。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from flux.config import Settings
from flux.core.desktop.resolve import DesktopEndpoints, resolve_desktop_endpoints


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _now_seconds(now: datetime | int | float | None) -> int:
    if now is None:
        return int(time.time())
    if isinstance(now, datetime):
        moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        return int(moment.timestamp())
    return int(now)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def desktop_profile(bot_id: str | uuid.UUID) -> str:
    """与 App 的 ``BotDesktop.profile()`` 一致：bot + Bot UUID 前 8 位 hex。"""
    key = uuid.UUID(str(bot_id)) if not isinstance(bot_id, uuid.UUID) else bot_id
    return f"bot{key.hex[:8]}"


def _sign(payload_bytes: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).digest()


def issue_desktop_ticket(
    *,
    bot_id: str | uuid.UUID,
    profile: str,
    principal: str,
    secret: str | None,
    ttl_seconds: int,
    device_id: str | None = None,
    now: datetime | int | float | None = None,
) -> str | None:
    """签发桌面票据；未配置密钥时返回 None。"""
    if not secret:
        return None
    payload: dict[str, Any] = {
        "bot_id": str(bot_id),
        "profile": profile,
        "principal": principal,
        "exp": _now_seconds(now) + max(1, int(ttl_seconds)),
        "nonce": secrets.token_urlsafe(16),
    }
    if device_id:
        payload["device_id"] = str(device_id)
        payload["principal"] = "device"
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = _b64url_encode(body)
    signature = _b64url_encode(_sign(encoded.encode("ascii"), secret))
    return f"{encoded}.{signature}"


def verify_desktop_ticket(
    ticket: str | None,
    secret: str | None,
    now: datetime | int | float | None = None,
    *,
    expected_bot_id: str | uuid.UUID | None = None,
    expected_profile: str | None = None,
) -> dict[str, Any] | None:
    """验签并校验过期时间；合法返回 payload，否则返回 None。

    桌面网关应按同一规则校验：拆成两段 → 用 secret 对第一段重算 HMAC-SHA256 →
    常量时间比较 → 解 base64url JSON → 检查 ``exp`` 大于当前 Unix 秒。

    网关还必须把票据**绑定到本次请求的目标**：传入 ``expected_bot_id`` /
    ``expected_profile`` 后，票据里的对应声明不匹配同样返回 None。否则一张 A Bot
    的票据可以被拿去打开 B Bot 的桌面（票据本身是合法的，只是用错了地方）。
    """
    if not ticket or not secret or "." not in ticket:
        return None
    encoded, signature = ticket.split(".", 1)
    if not encoded or not signature:
        return None
    try:
        expected = _sign(encoded.encode("ascii"), secret)
        provided = _b64url_decode(signature)
    except (ValueError, UnicodeEncodeError):
        return None
    if not hmac.compare_digest(expected, provided):
        return None
    try:
        payload = json.loads(_b64url_decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        expires = int(payload.get("exp", 0))
    except (TypeError, ValueError):
        return None
    if expires <= _now_seconds(now):
        return None
    if expected_bot_id is not None and str(payload.get("bot_id")) != str(expected_bot_id):
        return None
    if expected_profile is not None and str(payload.get("profile")) != expected_profile:
        return None
    return payload


def build_desktop_session(
    settings: Settings,
    *,
    bot_id: str | uuid.UUID,
    principal: dict[str, Any] | None,
    now: datetime | None = None,
    endpoints: DesktopEndpoints | None = None,
) -> dict[str, Any]:
    """构造桌面会话响应（不含 503 判定，调用方负责检查是否已配置）。

    ``endpoints`` 由调用方传入 ``DesktopService.resolve()`` 的结果，确保会话里
    的网关 / 网桥地址与 ``GET /api/v1/desktop`` 完全一致（包括刚轮换的
    ``desktop.json``）。不传时退回纯 env 别名解析，方便脚本与单元测试直接调用。
    """
    moment = now or _utcnow()
    resolved = endpoints or resolve_desktop_endpoints(settings)
    profile = desktop_profile(bot_id)
    principal_data = principal or {"kind": "local"}
    kind = str(principal_data.get("kind") or "local")
    device_id = principal_data.get("device_id")
    ttl = max(1, int(settings.desktop_ticket_ttl_seconds))
    expires_at = int(moment.timestamp()) + ttl
    ticket = issue_desktop_ticket(
        bot_id=bot_id,
        profile=profile,
        principal=kind,
        secret=settings.desktop_ticket_secret,
        ttl_seconds=ttl,
        device_id=str(device_id) if device_id else None,
        now=moment,
    )
    payload: dict[str, Any] = {
        "host": resolved.gateway or None,
        "bridge": resolved.bridge or None,
        "profile": profile,
        "ticket": ticket,
        "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
    }
    if settings.desktop_send_gateway_token and resolved.has_long_lived_credentials:
        payload["gateway_token"] = resolved.gateway_token
        payload["bridge_key"] = resolved.bridge_key
    return payload
