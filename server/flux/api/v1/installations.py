"""Agent Installation API：本机 CLI Agent 的发现、接入与移除（最终方案 §3.2 / §4）。

这组接口是 UI「外部 Agent」区块的后端：扫描本机 → 看到 codex / opencode 及其状态 →
一键接入 → 状态收敛到 READY。事实落 `agent_installations` 表，与 Agent 档案
（`agents` 表，身份与权限边界）分离——"装了什么"与"是谁"是两件事。

接入 ≠ 能用：接入只说明本机具备该 CLI 且已验证。任务真正走它，还需要一个
`runtime` 指向该 CLI 的 Agent 档案（`POST /agents` 的 `runtime` 字段），
并在新建任务时绑定该 Agent。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.errors import BadRequestError, NotFoundError
from flux.schemas.api import InstallationConnectRequest

router = APIRouter(prefix="/installations", tags=["installations"])


@router.get("")
async def list_installations(container: Container = Depends(get_container)) -> dict[str, object]:
    """已登记的接入记录（含未安装的：NOT_INSTALLED 也是事实）。"""
    rows = await container.installations.list()
    return ok([row.to_dict() for row in rows], metadata={"count": len(rows)})


@router.post("/scan")
async def scan_installations(container: Container = Depends(get_container)) -> dict[str, object]:
    """扫描全部内置 Adapter，刷新安装事实并落库。

    重扫不会把已接入的 Agent 降级（卸载除外）——状态机单向收敛，见 InstallationService。
    """
    rows = await container.installations.scan()
    ready = sum(1 for row in rows if row.status == "READY")
    return ok(
        [row.to_dict() for row in rows],
        metadata={"count": len(rows), "ready": ready},
    )


@router.post("/connect")
async def connect_installation(
    payload: InstallationConnectRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """接入 Agent：指定 `agent` 接入单个，`all=true` 接入全部已安装的。

    未安装的 Agent 会被跳过（all 模式）或 422 拒绝（单个模式）——
    不允许"假装接入成功"。
    """
    if payload.all:
        rows = await container.installations.connect_all()
        return ok(
            [row.to_dict() for row in rows],
            metadata={"count": len(rows), "mode": "all"},
        )
    if not payload.agent:
        raise BadRequestError(
            "请指定要接入的 Agent，或用 all=true 接入全部已安装的",
            details={"known": list(container.installations.adapters)},
        )
    row = await container.installations.connect(payload.agent)
    return ok(row.to_dict(), metadata={"mode": "single", "agent": payload.agent})


@router.delete("/{name}")
async def remove_installation(
    name: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    """移除接入记录（不卸载本机 CLI，只删 Flux 这边的登记）。"""
    if await container.installations.get(name) is None:
        raise NotFoundError(f"没有该 Agent 的接入记录：{name}", details={"agent": name})
    await container.installations.remove(name)
    return ok({"agent": name, "removed": True})


__all__ = ["router"]
