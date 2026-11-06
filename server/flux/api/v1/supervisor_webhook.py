"""Supervisor 的平台回调入口（飞书事件订阅 + 微信入站）。

这里**不**走 `require_rest_auth`：飞书 / OpenClaw 都没有本机令牌，验签靠各自
回调体或请求头里的令牌（见 `flux.core.supervisor.feishu` / `.wechat`，
未配置时一律拒绝）。响应必须是平台约定的原始 JSON，不能套 `ok()` 信封。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from flux.api.deps import get_container
from flux.container import Container

router = APIRouter(prefix="/supervisor", tags=["supervisor"])


@router.post("/channels/feishu")
async def feishu_callback(
    request: Request,
    container: Container = Depends(get_container),
) -> JSONResponse:
    """飞书事件回调：验签 → 解析文本消息 → 幂等入库。"""
    body = await request.body()
    result = await container.supervisor_feishu.handle(body)
    return JSONResponse(result.payload, status_code=result.status_code)


@router.post("/channels/wechat")
async def wechat_callback(
    request: Request,
    container: Container = Depends(get_container),
) -> JSONResponse:
    """微信入站：OpenClaw 插件把消息转给 Flux，拿回复后自己发回微信。

    返回 `{"handled": true, "reply": {"text": ...}}` 表示 Flux 认领并已生成回复；
    非 2xx 表示插件不认领，交回 OpenClaw 原生模型。
    """
    body = await request.body()
    result = await container.supervisor_wechat.handle(body, request.headers.get("authorization"))
    return JSONResponse(result.payload, status_code=result.status_code)
