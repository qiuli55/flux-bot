"""账号云接入 API（Flux 本体 ↔ B 机 `flux.qiuli55.top` 账号系统）。

这里的端点都**不**受 `require_rest_auth` 保护（见 flux.main 的挂载方式）：
浏览器在未登录时也要能读到登录入口，并用一次性 code 换取本机会话——
这正是"先有鸡还是先有蛋"的那一步。真正的写操作仍然落在 `/api/v1` 的其它面上，
由 `require_rest_auth` 用同一份云令牌把关。
"""

from __future__ import annotations

import time
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from flux.api.deps import get_settings_dep
from flux.api.response import ok
from flux.cloud.session import (
    CloudClient,
    CloudError,
    CloudSession,
    clear_session,
    load_session,
    save_session,
)
from flux.config import Settings
from flux.errors import AuthenticationError

router = APIRouter(prefix="/cloud", tags=["cloud"])


class CloudExchangeIn(BaseModel):
    code: str = Field(
        min_length=8,
        max_length=256,
        description="账号云登录页回跳时携带的一次性登录码",
    )


class CloudRefreshIn(BaseModel):
    refresh_token: str = Field(
        min_length=8,
        max_length=2048,
        description="账号云下发的 refresh token",
    )


def _login_url(settings: Settings) -> str:
    base = (settings.cloud_base_url or "").rstrip("/")
    next_url = (settings.cloud_return_url or "").strip()
    if not next_url:
        return f"{base}/login"
    return f"{base}/login?next={quote(next_url, safe='')}"


def _user_payload(settings: Settings) -> dict[str, object]:
    session = load_session(settings.cloud_session_path)
    if session is None:
        return {"authenticated": False, "user": None}
    return {
        "authenticated": not session.expired,
        "user": {"id": session.user_id, "username": session.username},
    }


def _token_payload(session: CloudSession) -> dict[str, object]:
    """回给浏览器的令牌信封。

    浏览器把 access token 存在本地，后续请求用 `Authorization: Bearer` 打到
    `/api/v1`——那边的 `require_rest_auth` 用同一份共享密钥本地验签，不每次回源。
    本机落盘的那份会话照旧保留，给"启动时自动上传日志"的后台任务用。
    """
    return {
        "authenticated": True,
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "expires_in": max(0, int(session.expires_at - time.time())),
        "user": {"id": session.user_id, "username": session.username},
    }


@router.get("/session", summary="查询本机账号云登录状态与登录入口")
async def cloud_session(settings: Settings = Depends(get_settings_dep)) -> dict[str, object]:
    await _refresh_if_expired(settings)
    payload = _user_payload(settings)
    secret_ok = bool((settings.cloud_secret_key or "").strip())
    payload["configured"] = secret_ok or settings.cloud_login_required
    payload["login_url"] = _login_url(settings)
    return ok(payload)


async def _refresh_if_expired(settings: Settings) -> None:
    """本机存有 refresh token 时，access token 一过期就先续期再回报状态。

    账号云的 access token 只有 30 分钟有效期；不续期的话，用户每半小时就会被
    桌面端踢回登录闸门。refresh token 是本机持有的一次性凭据，续期不改变
    /cloud/exchange 的语义。续期失败（凭据失效 / 账号云不可达）则保持原样，
    让前端走重新登录。
    """
    session = load_session(settings.cloud_session_path)
    if session is None or not session.expired or not session.refresh_token:
        return

    try:
        client = CloudClient(settings.cloud_base_url)
        refreshed = await client.refresh(session.refresh_token)
    except (CloudError, httpx.HTTPError):
        return

    save_session(settings.cloud_session_path, refreshed)


@router.post("/exchange", summary="用一次性登录码换取本机账号云会话")
async def cloud_exchange(
    body: CloudExchangeIn,
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, object]:
    client = CloudClient(settings.cloud_base_url)
    try:
        session = await client.exchange_code(body.code)
    except (CloudError, httpx.HTTPError) as exc:
        raise AuthenticationError(f"登录码兑换失败：{exc}") from exc
    save_session(settings.cloud_session_path, session)
    return ok(_token_payload(session))


@router.post("/refresh", summary="用 refresh token 续期本机账号云会话")
async def cloud_refresh(
    body: CloudRefreshIn,
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, object]:
    client = CloudClient(settings.cloud_base_url)
    try:
        session = await client.refresh(body.refresh_token)
    except (CloudError, httpx.HTTPError) as exc:
        raise AuthenticationError(f"令牌续期失败：{exc}") from exc
    save_session(settings.cloud_session_path, session)
    return ok(_token_payload(session))


@router.post("/logout", summary="清除本机保存的账号云会话")
async def cloud_logout(settings: Settings = Depends(get_settings_dep)) -> dict[str, object]:
    clear_session(settings.cloud_session_path)
    return ok({"authenticated": False})
