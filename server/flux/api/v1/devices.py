"""设备配对与设备令牌管理。

手机不再共享 `FLUX_AUTH_TOKEN`：管理员在本机/后端生成一次性配对码，手机输入后换到
只属于自己的长期令牌。管理接口（生成配对码 / 列表 / 撤销）只允许管理员；设备只能
撤销自己（`/devices/me/revoke`），用于手机端主动登出。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request

from flux.api.auth import require_admin, require_device
from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.schemas.api import DevicePairRequest

router = APIRouter(prefix="/devices", tags=["devices"])


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return moment.isoformat()


@router.post("/pairing-codes")
async def issue_pairing_code(
    container: Container = Depends(get_container),
    _: dict[str, object] = Depends(require_admin),
) -> dict[str, object]:
    """生成一次性配对码（管理员 / 本机直连；设备令牌不可用）。"""
    code, expires_at = await container.devices.issue_pairing_code()
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
    _: dict[str, object] = Depends(require_admin),
) -> dict[str, object]:
    """列出已配对设备（只含令牌前缀，不含哈希与明文）。"""
    devices = await container.devices.list()
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
    _: dict[str, object] = Depends(require_admin),
) -> dict[str, object]:
    """管理员撤销设备；本进程缓存立即清除，后续请求 401。"""
    device = await container.devices.revoke(device_id)
    return ok(device.to_dict())


__all__ = ["router"]
