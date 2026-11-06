"""WebSocket 鉴权：与 REST 同一套「单用户」边界，但不依赖 Request 依赖。

终端 PTY（`api/v1/terminal.py`）与 LSP 网关（`api/v1/ide.py`）共用这里的两个判断，
避免两处各写一份、日后改一处漏一处。
"""

from __future__ import annotations

import hmac
from urllib.parse import urlparse

from fastapi import WebSocket

from flux.api.auth import FORWARDED_HEADERS, LOOPBACK_HOSTS


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
    """与 REST 相同的鉴权边界：本机直连放行，其余需 Bearer / Cookie。"""

    settings = websocket.app.state.container.settings
    expected = (settings.auth_token or "").strip()
    if not expected:
        return True

    client = websocket.client
    has_forwarded = any(websocket.headers.get(name) for name in FORWARDED_HEADERS)
    if client is not None and client.host in LOOPBACK_HOSTS and not has_forwarded:
        return True

    authorization = websocket.headers.get("authorization", "")
    provided = authorization[7:].strip() if authorization.lower().startswith("bearer ") else None
    if not provided:
        provided = websocket.cookies.get("flux_auth_token")
    return bool(provided) and hmac.compare_digest(provided, expected)
