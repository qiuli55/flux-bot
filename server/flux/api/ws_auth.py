"""WebSocket 鉴权：与 REST 同一套「单用户」边界，但不依赖 Request 依赖。

终端 PTY（`api/v1/terminal.py`）与 LSP 网关（`api/v1/ide.py`）共用这里的两个判断，
避免两处各写一份、日后改一处漏一处。
"""

from __future__ import annotations

import hmac
from urllib.parse import urlparse

from fastapi import WebSocket

from flux.api.auth import FORWARDED_HEADERS, LOOPBACK_HOSTS, parse_cloud_access_token


def _set_ws_principal(websocket: WebSocket, kind: str, **extra: object) -> None:
    """与 REST 的 `request.state.principal` 保持同一形状。"""
    websocket.state.principal = {"kind": kind, **extra}


def same_origin_websocket(websocket: WebSocket) -> bool:
    """拒绝与 Host 不同源的浏览器 WebSocket。"""

    origin = websocket.headers.get("origin")
    if not origin:
        # 非浏览器客户端通常不带 Origin；放行，桌面端 / CLI 无需伪装成浏览器。
        return True
    parsed = urlparse(origin)
    host = websocket.headers.get("host", "")
    return parsed.netloc == host


def authenticated_websocket(websocket: WebSocket) -> bool:
    """与 REST 相同的鉴权边界：云登录态 / 本机直连 / Bearer / Cookie。

    顺序与 `flux.api.auth.require_rest_auth` 对齐：先认账号云 access token，
    这样只登录了账号云、没拿 `FLUX_AUTH_TOKEN` 的用户也能用 WS（终端 / LSP），
    不会被"配置了本机令牌"误伤。设备令牌需要异步查库，暂不在 WS 上支持，
    留作后续阶段的小改动。
    """

    settings = websocket.app.state.container.settings

    authorization = websocket.headers.get("authorization", "")
    provided = authorization[7:].strip() if authorization.lower().startswith("bearer ") else None
    if not provided:
        provided = websocket.cookies.get("flux_auth_token")

    cloud_payload = parse_cloud_access_token(settings, provided)
    if cloud_payload is not None:
        _set_ws_principal(websocket, "cloud", cloud_user_id=cloud_payload["sub"])
        return True

    expected = (settings.auth_token or "").strip()
    if not expected:
        _set_ws_principal(websocket, "local")
        return True

    client = websocket.client
    has_forwarded = any(websocket.headers.get(name) for name in FORWARDED_HEADERS)
    if client is not None and client.host in LOOPBACK_HOSTS and not has_forwarded:
        _set_ws_principal(websocket, "local")
        return True

    if bool(provided) and hmac.compare_digest(provided, expected):
        _set_ws_principal(websocket, "admin")
        return True
    return False
