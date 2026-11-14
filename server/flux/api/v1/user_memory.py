"""跨 Bot 共享用户记忆 API（Phase 4：按 Flux 云用户隔离）。

与 `/api/v1/memory`（三层记忆）和 `/api/v1/bots/{id}/memories`（每 Bot 记忆）
分开挂载：这里的画像与日志属于**当前登录人**，只对这个人可见、也只会注入这个人
的上下文。两个人要共享记忆请使用共享空间（`layer=shared`）。

- 画像是 last-write-wins 的键值事实，只有本组接口可写；
- 日志只追加，服务层按规范化文本的 sha256 去重，重复提交返回已有条目；
- 分不清是谁时一律 403 `cloud_login_required`（与 devices / keys / memory 同口径）；
  别人的 key / 条目按不存在处理（404），不泄露是否存在。
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import BaseModel, Field

from flux.api.account_scope import account_key_for_request
from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.core.memory.shared_user import MAX_JOURNAL_CHARS

router = APIRouter(prefix="/user-memory", tags=["user-memory"])


class JournalAppendRequest(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_JOURNAL_CHARS)
    source: Literal["user", "bot", "system"] = "user"
    source_bot_id: uuid.UUID | None = None


def _current_person(request: Request, container: Container) -> str:
    """本组接口都必须能确定「这是谁」；否则 403，绝不读写公共池。"""
    return account_key_for_request(request, container.settings)


@router.get("")
async def read_shared_user_memory(
    request: Request,
    journal_limit: int = Query(default=20, ge=1, le=200),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """当前人的画像 + 最近日志的组合视图。"""
    cloud_user_id = _current_person(request, container)
    data = await container.shared_user_memory.combined(
        journal_limit=journal_limit, cloud_user_id=cloud_user_id
    )
    return ok(
        data,
        metadata={
            "profile_keys": len(data["profile"]),  # type: ignore[arg-type]
            "journal": len(data["journal"]),  # type: ignore[arg-type]
        },
    )


@router.get("/profile")
async def read_profile(
    request: Request, container: Container = Depends(get_container)
) -> dict[str, object]:
    cloud_user_id = _current_person(request, container)
    profile = await container.shared_user_memory.get_profile(cloud_user_id=cloud_user_id)
    return ok({"profile": profile}, metadata={"keys": len(profile)})


@router.put("/profile")
async def update_profile(
    request: Request,
    payload: dict[str, str] = Body(...),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """按键覆盖写入当前人的画像；未知 key、空 key、超长值由服务层统一拒绝。"""
    cloud_user_id = _current_person(request, container)
    profile = await container.shared_user_memory.put_profile(payload, cloud_user_id=cloud_user_id)
    return ok({"profile": profile}, metadata={"updated": sorted(payload)})


@router.delete("/profile/{key}")
async def delete_profile(
    request: Request,
    key: str,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    cloud_user_id = _current_person(request, container)
    await container.shared_user_memory.delete_profile(key, cloud_user_id=cloud_user_id)
    return ok({"deleted": key.strip()})


@router.get("/journal")
async def list_journal(
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    cloud_user_id = _current_person(request, container)
    rows = await container.shared_user_memory.list_journal(
        limit=limit, offset=offset, cloud_user_id=cloud_user_id
    )
    return ok([row.to_dict() for row in rows], metadata={"count": len(rows)})


@router.post("/journal")
async def append_journal(
    request: Request,
    payload: JournalAppendRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """追加一条当前人的日志；重复内容返回已有条目（created=false）。"""
    cloud_user_id = _current_person(request, container)
    row, created = await container.shared_user_memory.append_journal(
        content=payload.content,
        source=payload.source,
        source_bot_id=payload.source_bot_id,
        cloud_user_id=cloud_user_id,
    )
    return ok({"entry": row.to_dict(), "created": created})


@router.delete("/journal/{entry_id}")
async def delete_journal(
    request: Request,
    entry_id: uuid.UUID,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    cloud_user_id = _current_person(request, container)
    await container.shared_user_memory.delete_journal(entry_id, cloud_user_id=cloud_user_id)
    return ok({"deleted": str(entry_id)})
