"""外部例程触发的 HTTP 入口（HMAC 签名，不走 require_rest_auth）。

这两个端点面向外部系统（CI / GitHub / 自建 Webhook），没有本机令牌：

- 通用入口：`POST /api/v1/hooks/routines`，校验 `X-Flux-Signature`；
- GitHub 入口：`POST /api/v1/hooks/github`，校验官方 `X-Hub-Signature-256`。

响应是给调用方 / GitHub 的原始 JSON，不套 `ok()` 信封。签名失败保持 4xx；
签名通过后的应用层问题按 2xx 确认，避免平台无限重试（`supervisor_webhook.py`
同一口径）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from flux.api.deps import get_container
from flux.container import Container
from flux.core.bot.webhook_triggers import (
    GITHUB_DELIVERY_HEADER,
    GITHUB_EVENT_HEADER,
    GITHUB_SIGNATURE_HEADER,
    SIGNATURE_HEADER,
)

router = APIRouter(prefix="/hooks", tags=["hooks"])


@router.post("/routines")
async def routine_webhook(
    request: Request,
    container: Container = Depends(get_container),
) -> JSONResponse:
    """通用签名 Webhook：验签 → 校验事件名 → 定向匹配 → 投递事件总线。"""
    body = await request.body()
    result = await container.webhook_triggers.handle_routine_hook(
        body, request.headers.get(SIGNATURE_HEADER)
    )
    return JSONResponse(result.payload, status_code=result.status_code)


@router.post("/github")
async def github_webhook(
    request: Request,
    container: Container = Depends(get_container),
) -> JSONResponse:
    """GitHub Webhook：官方 SHA-256 验签 → `github.<event>` → 投递事件总线。"""
    body = await request.body()
    result = await container.webhook_triggers.handle_github_hook(
        body,
        request.headers.get(GITHUB_SIGNATURE_HEADER),
        request.headers.get(GITHUB_EVENT_HEADER),
        request.headers.get(GITHUB_DELIVERY_HEADER),
    )
    return JSONResponse(result.payload, status_code=result.status_code)
