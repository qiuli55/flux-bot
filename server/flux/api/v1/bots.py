"""常驻 Bot 管理 API（Bot 模式）。

Bot 与「对话」共用同一套执行链路：每次开工都落一条 tasks 记录（`tasks.bot_id`），
这里只负责任务之外的配置面——名称/头像/Agent/权限/工作目录/定时。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.core.attachment_store import resolve_uploads
from flux.core.desktop.tickets import build_desktop_session
from flux.enums import BotMemoryRelevance, BotStatus, TaskStatus
from flux.errors import AIOSError, BadRequestError, NotFoundError
from flux.logging import get_logger
from flux.models.bot import Bot
from flux.schemas.api import (
    BotCreateRequest,
    BotMemoryCreateRequest,
    BotRoutineCreateRequest,
    BotRoutineUpdateRequest,
    BotRunRequest,
    BotTurnCreateRequest,
    BotUpdateRequest,
    TaskModelRequest,
)

router = APIRouter(prefix="/bots", tags=["bots"])
logger = get_logger(__name__)


def _bot_id(value: str) -> uuid.UUID:
    """路径参数非法时按「Bot 不存在」处理（与任务接口的 404 语义一致）。"""
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise NotFoundError(f"Bot {value} 不存在", details={"bot_id": value}) from exc


def _memory_id(value: str) -> uuid.UUID:
    """路径参数非法时按「记忆不存在」处理。"""
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise NotFoundError(f"记忆 {value} 不存在", details={"memory_id": value}) from exc


def _routine_id(value: str) -> uuid.UUID:
    """路径参数非法时按「例程不存在」处理。"""
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise NotFoundError(f"例程 {value} 不存在", details={"routine_id": value}) from exc


def _group_uuid(value: str) -> uuid.UUID:
    """记忆过滤用的群 id；非法时是请求错误，不是 404。"""
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise BadRequestError("group_id 不是合法的 UUID", details={"group_id": value}) from exc


def _turn_id(value: str) -> uuid.UUID:
    """路径参数非法时按「Turn 不存在」处理（与任务/记忆的 404 语义一致）。"""
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise NotFoundError(f"Turn {value} 不存在", details={"turn_id": value}) from exc


def _parse_message_uuid(value: str) -> uuid.UUID:
    """对话翻页游标里的消息 id；非法时按请求错误拒绝。"""
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise BadRequestError("before_id 不是合法的消息 id", details={"before_id": value}) from exc


def _turn_before(value: str | None) -> datetime | None:
    """分页游标：`before` 是上一页最后一条的 created_at（ISO 8601）。

    为方便客户端，也支持直接传上一页最后一条的 turn id（见 endpoint 里的解析）。
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BadRequestError("before 必须是 ISO 8601 时间", details={"before": value}) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


async def _resolve_turn_before(
    container: Container, bot: Bot, value: str | None
) -> datetime | None:
    if not value:
        return None
    try:
        turn_uuid = uuid.UUID(value)
    except ValueError:
        return _turn_before(value)
    turn = await container.bot_conversation.get_turn(bot.id, turn_uuid)
    return turn.created_at


def _bot_dict(container: Container, bot: Bot, *, tokens_used: int = 0) -> dict[str, Any]:
    data = bot.to_dict()
    # 默认工作目录是派生值，不进库；接口里直接给前端可读的绝对路径
    data["workspace_path"] = str(container.bots.workspace_path(bot))
    # 每日预算的实时用量：账本是唯一权威，按需聚合，不额外落库
    data["tokens_used_today"] = tokens_used
    # cron 与预算窗口按这个时区解释，前端展示 next_run_at 时不用再猜。
    # Bot 自己设置了时区就展示它，否则展示继承的进程级 FLUX_TIMEZONE。
    data["timezone"] = bot.timezone or container.settings.timezone
    return data


async def _bot_payload(container: Container, bot: Bot) -> dict[str, Any]:
    usage = await container.bots.tokens_used_today()
    return _bot_dict(container, bot, tokens_used=usage.get(bot.id, 0))


async def _bot_payloads(container: Container, bots: list[Bot]) -> list[dict[str, Any]]:
    usage = await container.bots.tokens_used_today() if bots else {}
    return [_bot_dict(container, bot, tokens_used=usage.get(bot.id, 0)) for bot in bots]


