"""共享空间与 shared 记忆 API（Phase 3）。

身份只认 Flux 云用户 id：无法解析时一律 403 `cloud_login_required`。
非成员访问空间（包括空间里的记忆）统一按 404 处理，不泄露空间是否存在。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from flux.api.account_scope import account_key_for_request
from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.errors import ValidationError
from flux.schemas.api import UserMemoryWriteRequest

router = APIRouter(prefix="/spaces", tags=["spaces"])


class SpaceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class SpaceMemberRequest(BaseModel):
    cloud_user_id: str = Field(min_length=1, max_length=64)
    role: str = Field(default="member", pattern="^(owner|member)$")


def _space_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        # 非法 id 与不存在同语义，避免成为探测信号。
        raise ValidationError("space_id 不是合法的 UUID", details={"space_id": value}) from exc


@router.post("")
async def create_space(
    payload: SpaceCreateRequest,
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """创建共享空间；创建者自动成为 owner。不自动创建默认空间。"""
    cloud_user_id = account_key_for_request(request, container.settings)
    space = await container.spaces.create(name=payload.name, cloud_user_id=cloud_user_id)
    return ok({**space.to_dict(), "role": "owner"})


@router.get("")
async def list_spaces(
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """当前用户加入的空间（含自己的角色）。"""
    cloud_user_id = account_key_for_request(request, container.settings)
    rows = await container.spaces.list(cloud_user_id)
    return ok(rows, metadata={"count": len(rows)})


@router.get("/{space_id}")
async def get_space(
    space_id: str,
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    cloud_user_id = account_key_for_request(request, container.settings)
    return ok(await container.spaces.view(_space_uuid(space_id), cloud_user_id))


@router.get("/{space_id}/members")
async def list_space_members(
    space_id: str,
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """成员可以查看成员列表。"""
    cloud_user_id = account_key_for_request(request, container.settings)
    rows = await container.spaces.members(_space_uuid(space_id), cloud_user_id)
    return ok([row.to_dict() for row in rows], metadata={"count": len(rows)})


@router.post("/{space_id}/members")
async def add_space_member(
    space_id: str,
    payload: SpaceMemberRequest,
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """owner 添加成员（或更新已有成员的角色）。"""
    actor = account_key_for_request(request, container.settings)
    row = await container.spaces.add_member(
        _space_uuid(space_id),
        actor_cloud_user_id=actor,
        cloud_user_id=payload.cloud_user_id,
        role=payload.role,
    )
    return ok(row.to_dict())


@router.delete("/{space_id}/members/{cloud_user_id}")
async def remove_space_member(
    space_id: str,
    cloud_user_id: str,
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """owner 移除成员；不允许把空间留成没有 owner（409）。"""
    actor = account_key_for_request(request, container.settings)
    await container.spaces.remove_member(
        _space_uuid(space_id),
        actor_cloud_user_id=actor,
        cloud_user_id=cloud_user_id,
    )
    return ok({"deleted": cloud_user_id, "space_id": space_id})


@router.get("/{space_id}/memories")
async def list_shared_memories(
    space_id: str,
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """列出本空间的 shared 记忆；非成员 404。"""
    cloud_user_id = account_key_for_request(request, container.settings)
    entries = await container.memory.list_shared(_space_uuid(space_id), cloud_user_id=cloud_user_id)
    return ok([entry.to_dict() for entry in entries], metadata={"count": len(entries)})


@router.post("/{space_id}/memories")
async def write_shared_memory(
    space_id: str,
    payload: UserMemoryWriteRequest,
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """成员写入一条 shared 记忆；owner / member 权限相同。"""
    cloud_user_id = account_key_for_request(request, container.settings)
    entry = await container.memory.write_shared(
        _space_uuid(space_id),
        content=payload.content,
        source=payload.source,
        meta=payload.metadata or None,
        cloud_user_id=cloud_user_id,
    )
    return ok(entry.to_dict())


@router.delete("/{space_id}/memories/{entry_id}")
async def delete_shared_memory(
    space_id: str,
    entry_id: str,
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """成员删除本空间的一条 shared 记忆；别的空间的条目按不存在处理。"""
    cloud_user_id = account_key_for_request(request, container.settings)
    try:
        entry_uuid = uuid.UUID(entry_id)
    except ValueError as exc:
        raise ValidationError("entry_id 不是合法的 UUID", details={"entry_id": entry_id}) from exc
    await container.memory.delete_shared(
        _space_uuid(space_id), entry_uuid, cloud_user_id=cloud_user_id
    )
    return ok({"deleted": entry_id})


__all__ = ["router"]
