"""正式飞书频道回调入口（Bot 入站）。

这里**不**走 `require_rest_auth`：飞书开放平台没有本机令牌，安全性由回调体里的
verification token、Encrypt Key 配置后的签名、以及加密体本身把关（未配置时
fail-closed）。响应必须保持飞书约定的原始 JSON，不能套 `ok()` 信封。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from flux.api.deps import get_container
from flux.container import Container

router = APIRouter(prefix="/channels", tags=["channels"])


@router.post("/feishu")
async def feishu_callback(
    request: Request,
    container: Container = Depends(get_container),
) -> JSONResponse:
    """飞书事件回调：验签/解密 → 校验 token → 建 Bot turn 或回 challenge。"""
    body = await request.body()
    result = await container.feishu_bot.handle(body, request.headers)
    return JSONResponse(result.payload, status_code=result.status_code)
