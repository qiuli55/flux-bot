"""制品页面管理 API（非公开）。

Bot 产出的「标题 + 正文」以页面形式持久化，可发布成公开链接。这里只做管理：
创建、列表、详情、保存新版本、发布 / 下线、版本历史与删除。公开读取在
`flux.api.v1.public_pages`，单独挂载且不经过 REST 鉴权。

版本只追加：PATCH 不会改写历史版本，只新增一版并把 `current_revision` 加一。
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.core.pages.service import MAX_BODY_BYTES, MAX_TITLE_CHARS
from flux.errors import BadRequestError, ValidationError

router = APIRouter(prefix="/pages", tags=["pages"])

PageStatus = Literal["draft", "published", "unpublished"]


class PageCreateRequest(BaseModel):
    bot_id: uuid.UUID | None = None
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    body: str = Field(max_length=MAX_BODY_BYTES)
    content_type: str | None = None
    slug: str | None = None
    publish: bool = False


class PageUpdateRequest(BaseModel):
    """只更新给出的字段；任一字段变化都会追加一版，不覆盖旧版本。"""

    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE_CHARS)
    body: str | None = Field(default=None, max_length=MAX_BODY_BYTES)
    content_type: str | None = None


@router.post("")
async def create_page(
    payload: PageCreateRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """创建页面与第 1 版；`publish=true` 时一并发布。"""
    page = await container.pages.create(
        title=payload.title,
        body=payload.body,
        content_type=payload.content_type,
        bot_id=payload.bot_id,
        slug=payload.slug,
        publish=payload.publish,
    )
    return ok(await container.pages.detail(page.id))


@router.get("")
async def list_pages(
    bot_id: str | None = Query(default=None),
    status: PageStatus | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """按 Bot / 状态过滤；列表不带正文。"""
    parsed_bot: uuid.UUID | None = None
    if bot_id is not None:
        try:
            parsed_bot = uuid.UUID(bot_id)
        except ValueError as exc:
            raise BadRequestError("bot_id 不是合法的 UUID", details={"bot_id": bot_id}) from exc
    rows = await container.pages.list(bot_id=parsed_bot, status=status, limit=limit, offset=offset)
    return ok([row.to_dict() for row in rows], metadata={"count": len(rows)})


@router.get("/{page_id}")
async def get_page(
    page_id: uuid.UUID,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """页面详情：元数据 + 当前版本正文与版本号。"""
    return ok(await container.pages.detail(page_id))


@router.patch("/{page_id}")
async def update_page(
    page_id: uuid.UUID,
    payload: PageUpdateRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """保存新版本（旧版本保持可读）。"""
    if payload.title is None and payload.body is None and payload.content_type is None:
        raise ValidationError(
            "没有可更新的字段",
            details={"fields": ["title", "body", "content_type"]},
        )
    await container.pages.update(
        page_id,
        title=payload.title,
        body=payload.body,
        content_type=payload.content_type,
    )
    return ok(await container.pages.detail(page_id))


@router.post("/{page_id}/publish")
async def publish_page(
    page_id: uuid.UUID,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    await container.pages.publish(page_id)
    return ok(await container.pages.detail(page_id))


@router.post("/{page_id}/unpublish")
async def unpublish_page(
    page_id: uuid.UUID,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    await container.pages.unpublish(page_id)
    return ok(await container.pages.detail(page_id))


@router.get("/{page_id}/revisions")
async def list_page_revisions(
    page_id: uuid.UUID,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """版本历史（不带正文，只给字节数与元数据）。"""
    rows = await container.pages.list_revisions(page_id)
    return ok([row.to_summary() for row in rows], metadata={"count": len(rows)})


@router.get("/{page_id}/revisions/{revision}")
async def get_page_revision(
    page_id: uuid.UUID,
    revision: int,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """某一版的完整正文。"""
    row = await container.pages.revision(page_id, revision)
    return ok(row.to_dict())


@router.delete("/{page_id}")
async def delete_page(
    page_id: uuid.UUID,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """删除页面与全部版本；公开链接立即失效。"""
    await container.pages.delete(page_id)
    return ok({"deleted": str(page_id)})
