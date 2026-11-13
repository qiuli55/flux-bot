"""密钥库 API（API Key Vault）。

管理面：列表 / 创建 / 修改 / 删除 / 连通性探测。明文 API Key 只进请求体，落库即加密；
所有响应都只回元数据与末 4 位掩码，绝不出密文/明文。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from flux.api.auth import get_current_cloud_user_id
from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.core.key_vault.probe import probe_vault_key
from flux.core.key_vault.service import CLOUD_LOGIN_REQUIRED
from flux.errors import PermissionDeniedError
from flux.schemas.api import ApiKeyCreateRequest, ApiKeyUpdateRequest

router = APIRouter(prefix="/keys", tags=["keys"])


def _require_user(cloud_user_id: str | None) -> str:
    if cloud_user_id is None:
        raise PermissionDeniedError(
            "该操作需要 Flux 账号登录（或由部署方设置 FLUX_OWNER_CLOUD_USER_ID）",
            code=CLOUD_LOGIN_REQUIRED,
            details={"hint": "请先登录账号云；本机 CLI / 主用户可配置 FLUX_OWNER_CLOUD_USER_ID"},
        )
    return cloud_user_id


@router.get("")
async def list_keys(
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """列出当前用户的密钥库条目（按创建时间）；响应只含掩码与元数据。

    没有可解析的人时返回空列表——不把任何人的密钥暴露给匿名/本机请求。
    """
    if cloud_user_id is None:
        return ok([], metadata={"count": 0})
    items = await container.key_vault.list_public(cloud_user_id=cloud_user_id)
    return ok(items, metadata={"count": len(items)})


@router.post("")
async def create_key(
    payload: ApiKeyCreateRequest,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """登记一条 API Key；同一用户内名称唯一，不同用户可以重名。"""
    row = await container.key_vault.create(
        name=payload.name,
        provider=payload.provider,
        base_url=payload.base_url,
        api_key=payload.api_key,
        models=payload.models,
        wire_api=payload.wire_api,
        enabled=payload.enabled,
        cloud_user_id=_require_user(cloud_user_id),
    )
    return ok(row.to_dict())


@router.patch("/{key_id}")
async def update_key(
    key_id: str,
    payload: ApiKeyUpdateRequest,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """修改当前用户的密钥条目；别人的条目按不存在处理（404）。"""
    data = payload.model_dump(exclude_unset=True)
    if not str(data.get("api_key") or "").strip():
        data.pop("api_key", None)
    if data.get("enabled") is None:
        data.pop("enabled", None)
    row = await container.key_vault.update(
        key_id, cloud_user_id=_require_user(cloud_user_id), **data
    )
    return ok(row.to_dict())


@router.post("/{key_id}/probe")
async def probe_key(
    key_id: str,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """用该 key 登记的模型与端点真实发起一次最小请求，校验是否可用。

    保存动作本身不触网；由调用方在保存后显式触发，结果只回状态与耗时，不回密钥。
    """
    result = await probe_vault_key(
        container.key_vault, key_id, cloud_user_id=_require_user(cloud_user_id)
    )
    return ok(result)


@router.delete("/{key_id}")
async def delete_key(
    key_id: str,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """删除密钥库条目；引用它的任务会回到 Agent 默认模型（fail-safe）。"""
    await container.key_vault.delete(key_id, cloud_user_id=_require_user(cloud_user_id))
    return ok({"deleted": True, "key_id": key_id})


__all__ = ["router"]
