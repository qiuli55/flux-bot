"""API 依赖注入。"""

from __future__ import annotations

from fastapi import Request

from flux.config import Settings
from flux.container import Container
from flux.errors import AIOSError


def get_container(request: Request) -> Container:
    container = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - 装配错误，属编程错误
        raise AIOSError("应用容器未初始化", code="container_missing")
    return container


def get_settings_dep(request: Request) -> Settings:
    return get_container(request).settings
