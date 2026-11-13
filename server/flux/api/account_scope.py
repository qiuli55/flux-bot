"""REST 面的账号标识：登录人就是账号。

Phase 2 起不再信任客户端自报的 `X-Flux-Account`：连接器 / 日历 / 对话等按账号
隔离的数据，一律以 `current_cloud_user_id`（云 JWT 的 `sub` 或
`FLUX_OWNER_CLOUD_USER_ID`）作为 `account_key`。分不清是谁时 fail-closed，
返回稳定的 `cloud_login_required`，而不是写一条 `'default'` 的公共记录。
"""

from __future__ import annotations

from fastapi import Request

from flux.api.auth import current_cloud_user_id
from flux.config import Settings
from flux.errors import PermissionDeniedError

#: 无法确定"这是谁"时的稳定错误码（与 devices / memory / keys 一致）。
CLOUD_LOGIN_REQUIRED = "cloud_login_required"


def account_key_for_request(request: Request, settings: Settings) -> str:
    """把本次请求解析成账号标识（= Flux 云用户 id）。"""
    principal = getattr(request.state, "principal", None) or {}
    user_id = current_cloud_user_id(principal, settings)
    if not user_id:
        raise PermissionDeniedError(
            "该操作需要 Flux 账号登录（或由部署方设置 FLUX_OWNER_CLOUD_USER_ID）",
            code=CLOUD_LOGIN_REQUIRED,
            details={"hint": "请先登录账号云；本机 CLI / 主用户可配置 FLUX_OWNER_CLOUD_USER_ID"},
        )
    return user_id


__all__ = ["CLOUD_LOGIN_REQUIRED", "account_key_for_request"]
