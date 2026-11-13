"""Bot 后台子任务 API（2026-11-13）。

长活可以派成独立子任务：接口立即返回，用户继续聊天；开始/完成/失败/取消都会
以 kind=subtask 的消息回到父对话，并通过 subtask_update 流事件通知在线客户端。

与 bots.py 共用 Bot 资源与错误语义（未知 Bot / 未知子任务一律 404，终态取消 409）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.enums import TaskStatus
from flux.errors import BadRequestError, NotFoundError
from flux.schemas.api import BotSubtaskCreateRequest

router = APIRouter(prefix="/bots", tags=["bots"])


def _bot_id(value: str) -> uuid.UUID:
    """路径参数非法时按「Bot 不存在」处理（与 bots.py 的 404 语义一致）。"""
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise NotFoundError(f"Bot {value} 不存在", details={"bot_id": value}) from exc


def _subtask_id(value: str) -> uuid.UUID:
    """路径参数非法时按「子任务不存在」处理。"""
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise NotFoundError(f"子任务 {value} 不存在", details={"subtask_id": value}) from exc


@router.post("/{bot_id}/subtasks")
async def create_bot_subtask(
    bot_id: str,
    payload: BotSubtaskCreateRequest,
    response: Response,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """派一条后台子任务：202 立即返回，执行在后台协程里进行。"""
    bot = await container.bots.get(_bot_id(bot_id))
    task = await container.bot_subtasks.create(
        bot,
        instruction=payload.instruction,
        title=payload.title,
    )
    response.status_code = 202
    return ok(task.to_dict())


@router.get("/{bot_id}/subtasks")
async def list_bot_subtasks(
    bot_id: str,
    status: str | None = None,
    limit: int = 50,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """某个 Bot 的后台子任务（创建时间倒序），可按状态过滤。"""
    bot = await container.bots.get(_bot_id(bot_id))
    wanted: TaskStatus | None = None
    if status:
        try:
            wanted = TaskStatus(status)
        except ValueError as exc:
            raise BadRequestError(
                f"status 不是合法的任务状态：{status}",
                details={"status": status, "allowed": [item.value for item in TaskStatus]},
            ) from exc
    if limit < 1 or limit > 200:
        raise BadRequestError("limit 必须在 1~200 之间", details={"limit": limit})
    tasks = await container.bot_subtasks.list(bot, status=wanted, limit=limit)
    return ok([task.to_dict() for task in tasks], metadata={"count": len(tasks)})


@router.get("/{bot_id}/subtasks/{subtask_id}")
async def get_bot_subtask(
    bot_id: str,
    subtask_id: str,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """一条子任务的状态与结果。"""
    bot = await container.bots.get(_bot_id(bot_id))
    task = await container.bot_subtasks.get(bot, _subtask_id(subtask_id))
    return ok(task.to_dict())


@router.post("/{bot_id}/subtasks/{subtask_id}/cancel")
async def cancel_bot_subtask(
    bot_id: str,
    subtask_id: str,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """取消仍在执行的子任务；已经终态时返回 409。"""
    bot = await container.bots.get(_bot_id(bot_id))
    task = await container.bot_subtasks.cancel(bot, _subtask_id(subtask_id))
    return ok(task.to_dict())


__all__ = ["router"]
