"""出站草稿箱 API（只读 + 取消，没有绕过审批的发送入口）。

草稿由频道侧在生成回复时落库并挂审批卡；这里的接口只让客户端查看草稿正文与
状态、取消尚未发送的草稿。真正的发送只发生在用户点「允许」后的事件处理里。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.enums import OutboundDraftStatus
from flux.errors import BadRequestError, NotFoundError

router = APIRouter(prefix="/outbound-drafts", tags=["outbound-drafts"])


def _draft_id(value: str) -> uuid.UUID:
    """路径参数非法时按「草稿不存在」处理（与其它资源的 404 语义一致）。"""
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise NotFoundError(f"出站草稿 {value} 不存在", details={"draft_id": value}) from exc


def _bot_uuid(value: str) -> uuid.UUID:
    """过滤参数非法是请求错误，不是 404。"""
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise BadRequestError("bot_id 不是合法的 UUID", details={"bot_id": value}) from exc


def _status(value: str) -> str:
    try:
        return OutboundDraftStatus(value).value
    except ValueError as exc:
        raise BadRequestError(
            f"status 不是合法的草稿状态：{value}",
            details={
                "status": value,
                "allowed": [item.value for item in OutboundDraftStatus],
            },
        ) from exc


@router.get("")
async def list_outbound_drafts(
    bot_id: str | None = None,
    channel: str | None = None,
    status: str | None = None,
    limit: int = 100,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """按 Bot / 频道 / 状态列出草稿，最新在前（正文随响应返回给用户自己看）。"""
    if limit < 1 or limit > 200:
        raise BadRequestError("limit 必须在 1~200 之间", details={"limit": limit})
    rows = await container.outbound_drafts.list(
        bot_id=_bot_uuid(bot_id) if bot_id else None,
        channel=(channel or "").strip() or None,
        status=_status(status) if status else None,
        limit=limit,
    )
    return ok([row.to_dict() for row in rows], metadata={"count": len(rows)})


@router.get("/{draft_id}")
async def get_outbound_draft(
    draft_id: str,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    row = await container.outbound_drafts.get(_draft_id(draft_id))
    return ok(row.to_dict())


@router.post("/{draft_id}/cancel")
async def cancel_outbound_draft(
    draft_id: str,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """取消尚未发送的草稿；已发送 / 已拒绝 / 已取消 / 失败的草稿返回 409。"""
    row = await container.outbound_drafts.cancel(_draft_id(draft_id))
    return ok(row.to_dict())


__all__ = ["router"]
