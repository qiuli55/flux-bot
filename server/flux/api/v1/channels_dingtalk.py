"""正式钉钉频道回调入口（HTTP 事件订阅入站）。

这里**不**走 `require_rest_auth`：钉钉开放平台没有本机令牌，安全性由查询串里的
签名 + AES 加密体 + 后台配置的 token / EncodingAESKey 把关（未配置时 fail-closed）。
响应必须保持钉钉约定的加密 JSON（密文 ``"success"``），不能套 `ok()` 信封。

官方 HTTP 推送只使用 POST（``check_url`` 也是 POST 事件），因此不注册 GET。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from flux.api.deps import get_container
from flux.container import Container

router = APIRouter(prefix="/channels", tags=["channels"])


@router.post("/dingtalk")
async def dingtalk_callback(
    request: Request,
    container: Container = Depends(get_container),
) -> JSONResponse:
    """钉钉事件回调：验签/解密 → check_url 或建 Bot turn → 回加密 success。"""
    body = await request.body()
    result = await container.dingtalk_bot.handle(
        body,
        dict(request.query_params),
        request.headers,
    )
    return JSONResponse(result.payload, status_code=result.status_code)
