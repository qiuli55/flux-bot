"""跨 Bot 共享用户记忆 API。

与 `/api/v1/memory`（三层记忆）和 `/api/v1/bots/{id}/memories`（每 Bot 记忆）
分开挂载：这里的画像与日志对账号下的所有 Bot 可见。

- 画像是 last-write-wins 的键值事实，只有本组接口可写；
- 日志只追加，服务层按规范化文本的 sha256 去重，重复提交返回已有条目。
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, Field

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.core.memory.shared_user import MAX_JOURNAL_CHARS

router = APIRouter(prefix="/user-memory", tags=["user-memory"])


class JournalAppendRequest(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_JOURNAL_CHARS)
    source: Literal["user", "bot", "system"] = "user"
    source_bot_id: uuid.UUID | None = None


@router.get("")
async def read_shared_user_memory(
    journal_limit: int = Query(default=20, ge=1, le=200),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """画像 + 最近日志的组合视图。"""
    data = await container.shared_user_memory.combined(journal_limit=journal_limit)
    return ok(
        data,
        metadata={
            "profile_keys": len(data["profile"]),  # type: ignore[arg-type]
            "journal": len(data["journal"]),  # type: ignore[arg-type]
        },
    )


@router.get("/profile")
async def read_profile(container: Container = Depends(get_container)) -> dict[str, object]:
    profile = await container.shared_user_memory.get_profile()
    return ok({"profile": profile}, metadata={"keys": len(profile)})


@router.put("/profile")
async def update_profile(
    payload: dict[str, str] = Body(...),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """按键覆盖写入画像；未知 key、空 key、超长值由服务层统一拒绝。"""
    profile = await container.shared_user_memory.put_profile(payload)
    return ok({"profile": profile}, metadata={"updated": sorted(payload)})


@router.delete("/profile/{key}")
async def delete_profile(
    key: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    await container.shared_user_memory.delete_profile(key)
    return ok({"deleted": key.strip()})


@router.get("/journal")
async def list_journal(
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    rows = await container.shared_user_memory.list_journal(limit=limit, offset=offset)
    return ok([row.to_dict() for row in rows], metadata={"count": len(rows)})


@router.post("/journal")
async def append_journal(
    payload: JournalAppendRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """追加一条共享日志；重复内容返回已有条目（created=false）。"""
    row, created = await container.shared_user_memory.append_journal(
        content=payload.content,
        source=payload.source,
        source_bot_id=payload.source_bot_id,
    )
    return ok({"entry": row.to_dict(), "created": created})


@router.delete("/journal/{entry_id}")
async def delete_journal(
    entry_id: uuid.UUID, container: Container = Depends(get_container)
) -> dict[str, object]:
    await container.shared_user_memory.delete_journal(entry_id)
    return ok({"deleted": str(entry_id)})
