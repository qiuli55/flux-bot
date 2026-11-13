"""技能库 API：技能包 CRUD + 每 Bot 分配。

与 `/api/v1/capabilities`（扫描本机 SKILL.md 后登记到 imported_capabilities）
分开：这里的技能正文存在数据库里，可由 API 创建 / 编辑，并按 Bot 分配后
注入该 Bot 的对话上下文。

删除策略：技能包仍被任何 Bot 分配时返回 409，先解除分配再删除。

本组接口沿用 bots.py 的约定：`get_container` 依赖注入、`ok()` 统一响应体、
领域异常交给统一错误处理；不新增鉴权。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.core.skills.package import (
    MAX_BODY_CHARS,
    MAX_DESCRIPTION_CHARS,
    MAX_LIST_ITEMS,
    MAX_NAME_CHARS,
    MAX_PATH_CHARS,
    MAX_VERSION_CHARS,
)
from flux.errors import BadRequestError, NotFoundError, ValidationError

router = APIRouter(prefix="/skills", tags=["skills"])
#: 分配到 Bot 的接口挂在 /bots 下，保持「Bot 的配置归 Bot」这一 URL 直觉。
bot_router = APIRouter(prefix="/bots", tags=["skills"])

#: 原始 SKILL.md 文本除了正文还有 frontmatter，留一点余量
_MAX_DOCUMENT_CHARS = MAX_BODY_CHARS + 16_384


class SkillCreateRequest(BaseModel):
    """创建技能包。

    两种写法二选一：
    - JSON 字段：`{"name": "...", "body": "...", ...}`；
    - 原始 SKILL.md：`{"content": "---\\nname: ...\\n---\\n正文"}`。
    """

    name: str | None = Field(default=None, max_length=MAX_NAME_CHARS)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_CHARS)
    version: str | None = Field(default=None, max_length=MAX_VERSION_CHARS)
    body: str | None = Field(default=None, max_length=MAX_BODY_CHARS)
    entry: str | None = Field(default=None, max_length=MAX_PATH_CHARS)
    requires_bins: list[str] | None = Field(default=None, max_length=MAX_LIST_ITEMS)
    requires_skills: list[str] | None = Field(default=None, max_length=MAX_LIST_ITEMS)
    permissions: list[str] | None = Field(default=None, max_length=MAX_LIST_ITEMS)
    network: bool | None = None
    files: list[str] | None = Field(default=None, max_length=MAX_LIST_ITEMS)
    enabled: bool = True
    #: 原始 SKILL.md 文本；提供时不能再同时传单个字段
    content: str | None = Field(default=None, max_length=_MAX_DOCUMENT_CHARS)


class SkillUpdateRequest(BaseModel):
    """部分更新技能包；`content` 提供时整包重新解析。"""

    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_CHARS)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_CHARS)
    version: str | None = Field(default=None, max_length=MAX_VERSION_CHARS)
    body: str | None = Field(default=None, max_length=MAX_BODY_CHARS)
    entry: str | None = Field(default=None, max_length=MAX_PATH_CHARS)
    requires_bins: list[str] | None = Field(default=None, max_length=MAX_LIST_ITEMS)
    requires_skills: list[str] | None = Field(default=None, max_length=MAX_LIST_ITEMS)
    permissions: list[str] | None = Field(default=None, max_length=MAX_LIST_ITEMS)
    network: bool | None = None
    files: list[str] | None = Field(default=None, max_length=MAX_LIST_ITEMS)
    enabled: bool | None = None
    content: str | None = Field(default=None, max_length=_MAX_DOCUMENT_CHARS)


class BotSkillEntry(BaseModel):
    """一条 Bot 分配：技能 id + 可选顺序 / 启用状态。"""

    skill_id: str
    position: int | None = Field(default=None, ge=0, le=10_000)
    enabled: bool = True


class BotSkillsReplaceRequest(BaseModel):
    """整表替换某 Bot 的技能分配。

    两种写法：
    - `{"skills": [{"skill_id": "...", "position": 0}]}`；
    - `{"skill_ids": ["...", "..."]}`（position 按数组顺序）。
    """

    skills: list[BotSkillEntry] = Field(default_factory=list, max_length=128)
    skill_ids: list[str] | None = Field(default=None, max_length=128)


def _bot_id(value: str) -> uuid.UUID:
    """路径参数非法时按「Bot 不存在」处理（与 bots.py 一致）。"""
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise NotFoundError(f"Bot {value} 不存在", details={"bot_id": value}) from exc


def _skill_id(value: str) -> uuid.UUID:
    """路径参数非法时按「技能不存在」处理。"""
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise NotFoundError(f"技能 {value} 不存在", details={"skill_id": value}) from exc


@router.post("")
async def create_skill(
    payload: SkillCreateRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    """创建一个技能包（JSON 字段或原始 SKILL.md 文本）。"""
    enabled = payload.enabled
    if payload.content is not None:
        extras = sorted(set(payload.model_fields_set) - {"content", "enabled"})
        if extras:
            raise ValidationError(
                "提供 content 时不能再同时修改单个字段",
                details={"fields": extras},
            )
        row = await container.skills.create(document=payload.content, enabled=enabled)
    else:
        data = payload.model_dump(exclude_unset=True)
        data.pop("content", None)
        data.pop("enabled", None)
        row = await container.skills.create(data=data, enabled=enabled)
    return ok(row.to_dict())


@router.get("")
async def list_skills(
    enabled: bool | None = Query(default=None, description="按启用状态过滤"),
    limit: int = Query(default=50, ge=1, le=200),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """列出技能包；返回摘要，不含正文。"""
    rows = await container.skills.list(enabled=enabled, limit=limit)
    return ok([row.to_summary() for row in rows], metadata={"count": len(rows)})


@router.get("/{skill_id}")
async def get_skill(
    skill_id: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    row = await container.skills.get(_skill_id(skill_id))
    return ok(row.to_dict())


@router.patch("/{skill_id}")
async def update_skill(
    skill_id: str,
    payload: SkillUpdateRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """部分更新；显式传 null 的 version 会被清空。"""
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("content") is None:
        changes.pop("content", None)
    row = await container.skills.update(_skill_id(skill_id), changes)
    return ok(row.to_dict())


@router.delete("/{skill_id}")
async def delete_skill(
    skill_id: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    """删除技能包；仍被 Bot 分配时 409（先解除分配）。"""
    target = _skill_id(skill_id)
    await container.skills.delete(target)
    return ok({"deleted": str(target)})


# --- Bot 分配（固定放在 /bots/{bot_id}/skills）---


@bot_router.get("/{bot_id}/skills")
async def list_bot_skills(
    bot_id: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    """某个 Bot 当前分配到的技能（含停用的分配）。"""
    bot = await container.bots.get(_bot_id(bot_id))
    rows = await container.skills.list_for_bot(bot.id)
    return ok(rows, metadata={"count": len(rows)})


@bot_router.put("/{bot_id}/skills")
async def replace_bot_skills(
    bot_id: str,
    payload: BotSkillsReplaceRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """整表替换该 Bot 的技能分配；空列表表示全部解除。"""
    bot = await container.bots.get(_bot_id(bot_id))
    if payload.skill_ids is not None and payload.skills:
        raise BadRequestError(
            "skills 与 skill_ids 只能提供一种",
            details={"fields": ["skills", "skill_ids"]},
        )
    entries: list[dict[str, Any]]
    if payload.skill_ids is not None:
        entries = [
            {"skill_id": skill_id, "position": index}
            for index, skill_id in enumerate(payload.skill_ids)
        ]
    else:
        entries = [entry.model_dump() for entry in payload.skills]
    rows = await container.skills.replace_for_bot(bot.id, entries)
    return ok(rows, metadata={"count": len(rows)})


@bot_router.delete("/{bot_id}/skills/{skill_id}")
async def remove_bot_skill(
    bot_id: str,
    skill_id: str,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """解除一个技能分配；技能包本身保留。"""
    bot = await container.bots.get(_bot_id(bot_id))
    target = _skill_id(skill_id)
    await container.skills.remove_from_bot(bot.id, target)
    return ok({"deleted": str(target)})
