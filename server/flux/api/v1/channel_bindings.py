"""IM 频道 ↔ Flux Cloud 账号绑定 API（Phase 3）。

绑定码只在这里生成：必须是已登录的 Flux 账号（云 JWT 的 sub 或主用户兜底），
否则 403 `cloud_login_required`。频道回调本身仍是公开入口（平台签名/HMAC 自证），
真正决定「这条消息代表谁」的是频道侧查到的绑定关系。

这些接口都不在 `PUBLIC_REST_PATHS` 里，不能匿名调用。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from flux.api.account_scope import account_key_for_request
from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container

router = APIRouter(prefix="/channel-bindings", tags=["channel-bindings"])


@router.post("/codes")
async def issue_bind_code(
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """生成一次性绑定码（明文只返回这一次）。在 IM 里发这条码即可绑定。"""
    cloud_user_id = account_key_for_request(request, container.settings)
    code, expires_at = await container.channel_bindings.issue_code(cloud_user_id=cloud_user_id)
    return ok(
        {
            "code": code,
            "expires_at": expires_at.isoformat(),
            "ttl_seconds": container.channel_bindings.ttl_seconds,
            "instruction": (
                "在飞书 / 企业微信 / 钉钉里直接把这条 8 位码发给 Flux Bot，"
                "绑定成功后就可以正常聊天。绑定码一次有效，过期需重新生成。"
            ),
        }
    )


@router.get("")
async def list_bindings(
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """当前登录人的全部频道绑定（看不到别人的）。"""
    cloud_user_id = account_key_for_request(request, container.settings)
    rows = await container.channel_bindings.list(cloud_user_id)
    return ok([row.to_dict() for row in rows], metadata={"count": len(rows)})


@router.delete("/{binding_id}")
async def delete_binding(
    binding_id: str,
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """解绑自己的频道账号；别人的绑定按不存在处理（404）。"""
    cloud_user_id = account_key_for_request(request, container.settings)
    await container.channel_bindings.unbind(binding_id, cloud_user_id=cloud_user_id)
    return ok({"deleted": binding_id})


__all__ = ["router"]
