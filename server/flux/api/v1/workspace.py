"""Virtual Workspace API（主规格 §12.5）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.enums import RecoveryResolution
from flux.schemas.api import (
    ChangeIdsRequest,
    ExpireRequest,
    GroupRequest,
    RecoveryResolveRequest,
    RejectRequest,
    WorkspaceRootRequest,
)

router = APIRouter(prefix="/workspace", tags=["workspace"])


@router.get("/root")
async def get_workspace_root(container: Container = Depends(get_container)) -> dict[str, object]:
    return ok({"root": container.settings.workspace_root})


@router.put("/root")
async def set_workspace_root(
    payload: WorkspaceRootRequest,
    container: Container = Depends(get_container),
    x_flux_desktop: str | None = Header(default=None),
) -> dict[str, object]:
    """Switch the live workspace root from the local Electron desktop shell only."""
    if container.settings.env != "local" or x_flux_desktop != "1":
        raise HTTPException(status_code=403, detail="只有本机 Flux 桌面端可以选择工作目录")
    try:
        root = container.set_workspace_root(payload.root)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ok({"root": root})


@router.get("/changes")
async def list_changes(
    project_id: str | None = None,
    task_id: str | None = None,
    group_id: str | None = None,
    file_path: str | None = None,
    status: str | None = None,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    changes = await container.workspace.list(
        project_id=project_id,
        task_id=task_id,
        group_id=group_id,
        file_path=file_path,
        status=status,
    )
    return ok([c.to_dict() for c in changes], metadata={"count": len(changes)})


@router.get("/changes/{change_id}")
async def get_change(
    change_id: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    change = await container.workspace.get(change_id)
    return ok(change.to_dict())


@router.post("/accept")
async def accept_changes(
    payload: ChangeIdsRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    accepted = [c.to_dict() for c in await container.workspace.accept_many(payload.change_ids)]
    return ok(accepted, metadata={"count": len(accepted)})


@router.post("/apply")
async def apply_changes(
    payload: ChangeIdsRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    applied = [c.to_dict() for c in await container.workspace.apply_many(payload.change_ids)]
    return ok(applied, metadata={"count": len(applied)})


@router.post("/reject")
async def reject_changes(
    payload: RejectRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    rejected = [
        c.to_dict()
        for c in await container.workspace.reject_many(payload.change_ids, reason=payload.reason)
    ]
    return ok(rejected, metadata={"count": len(rejected), "reason": payload.reason})


@router.post("/accept-group")
async def accept_group(
    payload: GroupRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    accepted = [c.to_dict() for c in await container.workspace.accept_group(payload.group_id)]
    return ok(accepted, metadata={"count": len(accepted), "group_id": payload.group_id})


@router.post("/reject-group")
async def reject_group(
    payload: GroupRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    rejected = [
        c.to_dict()
        for c in await container.workspace.reject_group(payload.group_id, reason=payload.reason)
    ]
    return ok(rejected, metadata={"count": len(rejected), "group_id": payload.group_id})


@router.post("/apply-group")
async def apply_group(
    payload: GroupRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    applied = [c.to_dict() for c in await container.workspace.apply_group(payload.group_id)]
    return ok(applied, metadata={"count": len(applied), "group_id": payload.group_id})


@router.post("/expire")
async def expire_changes(
    payload: ExpireRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    expired = [
        c.to_dict()
        for c in await container.workspace.expire_many(payload.change_ids, reason=payload.reason)
    ]
    return ok(expired, metadata={"count": len(expired)})


@router.post("/expire-stale")
async def expire_stale(container: Container = Depends(get_container)) -> dict[str, object]:
    expired = [c.to_dict() for c in await container.workspace.expire_stale()]
    return ok(expired, metadata={"count": len(expired)})


@router.get("/apply-batches")
async def list_apply_batches(
    recent: int = 0, limit: int = 20, container: Container = Depends(get_container)
) -> dict[str, object]:
    batches = await container.workspace.list_recent_batches(recent=bool(recent), limit=limit)
    return ok([b.to_dict() for b in batches], metadata={"count": len(batches)})


@router.post("/apply-batches/{batch_id}/rollback")
async def rollback_batch(
    batch_id: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    batch = await container.workspace.rollback_batch(batch_id)
    return ok(batch.to_dict(), metadata={"batch_id": str(batch.id)})


@router.post("/changes/{change_id}/rollback")
async def rollback_change(
    change_id: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    change = await container.workspace.rollback_change(change_id)
    return ok(change.to_dict(), metadata={"change_id": str(change.id)})


@router.get("/recovery")
async def list_recovery(container: Container = Depends(get_container)) -> dict[str, object]:
    items = await container.workspace.list_pending_recovery_items()
    return ok([item.to_dict() for item in items], metadata={"count": len(items)})


@router.post("/recovery/resolve")
async def resolve_recovery(
    payload: RecoveryResolveRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    change = await container.workspace.resolve_recovery(
        payload.change_id, RecoveryResolution(payload.action)
    )
    return ok(change.to_dict(), metadata={"action": payload.action})
