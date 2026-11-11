"""Bot 桌面（Hermes 云电脑 noVNC）入口参数。

凭据（桌面 token / 桥接密钥）只从服务端环境变量下发，App 源码不保留；
与其他业务接口共用同一道 REST 鉴权，仓库公开也不会直接泄漏。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container

router = APIRouter(prefix="/desktop", tags=["desktop"])


@router.get("")
async def get_desktop_config(container: Container = Depends(get_container)) -> dict[str, object]:
    """当前可用的桌面入口：隧道地址随隧道变化，凭据由服务端注入。"""
    return ok(await container.desktop.config())
