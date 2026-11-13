"""制品页面（artifact page）：Bot 产出的可分享静态内容。"""

from __future__ import annotations

from flux.core.pages.repository import ArtifactPageRepository
from flux.core.pages.service import (
    MAX_BODY_BYTES,
    MAX_SLUG_CHARS,
    MAX_TITLE_CHARS,
    ArtifactPageService,
)

__all__ = [
    "MAX_BODY_BYTES",
    "MAX_SLUG_CHARS",
    "MAX_TITLE_CHARS",
    "ArtifactPageRepository",
    "ArtifactPageService",
]
