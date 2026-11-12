"""按 Bot 隔离的云桌面会话票据。"""

from flux.core.desktop.tickets import (
    build_desktop_session,
    desktop_profile,
    issue_desktop_ticket,
    verify_desktop_ticket,
)

__all__ = [
    "build_desktop_session",
    "desktop_profile",
    "issue_desktop_ticket",
    "verify_desktop_ticket",
]
