"""Bot 桌面（Hermes 云电脑）入口参数与按 Bot 隔离的会话票据。

`DesktopService` 负责读取公开的 `desktop.json` 隧道地址并拼装入口参数
（`GET /api/v1/desktop`）；票据助手负责为单台 Bot 签发短期 HMAC 桌面票据
（`POST /api/v1/bots/{bot_id}/desktop/session`）。两者共用 `resolve` 里的
地址/凭据别名解析，避免桌面网关地址在两个接口之间漂移。
"""

from flux.core.desktop.resolve import DesktopEndpoints, resolve_desktop_endpoints
from flux.core.desktop.service import DesktopService
from flux.core.desktop.tickets import (
    build_desktop_session,
    desktop_profile,
    issue_desktop_ticket,
    verify_desktop_ticket,
)

__all__ = [
    "DesktopEndpoints",
    "DesktopService",
    "build_desktop_session",
    "desktop_profile",
    "issue_desktop_ticket",
    "resolve_desktop_endpoints",
    "verify_desktop_ticket",
]
