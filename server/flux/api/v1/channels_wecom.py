"""正式企业微信（WeCom）频道回调入口（Bot 入站）。

这里**不**走 `require_rest_auth`：企业微信服务器没有本机令牌，安全性由回调
token、EncodingAESKey 签名与密文本身把关（未配置时 fail-closed）。响应必须保持
企业微信约定的纯文本（GET 返回明文 echostr，POST 返回 ``success``），不能套
`ok()` 信封。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from flux.api.deps import get_container
from flux.container import Container

router = APIRouter(prefix="/channels", tags=["channels"])


@router.get("/wecom")
async def wecom_verify_url(
    request: Request,
    container: Container = Depends(get_container),
) -> PlainTextResponse:
    """企业微信 VerifyURL：验签并解密 echostr，原样返回明文。"""
    result = await container.wecom_bot.handle_get(request.query_params)
    return PlainTextResponse(
        result.body,
        status_code=result.status_code,
        media_type=result.media_type,
        headers=result.headers,
    )


@router.post("/wecom")
async def wecom_callback(
    request: Request,
    container: Container = Depends(get_container),
) -> PlainTextResponse:
    """企业微信消息与事件回调：验签/解密 → 建 Bot turn，立即回 success。"""
    body = await request.body()
    result = await container.wecom_bot.handle_post(body, request.query_params)
    return PlainTextResponse(
        result.body,
        status_code=result.status_code,
        media_type=result.media_type,
        headers=result.headers,
    )