@router.get("")
async def list_bots(
    status: str | None = None,
    limit: int = 100,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """列出全部 Bot；status=active / paused 用于分组展示。"""
    if status is not None:
        try:
            BotStatus(status)
        except ValueError as exc:
            raise BadRequestError(
                f"status 不是合法的 Bot 状态：{status}",
                details={"status": status, "allowed": [s.value for s in BotStatus]},
            ) from exc
    if limit < 1 or limit > 200:
        raise BadRequestError("limit 必须在 1~200 之间", details={"limit": limit})
    bots = await container.bots.list(status=status, limit=limit)
    return ok(
        await _bot_payloads(container, bots),
        metadata={"count": len(bots)},
    )


@router.post("")
async def create_bot(
    payload: BotCreateRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    """创建 Bot。带 schedule 时立刻算出 next_run_at，后台调度器随即接管。"""
    bot = await container.bots.create(
        name=payload.name,
        description=payload.description,
        avatar_shape=payload.avatar_shape,
        avatar_color=payload.avatar_color,
        agent_id=payload.agent_id,
        model=payload.model,
        key_id=payload.key_id,
        permission_mode=payload.permission_mode,
        decision_mode=payload.decision_mode,
        workspace=payload.workspace,
        schedule=payload.schedule,
        timezone=payload.timezone,
        missed_run_policy=payload.missed_run_policy,
        failure_threshold=payload.failure_threshold,
        token_budget=payload.token_budget,
        notify_on_failure=payload.notify_on_failure,
        event_triggers=payload.event_triggers,
        allowed_tools=payload.allowed_tools,
        allowed_commands=payload.allowed_commands,
        allowed_paths=payload.allowed_paths,
    )
    return ok(await _bot_payload(container, bot))


# --- 固定路径必须排在 /{bot_id} 之前，否则会被路径参数吃掉 ---


@router.get("/tools")
async def list_bot_tools(container: Container = Depends(get_container)) -> dict[str, object]:
    """可为 Bot 配置的技能清单（MCP 工具面，已剔除硬禁令工具）。"""
    tools = container.bots.tools_catalog()
    return ok(tools, metadata={"count": len(tools)})


@router.get("/notifications")
async def list_bot_notifications(
    unread_only: bool = False,
    limit: int = 100,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """Bot 主动通知（当前主要是开工失败）；按时间倒序。"""
    if limit < 1 or limit > 200:
        raise BadRequestError("limit 必须在 1~200 之间", details={"limit": limit})
    rows = await container.bots.list_notifications(unread_only=unread_only, limit=limit)
    unread = await container.bots.unread_notification_count()
    return ok(
        [row.to_dict() for row in rows],
        metadata={"count": len(rows), "unread": unread},
    )


@router.post("/notifications/read-all")
async def read_all_bot_notifications(
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """把全部未读通知标记为已读。"""
    updated = await container.bots.mark_all_notifications_read()
    return ok({"updated": updated})


@router.post("/notifications/{notification_id}/read")
async def read_bot_notification(
    notification_id: str,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """标记单条通知已读（已读的重复调用保持幂等）。"""
    try:
        target = uuid.UUID(notification_id)
    except ValueError as exc:
        raise NotFoundError(
            f"通知 {notification_id} 不存在", details={"notification_id": notification_id}
        ) from exc
    row = await container.bots.mark_notification_read(target)
    if row is None:
        raise NotFoundError(
            f"通知 {notification_id} 不存在", details={"notification_id": notification_id}
        )
    return ok(row.to_dict())


@router.get("/{bot_id}/routines")
async def list_bot_routines(
    bot_id: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    """某个 Bot 的全部例程。"""
    rows = await container.bots.list_routines(_bot_id(bot_id))
    return ok([row.to_dict() for row in rows], metadata={"count": len(rows)})


@router.post("/{bot_id}/routines")
async def create_bot_routine(
    bot_id: str,
    payload: BotRoutineCreateRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """新增例程：自带指令与 cron，到点由后台调度器开工。"""
    row = await container.bots.create_routine(
        _bot_id(bot_id),
        name=payload.name,
        instruction=payload.instruction,
        schedule=payload.schedule,
        enabled=payload.enabled,
    )
    return ok(row.to_dict())


@router.patch("/{bot_id}/routines/{routine_id}")
async def update_bot_routine(
    bot_id: str,
    routine_id: str,
    payload: BotRoutineUpdateRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """部分更新例程；只改请求里出现的字段。"""
    row = await container.bots.update_routine(
        _bot_id(bot_id),
        _routine_id(routine_id),
        payload.model_dump(exclude_unset=True),
    )
    return ok(row.to_dict())


@router.delete("/{bot_id}/routines/{routine_id}")
async def delete_bot_routine(
    bot_id: str,
    routine_id: str,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """删除例程；Bot 本身与历史任务不受影响。"""
    target = _routine_id(routine_id)
    await container.bots.delete_routine(_bot_id(bot_id), target)
    return ok({"deleted": str(target)})


@router.get("/{bot_id}")
async def get_bot(bot_id: str, container: Container = Depends(get_container)) -> dict[str, object]:
    bot = await container.bots.get(_bot_id(bot_id))
    return ok(await _bot_payload(container, bot))


@router.patch("/{bot_id}")
async def update_bot(
    bot_id: str,
    payload: BotUpdateRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """部分更新：只改请求里出现的字段（exclude_unset 保留「没传」与「传 null」的区别）。"""
    changes = payload.model_dump(exclude_unset=True)
    bot = await container.bots.update(_bot_id(bot_id), changes)
    return ok(await _bot_payload(container, bot))


@router.post("/{bot_id}/model")
async def set_bot_model(
    bot_id: str,
    payload: TaskModelRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """切换 Bot 的默认执行模型；`model` 留空 = 回到平台默认。

    单列一个接口而不是走 PATCH：App 的 HttpURLConnection 对 PATCH 支持不稳，
    切换模型只需要改 model / key_id 两个字段。
    """
    bot = await container.bots.update(
        _bot_id(bot_id),
        {"model": payload.model, "key_id": payload.key_id},
    )
    return ok(await _bot_payload(container, bot))


@router.delete("/{bot_id}")
async def delete_bot(
    bot_id: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    """删除 Bot；历史开工任务保留（tasks.bot_id 由外键置空）。"""
    target = _bot_id(bot_id)
    await container.bots.delete(target)
    return ok({"deleted": str(target)})


@router.post("/{bot_id}/run")
async def run_bot(
    bot_id: str,
    payload: BotRunRequest | None = None,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """手动开工一次。`instruction` 留空则用 Bot 自己的 description。"""
    target = _bot_id(bot_id)
    task = await container.bots.run_now(
        target, instruction=(payload.instruction if payload else None)
    )
    bot = await container.bots.get(target)
    return ok({"bot": await _bot_payload(container, bot), "task": task.to_dict()})


@router.get("/{bot_id}/tasks")
async def list_bot_tasks(
    bot_id: str,
    status: str | None = None,
    limit: int = 50,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """某个 Bot 的开工记录（按创建时间倒序）。"""
    target = await container.bots.get(_bot_id(bot_id))
    wanted: TaskStatus | None = None
    if status:
        try:
            wanted = TaskStatus(status)
        except ValueError as exc:
            raise BadRequestError(
                f"status 不是合法的任务状态：{status}",
                details={"status": status, "allowed": [s.value for s in TaskStatus]},
            ) from exc
    if limit < 1 or limit > 200:
        raise BadRequestError("limit 必须在 1~200 之间", details={"limit": limit})
    tasks = await container.task_repo.list(bot_id=target.id, status=wanted, limit=limit)
    return ok([t.to_dict() for t in tasks], metadata={"count": len(tasks)})


@router.post("/{bot_id}/turns")
async def create_bot_turn(
    bot_id: str,
    payload: BotTurnCreateRequest,
    response: Response,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """给 Bot 发一条对话消息：入队后立即返回 202（重放 client_turn_id 返回 200）。

    预算在开始前检查；超限返回 409，错误码 `budget_exhausted`（与手动开工一致）。
    Bot 暂停时同样 409，恢复后才能继续对话。
    """
    bot = await container.bots.get(_bot_id(bot_id))
    files = resolve_uploads(container.settings, payload.attachment_ids)
    turn, created = await container.bot_conversation.create_turn(
        bot,
        content=payload.content,
        files=files,
        client_turn_id=payload.client_turn_id,
    )
    response.status_code = 202 if created else 200
    return ok({"turn": await container.bot_conversation.turn_payload(turn)})


@router.get("/{bot_id}/turns")
async def list_bot_turns(
    bot_id: str,
    limit: int = 50,
    before: str | None = None,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """最近的 Bot 对话轮次（按创建时间倒序）；before 为上一页最后一条的 created_at。"""
    bot = await container.bots.get(_bot_id(bot_id))
    if limit < 1 or limit > 200:
        raise BadRequestError("limit 必须在 1~200 之间", details={"limit": limit})
    turns = await container.bot_conversation.list_turns(
        bot.id, limit=limit, before=await _resolve_turn_before(container, bot, before)
    )
    payloads = [await container.bot_conversation.turn_payload(turn) for turn in turns]
    return ok(
        payloads,
        metadata={
            "count": len(payloads),
            "next_before": payloads[-1]["created_at"] if payloads else None,
        },
    )


@router.get("/{bot_id}/turns/{turn_id}")
async def get_bot_turn(
    bot_id: str,
    turn_id: str,
    wait: float = Query(default=0, ge=0, le=30),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """查询一轮对话；`wait` 秒内等待状态变化（长轮询），终态立即返回。"""
    target_bot = _bot_id(bot_id)
    target_turn = _turn_id(turn_id)
    await container.bots.get(target_bot)
    turn = await container.bot_conversation.wait_for_turn(
        target_bot, target_turn, wait_seconds=wait
    )
    return ok({"turn": await container.bot_conversation.turn_payload(turn)})


@router.get("/{bot_id}/turns/{turn_id}/stream")
async def stream_bot_turn(
    bot_id: str,
    turn_id: str,
    after_seq: int = 0,
    container: Container = Depends(get_container),
) -> StreamingResponse:
    """一轮对话的 SSE 增量（assistant_start / delta / message / error）。

    `bot_turns` 仍是真相源；这里只是把 task_stream 中带同一 turn_id 的事件转发出去。
    回合已结束时先补缓冲再关闭连接，避免客户端连上来后一直等不到收尾。
    """
    target_bot = _bot_id(bot_id)
    target_turn = _turn_id(turn_id)
    if after_seq < 0:
        raise BadRequestError("after_seq 不能为负数", details={"after_seq": after_seq})
    await container.bots.get(target_bot)
    turn = await container.bot_conversation.get_turn(target_bot, target_turn)
    terminal = {"completed", "failed", "cancelled"}

    def _format(event: Any) -> str:
        data = json.dumps(event.to_dict(), ensure_ascii=False)
        return f"id: {event.seq}\nevent: turn.event\ndata: {data}\n\n"

    async def generate() -> AsyncIterator[str]:
        if turn.status in terminal:
            for event in container.task_stream.poll(turn.task_id, after_seq=after_seq):
                if event.data.get("turn_id") == str(target_turn):
                    yield _format(event)
            return
        async for event in container.task_stream.stream(turn.task_id, after_seq=after_seq):
            if event is None:
                yield ": keep-alive\n\n"
                continue
            if event.data.get("turn_id") != str(target_turn):
                continue
            yield _format(event)
            if event.type in ("assistant_message", "assistant_error"):
                current = await container.bot_conversation.get_turn(target_bot, target_turn)
                if current.status in terminal:
                    return

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{bot_id}/conversation")
async def get_bot_conversation(
    bot_id: str,
    limit: int = 50,
    before_id: str | None = None,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """Bot 当前对话任务的消息（seq 升序）；还没有对话时返回空列表。

    `before_id` 可传消息 seq（向上翻页游标），也可传一条消息的 UUID
    （服务端换算成它的 seq），两种客户端都容易用。
    """
    bot = await container.bots.get(_bot_id(bot_id))
    if limit < 1 or limit > 200:
        raise BadRequestError("limit 必须在 1~200 之间", details={"limit": limit})
    before_seq: int | None = None
    if before_id:
        if before_id.isdigit():
            before_seq = int(before_id)
        elif bot.chat_task_id is not None:
            message = await container.task_repo.get_message(
                bot.chat_task_id, _parse_message_uuid(before_id)
            )
            if message is not None:
                before_seq = message.seq
    chat_task_id, messages = await container.bot_conversation.conversation(
        bot, limit=limit, before_id=before_seq
    )
    return ok(
        {
            "chat_task_id": str(chat_task_id) if chat_task_id is not None else None,
            "messages": [message.to_dict() for message in messages],
        },
        metadata={"count": len(messages)},
    )


@router.get("/{bot_id}/memories")
async def list_bot_memories(
    bot_id: str,
    relevance: str | None = None,
    group_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """某个 Bot 的长期记忆摘要（第一人称视角），可按相关性与来源群过滤。"""
    bot = await container.bots.get(_bot_id(bot_id))
    wanted: BotMemoryRelevance | None = None
    if relevance:
        try:
            wanted = BotMemoryRelevance(relevance)
        except ValueError as exc:
            raise BadRequestError(
                f"relevance 不是合法的相关性：{relevance}",
                details={
                    "relevance": relevance,
                    "allowed": [item.value for item in BotMemoryRelevance],
                },
            ) from exc
    if limit < 1 or limit > 200:
        raise BadRequestError("limit 必须在 1~200 之间", details={"limit": limit})
    if offset < 0:
        raise BadRequestError("offset 不能为负数", details={"offset": offset})
    rows = await container.bot_memories.list_memories(
        bot.id,
        relevance=wanted.value if wanted else None,
        group_id=_group_uuid(group_id) if group_id else None,
        limit=limit,
        offset=offset,
    )
    return ok([row.to_dict() for row in rows], metadata={"count": len(rows)})


@router.get("/{bot_id}/memories/search")
async def search_bot_memories(
    bot_id: str,
    q: str,
    limit: int = 20,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """全文检索：从只追加的原文层里找回「当时到底怎么说的」。"""
    bot = await container.bots.get(_bot_id(bot_id))
    if not q.strip():
        raise BadRequestError("q 不能为空", details={"q": q})
    if limit < 1 or limit > 100:
        raise BadRequestError("limit 必须在 1~100 之间", details={"limit": limit})
    rows = await container.bot_memories.search(bot.id, q, limit=limit)
    return ok(rows, metadata={"count": len(rows)})


@router.get("/{bot_id}/memories/{memory_id}")
async def get_bot_memory(
    bot_id: str,
    memory_id: str,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """一条记忆的摘要 + 全文副本。"""
    bot = await container.bots.get(_bot_id(bot_id))
    data = await container.bot_memories.detail(_memory_id(memory_id))
    if data["bot_id"] != str(bot.id):
        raise NotFoundError(
            f"记忆 {memory_id} 不存在", details={"memory_id": memory_id, "bot_id": bot_id}
        )
    return ok(data)


@router.post("/{bot_id}/memories")
async def create_bot_memory(
    bot_id: str,
    payload: BotMemoryCreateRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """手工给 Bot 记一条记忆（不参与摘要合并，直接成为独立一条）。"""
    bot = await container.bots.get(_bot_id(bot_id))
    row = await container.bot_memories.add_manual(
        bot.id,
        content=payload.content,
        kind=payload.kind,
        relevance=payload.relevance,
    )
    return ok(row.to_dict())


@router.delete("/{bot_id}/memories/{memory_id}")
async def delete_bot_memory(
    bot_id: str,
    memory_id: str,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """删除一条记忆（全文副本级联删除）。"""
    bot = await container.bots.get(_bot_id(bot_id))
    data = await container.bot_memories.detail(_memory_id(memory_id))
    if data["bot_id"] != str(bot.id):
        raise NotFoundError(
            f"记忆 {memory_id} 不存在", details={"memory_id": memory_id, "bot_id": bot_id}
        )
    target = _memory_id(memory_id)
    await container.bot_memories.delete(target)
    return ok({"deleted": str(target)})


@router.delete("/{bot_id}/memories")
async def clear_bot_memories(
    bot_id: str,
    group_id: str | None = None,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """清空该 Bot 的记忆；传 group_id 时只清某个群沉淀的部分。"""
    bot = await container.bots.get(_bot_id(bot_id))
    if group_id:
        removed = await container.bot_memories.delete_by_group(bot.id, _group_uuid(group_id))
    else:
        removed = await container.bot_memories.clear(bot.id)
    return ok({"deleted": removed})


@router.post("/{bot_id}/desktop/session")
async def create_desktop_session(
    bot_id: str,
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """签发某一台 Bot 的云桌面会话（票据按 Bot 隔离）。

    未配置桌面网关时返回 503 `desktop_not_configured`。profile 完全由服务端按
    Bot UUID 计算，客户端不能自报；票据密钥未配置时 ticket 为 null。
    """
    settings = container.settings
    host = (settings.desktop_host or "").strip()
    gateway_token = (settings.desktop_gateway_token or "").strip()
    if not host or not gateway_token:
        raise AIOSError(
            "云桌面未配置",
            code="desktop_not_configured",
            http_status=503,
        )
    bot = await container.bots.get(_bot_id(bot_id))
    principal = getattr(request.state, "principal", None) or {"kind": "local"}
    data = build_desktop_session(settings, bot_id=bot.id, principal=principal)
    logger.info(
        "desktop.session.issue principal=%s bot=%s profile=%s",
        principal.get("kind"),
        bot.id,
        data["profile"],
    )
    return ok(data)


__all__ = ["router"]
