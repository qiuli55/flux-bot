"""ChatGPT 账号（Codex 订阅通道）API。

只做登录态管理：状态 / 设备码登录 / 轮询登录结果 / 退出。
响应里不出现 access_token / refresh_token / id_token —— status 只给邮箱、套餐与过期时间。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.errors import NotFoundError

router = APIRouter(prefix="/codex-account", tags=["codex-account"])


@router.get("")
async def account_status(container: Container = Depends(get_container)) -> dict[str, object]:
    """当前 ChatGPT 账号的登录态（不含任何令牌）。"""
    return ok(container.codex_account.status())


@router.post("/login")
async def start_login(container: Container = Depends(get_container)) -> dict[str, object]:
    """发起设备码登录；返回授权链接与设备码，前端据此引导用户授权。"""
    session = await container.codex_account.start_login()
    return ok(session)


@router.get("/login/{login_id}")
async def login_state(
    login_id: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    """轮询登录结果：starting / pending / success / failed。"""
    state = container.codex_account.login_state(login_id)
    if state is None:
        raise NotFoundError(f"登录会话不存在或已过期：{login_id}", details={"login_id": login_id})
    return ok(state)


@router.post("/logout")
async def logout(container: Container = Depends(get_container)) -> dict[str, object]:
    """退出 ChatGPT 账号：删除 Flux 账号目录里的 auth.json。"""
    return ok(await container.codex_account.logout())


__all__ = ["router"]
