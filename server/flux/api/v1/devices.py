"""设备配对与设备令牌管理。

手机不再共享 `FLUX_AUTH_TOKEN`：管理员在本机/后端生成一次性配对码，手机输入后换到
只属于自己的长期令牌。管理接口（生成配对码 / 列表 / 撤销）只允许管理员；设备只能
撤销自己（`/devices/me/revoke`），用于手机端主动登出。

Phase 1 起设备归属到具体的 Flux 云用户：签发配对码时把当前用户 id 钉在码上，
兑换出的设备继承它；列表 / 撤销只看得到当前用户名下的设备。`POST /devices/pair`
仍免鉴权——手机此时还没有任何令牌，配对码本身就是凭据，归属由码携带。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request

from flux.api.auth import current_cloud_user_id, require_admin, require_device
from flux.api.deps import get_container
from flux.api.response import ok
from flux.config import Settings
from flux.container import Container
from flux.errors import PermissionDeniedError
from flux.schemas.api import DevicePairRequest

#: 无法确定"这是谁"时的稳定错误码：调用方应引导用户先登录账号云，
#: 或在部署侧设置 FLUX_OWNER_CLOUD_USER_ID。
CLOUD_LOGIN_REQUIRED = "cloud_login_required"

router = APIRouter(prefix="/devices", tags=["devices"])


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return moment.isoformat()


def _require_cloud_user_id(principal: dict[str, object], settings: Settings) -> str:
    """按人管理设备前必须先知道"这是谁"；分不清就明确拒绝。

    顺序：principal 的 cloud_user_id（云 JWT / 设备归属）→
    FLUX_OWNER_CLOUD_USER_ID（主用户 / 本机管理员兜底）。两者都没有时，
    说明既没登录账号云、部署也没配主用户，不能把设备随便归到某个人名下。
    """
    user_id = current_cloud_user_id(principal, settings)
    if not user_id:
        raise PermissionDeniedError(
            "该操作需要 Flux 账号登录（或由部署方设置 FLUX_OWNER_CLOUD_USER_ID）",
            code=CLOUD_LOGIN_REQUIRED,
            details={"hint": "请先登录账号云；本机 CLI / 主用户可配置 FLUX_OWNER_CLOUD_USER_ID"},
        )
    return user_id


@router.post("/pairing-codes")
async def issue_pairing_code(
    container: Container = Depends(get_container),
    principal: dict[str, object] = Depends(require_admin),
) -> dict[str, object]:
    """生成一次性配对码（管理员 / 本机直连；设备令牌不可用）。

    码上会盖章当前用户 id；兑换出的设备继承该归属。云登录态的 sub 优先于
    FLUX_OWNER_CLOUD_USER_ID（不会把用户 2 的码误记到主用户 1 名下）。
    """
    owner = _require_cloud_user_id(principal, container.settings)
    code, expires_at = await container.devices.issue_pairing_code(cloud_user_id=owner)
    return ok({"code": code, "expires_at": _iso(expires_at)})


@router.post("/pair", status_code=201)
async def pair_device(
    payload: DevicePairRequest,
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """用配对码兑换设备令牌；明文 token 只在本响应出现一次。

    该路径在 `require_rest_auth` 的免鉴权白名单里——手机此时还没有任何令牌。
    """
    client_ip = request.client.host if request.client is not None else "unknown"
    device, token = await container.devices.pair(
        code=payload.code,
        device_name=payload.device_name,
        client_ip=client_ip,
    )
    return ok({"device": device.to_dict(), "token": token})


@router.get("")
async def list_devices(
    container: Container = Depends(get_container),
    principal: dict[str, object] = Depends(require_admin),
) -> dict[str, object]:
    """列出当前用户名下的设备（只含令牌前缀，不含哈希与明文）。"""
    owner = _require_cloud_user_id(principal, container.settings)
    devices = await container.devices.list(cloud_user_id=owner)
    return ok([device.to_dict() for device in devices], metadata={"count": len(devices)})


@router.post("/me/revoke")
async def revoke_current_device(
    principal: dict[str, object] = Depends(require_device),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """手机主动登出：撤销当前设备令牌，立即生效。"""
    device = await container.devices.revoke(str(principal["device_id"]))
    return ok(device.to_dict())


@router.delete("/{device_id}")
async def revoke_device(
    device_id: str,
    container: Container = Depends(get_container),
    principal: dict[str, object] = Depends(require_admin),
) -> dict[str, object]:
    """管理员撤销自己名下的设备；本进程缓存立即清除，后续请求 401。

    别人的设备返回 404（与"不存在"一致），不泄露设备 id 是否存在。
    """
    owner = _require_cloud_user_id(principal, container.settings)
    device = await container.devices.revoke(device_id, cloud_user_id=owner)
    return ok(device.to_dict())


__all__ = ["router"]
