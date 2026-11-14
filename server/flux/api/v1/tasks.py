"""Task API（主规格 §12.4）。

任务状态落 tasks 表（本增量起），进程内的调度队列是易失结构、不承担状态权威。
对话消息落 task_messages 表（任务执行中心增量）：刷新页面、重启后端都不丢；
助手回复由平台经 ModelRouter 真实调用生成（见 core/task_engine/assistant.py）。
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse

from flux.api.account_scope import account_key_for_request
from flux.api.auth import current_cloud_user_id, get_current_cloud_user_id
from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.core.agent_runtime.run_repository import as_utc, utcnow
from flux.core.agent_runtime.runtimes.base import DEFAULT_RUNTIME
from flux.core.agent_runtime.trace import summarize_trace
from flux.core.attachment_store import resolve_uploads
from flux.core.bot.conversation import attachments_brief, record_attachments
from flux.core.components.actions import find_action, route_component_action
from flux.core.event.bus import Events
from flux.core.group.avatar import normalize_avatar
from flux.core.task_engine.approvals import APPROVAL_ALLOW
from flux.core.task_engine.assistant import validate_confirmation_items
from flux.core.task_engine.context_usage import (
    CONTEXT_MESSAGE_KINDS,
    context_message_limit,
    measure_context,
)
from flux.core.task_engine.decisions import build_decision, reject_decision, resolve_decision
from flux.core.task_engine.repository import MessageDraft
from flux.core.task_engine.secure_input import store_secure_input
from flux.enums import (
    DecisionMode,
    DecisionStatus,
    DshRunStatus,
    TaskMessageRole,
    TaskOrigin,
    TaskStatus,
)
from flux.errors import BadRequestError, ConflictError, NotFoundError, ValidationError
from flux.logging import get_logger
from flux.models.task import Task
from flux.schemas.api import (
    ComponentActionRequest,
    ConfirmationUpdateRequest,
    DecisionChooseRequest,
    DecisionCreateRequest,
    DecisionModeRequest,
    PermissionModeRequest,
    SecureInputRequest,
    TaskAgentRequest,
    TaskCreateRequest,
    TaskMessageCreateRequest,
    TaskModelRequest,
    TaskStartRequest,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])

logger = get_logger(__name__)

_FINAL_STATUSES = {TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value, TaskStatus.FAILED.value}

#: 生成交接摘要时最多回看的历史消息条数（更早的由摘要器按字符预算丢弃并标注）
_HANDOFF_MESSAGE_LIMIT = 120


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_task_sse(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False)
    return f"id: {payload['seq']}\nevent: task.event\ndata: {data}\n\n"


def _confirmation_text(items: list[dict[str, str]]) -> str:
    """把确认卡拍成一条可读消息（聊天记录里能直接看懂，不依赖前端渲染）。"""
    lines = ["需求确认（最终版本）："]
    lines.extend(f"- {item['label']}：{item['value']}" for item in items)
    return "\n".join(lines)


async def _owned_task(
    container: Container, task_uuid: uuid.UUID, cloud_user_id: str | None
) -> Task:
    """REST 面统一按人取任务：不属于当前人一律 404。"""
    return await container.task_repo.get_owned(
        task_uuid,
        cloud_user_id=cloud_user_id,
        owner_cloud_user_id=container.settings.owner_cloud_user_id,
    )


async def _require_open_task(
    container: Container,
    task_uuid: uuid.UUID,
    task_id: str,
    cloud_user_id: str | None,
) -> Task:
    """取任务并确认它还能被操作（终态任务一律 409，与追加消息同一口径）。"""
    task = await _owned_task(container, task_uuid, cloud_user_id)
    if task.status in _FINAL_STATUSES:
        raise ConflictError(
            f"任务 {task_id} 已处于终态 {task.status}，不能再执行该操作",
            details={"task_id": task_id, "status": task.status},
        )
    return task


def _task_dict(task: Task) -> dict[str, object]:
    """任务响应体：M0 冻结的 7 个键 + created_at（任务执行中心展示真实创建时间）。"""
    return task.to_dict()


def _parse_uuid(value: str, *, field: str) -> uuid.UUID:
    """请求体里的标识串必须是合法 UUID，否则按 400 拒绝。"""
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise BadRequestError(f"{field} 不是合法 UUID：{value}", details={field: value}) from exc


def _task_id(value: str) -> uuid.UUID:
    """路径参数里的任务标识非法时按「任务不存在」处理，与 M0 的 404 语义一致（不返回 422）。"""
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise NotFoundError(f"任务 {value} 不存在", details={"task_id": value}) from exc


@router.post("")
async def create_task(
    payload: TaskCreateRequest,
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    # 新建任务必须能确定归属人：分不清是谁就 403，绝不落一条无主任务。
    cloud_user_id = account_key_for_request(request, container.settings)
    project_id = _parse_uuid(payload.project_id, field="project_id") if payload.project_id else None
    agent_id = _parse_uuid(payload.agent_id, field="agent_id") if payload.agent_id else None
    runtime = DEFAULT_RUNTIME
    if agent_id is not None:
        # Agent 注册表是 Agent 存在性的唯一权威：API 边界 fail-closed。
        agent = container.agents.get(payload.agent_id)
        runtime = str(getattr(getattr(agent, "spec", None), "runtime", DEFAULT_RUNTIME))
    model, key_id = await container.key_vault.resolve_selection(
        runtime=runtime,
        model=payload.model or "",
        key_id=payload.key_id,
        cloud_user_id=cloud_user_id,
    )
    avatar_shape, avatar_color = normalize_avatar(payload.avatar_shape, payload.avatar_color)

    # 附件先校验：不合格就别把任务建出来，免得留下一条空对话
    files = resolve_uploads(container.settings, payload.attachment_ids)
    task = await container.task_repo.create(
        task_id=uuid.uuid4(),
        description=payload.description,
        priority=payload.priority,
        project_id=project_id,
        agent_id=agent_id,
        decision_mode=payload.decision_mode,
        permission_mode=payload.permission_mode,
        model=model,
        api_key_id=key_id,
        origin=payload.origin,
        bot_name=payload.bot_name,
        avatar_shape=avatar_shape,
        avatar_color=avatar_color,
        cloud_user_id=cloud_user_id,
    )
    # 从旧对话接手：摘要落成一条 kind=summary 的助手消息。它同时进入助手历史
    # 与 Agent 执行指令（TaskRunService._latest_summary），新对话因此不是"从零开始"，
    # 但也不会拷贝任何旧消息。
    summary = (payload.summary or "").strip()
    if summary:
        source_id = (
            _parse_uuid(payload.summary_from, field="summary_from")
            if payload.summary_from
            else None
        )
        await container.task_repo.add_messages(
            task.id,
            [
                MessageDraft(
                    role=TaskMessageRole.ASSISTANT.value,
                    kind="summary",
                    content=summary,
                    payload={"from_task_id": str(source_id) if source_id else None},
                )
            ],
        )
        logger.info(
            "新对话带入前情摘要 task=%s from=%s 字数=%d",
            task.id,
            source_id or "未提供",
            len(summary),
        )
    await record_attachments(container, task.id, files)
    # 建 bot 时只落对话、不启动执行：否则会拿 bot 名字当需求直接跑 Agent。
    if payload.start:
        container.scheduler.submit(
            str(task.id),
            task.description,
            priority=task.priority,
            agent_id=str(agent_id) if agent_id is not None else None,
        )
    result = _task_dict(task)
    await container.bus.publish(Events.TASK_CREATED, result)
    return ok(result)


@router.get("")
async def list_tasks(
    project_id: str | None = None,
    status: str | None = None,
    origin: str | None = None,
    bot_id: str | None = None,
    include_bot_tasks: bool = False,
    limit: int = 50,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """列出任务（任务执行中心的任务列表）。默认按创建时间倒序。

    `bot_id` 只看某个 Bot 的开工记录；不传时默认把 Bot 任务挡在对话列表之外，
    需要连同 Bot 任务一起看时显式传 `include_bot_tasks=true`。
    """
    project_uuid = _parse_uuid(project_id, field="project_id") if project_id else None
    bot_uuid = _parse_uuid(bot_id, field="bot_id") if bot_id else None
    wanted: TaskStatus | None = None
    if status:
        try:
            wanted = TaskStatus(status)
        except ValueError as exc:
            raise BadRequestError(
                f"status 不是合法的任务状态：{status}",
                details={"status": status, "allowed": [s.value for s in TaskStatus]},
            ) from exc
    origin_filter: TaskOrigin | None = None
    if origin:
        try:
            origin_filter = TaskOrigin(origin)
        except ValueError as exc:
            raise BadRequestError(
                f"origin 不是合法的对话来源：{origin}",
                details={"origin": origin, "allowed": [o.value for o in TaskOrigin]},
            ) from exc
    if limit < 1 or limit > 200:
        raise BadRequestError("limit 必须在 1~200 之间", details={"limit": limit})
    tasks = await container.task_repo.list(
        project_id=project_uuid,
        status=wanted,
        origin=origin_filter,
        bot_id=bot_uuid,
        exclude_bot_tasks=bot_uuid is None and not include_bot_tasks,
        limit=limit,
        cloud_user_id=cloud_user_id,
        owner_cloud_user_id=container.settings.owner_cloud_user_id,
        scope_to_user=True,
    )
    return ok([_task_dict(t) for t in tasks], metadata={"count": len(tasks)})


@router.post("/context-sync")
async def sync_context(container: Container = Depends(get_container)) -> dict[str, object]:
    """手动触发一次对话上下文上云（隐私输入以外的消息），返回本轮统计。"""
    result = await container.context_cloud.push()
    return ok(result)


@router.get("/{task_id}")
async def get_task(
    task_id: str,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    task = await _owned_task(container, _task_id(task_id), cloud_user_id)
    return ok(_task_dict(task))


@router.get("/{task_id}/messages")
async def list_task_messages(
    task_id: str,
    limit: int = 30,
    before: int | None = None,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """按 seq 升序返回一段任务消息；before 为向上加载更早消息的游标。"""
    if limit < 1 or limit > 200:
        raise BadRequestError("limit 必须在 1~200 之间", details={"limit": limit})
    task_uuid = _task_id(task_id)
    await _owned_task(container, task_uuid, cloud_user_id)
    messages, has_more = await container.task_repo.list_messages(
        task_uuid, limit=limit, before=before
    )
    return ok(
        [m.to_dict() for m in messages],
        metadata={"count": len(messages), "has_more": has_more},
    )


def _run_summary(run: Any, stats: dict[str, int]) -> dict[str, object]:
    """一轮 Run 的摘要：耗时 + 概览计数（对话里的「任务耗时」入口与详情页眉用）。"""
    duration_ms: int | None = None
    if run.started_at is not None:
        end = run.finished_at if run.finished_at is not None else utcnow()
        duration_ms = max(0, int((as_utc(end) - as_utc(run.started_at)).total_seconds() * 1000))
    return {
        "run_id": run.id,
        "task_id": str(run.task_id) if run.task_id else None,
        "agent_id": run.agent_id,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_ms": duration_ms,
        "commands": stats.get("commands", 0),
        "files_read": stats.get("files_read", 0),
        "files_changed": stats.get("files_changed", 0),
    }


@router.get("/{task_id}/runs")
async def list_task_runs(
    task_id: str,
    limit: int = 20,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """任务名下的 Run 摘要（按时间升序）：耗时与「改动/命令/读取」计数。"""
    if limit < 1 or limit > 100:
        raise BadRequestError("limit 必须在 1~100 之间", details={"limit": limit})
    task_uuid = _task_id(task_id)
    await _owned_task(container, task_uuid, cloud_user_id)
    runs = await container.run_repo.list_by_task(task_uuid, limit=limit)
    rows = await container.run_trace_repo.list_for_runs([run.id for run in runs])
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["run_id"]), []).append(row)
    return ok([_run_summary(run, summarize_trace(grouped.get(run.id, []))) for run in runs])


@router.get("/{task_id}/runs/{run_id}/trace")
async def get_run_trace(
    task_id: str,
    run_id: str,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """一轮 Run 的完整执行轨迹：概览 + 思考过程 + 命令（含输出）。"""
    task_uuid = _task_id(task_id)
    await _owned_task(container, task_uuid, cloud_user_id)
    run = await container.run_repo.get(run_id)
    if run is None or run.task_id != task_uuid:
        raise NotFoundError(
            f"Run {run_id} 不属于任务 {task_id}",
            details={"task_id": task_id, "run_id": run_id},
        )
    rows = await container.run_trace_repo.list_for_run(run_id)
    items = [row.to_dict() for row in rows]
    data = _run_summary(run, summarize_trace(items))
    data["items"] = items
    return ok(data)


@router.get("/{task_id}/context")
async def get_task_context(
    task_id: str,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """对话上下文占用（「上下文快满」提醒的唯一口径）。

    度量的不是模型标称窗口，而是执行 Agent 下一轮实际会收到的内容：
    与 `TaskRunService.start()` 同源装配的执行指令（约束 / 任务 / 工作记忆 /
    压缩后的对话轨迹 / 计划）。历史已压缩在指令里，这里不再把原始消息重复计入，
    否则对话一长就永远停在 100%。条数只作为解释信息返回。
    """
    task_uuid = _task_id(task_id)
    task = await _owned_task(container, task_uuid, cloud_user_id)
    # 与真实开跑共用一套装配，避免「占用条」与「Agent 实际收到」两套口径
    instruction = await container.task_run_service.preview_instruction(task)
    limit = context_message_limit()
    _, total = await container.task_repo.context_snapshot(
        task_uuid, limit=limit, kinds=CONTEXT_MESSAGE_KINDS
    )
    usage = measure_context([], message_count=total, limit=limit, extra_chars=len(instruction))
    return ok(usage.to_dict())


@router.post("/{task_id}/handoff")
async def create_task_handoff(
    task_id: str,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """把一段对话压成前情摘要，供客户端带进新对话。

    只生成摘要、不建任务：新对话由客户端在用户发出第一条消息时创建
    （`POST /tasks` 的 summary / summary_from 字段），避免留下一段
    没有用户意图的孤儿对话。
    """
    task_uuid = _task_id(task_id)
    task = await _owned_task(container, task_uuid, cloud_user_id)
    if task.status == TaskStatus.RUNNING.value:
        raise ConflictError(
            "这段对话正在执行，结束后再新建带摘要的对话",
            details={"task_id": task_id, "status": task.status},
        )
    messages, _ = await container.task_repo.list_messages(task_uuid, limit=_HANDOFF_MESSAGE_LIMIT)
    pairs = [
        (message.role, message.content)
        for message in messages
        if message.kind in CONTEXT_MESSAGE_KINDS and message.content.strip()
    ]
    summary = await container.conversation_summarizer.summarize(
        description=task.description, messages=pairs
    )
    logger.info(
        "生成对话交接摘要 task=%s 来源消息=%d 截断=%s",
        task_uuid,
        summary.source_messages,
        summary.truncated,
    )
    data = summary.to_dict()
    data["from_task_id"] = str(task_uuid)
    return ok(data)


@router.post("/{task_id}/messages")
async def create_task_message(
    task_id: str,
    payload: TaskMessageCreateRequest,
    request: Request,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """发一条消息并取回助手的真实回复。

    用户消息先落库（模型调用失败时它也不会丢，刷新页面仍在），
    随后调用模型生成回复并落库——两个步骤都是真实数据，服务端不接受伪造的 assistant 消息。
    """
    task_uuid = _task_id(task_id)
    task = await _owned_task(container, task_uuid, cloud_user_id)
    content = payload.content.strip()
    files = resolve_uploads(container.settings, payload.attachment_ids)
    if not content and not files:
        raise ValidationError(
            "消息内容不能为空", details={"task_id": task_id, "attachments": len(files)}
        )
    if task.status in _FINAL_STATUSES:
        raise ConflictError(
            f"任务 {task_id} 已处于终态 {task.status}，不能再追加消息",
            details={"task_id": task_id, "status": task.status},
        )

    # 对话不是连接器管理动作：分不清是谁时仍可聊天，只是不注入 GitHub 工具。
    principal = getattr(request.state, "principal", None) or {}
    account_key = current_cloud_user_id(principal, container.settings) or ""
    result = await container.bot_conversation.run_turn(
        task,
        content=content,
        files=files,
        account_key=account_key,
    )
    return ok(
        {
            "task": _task_dict(result.task),
            "messages": [
                result.user_message.to_dict(),
                result.reply_message.to_dict(),
            ],
        },
        metadata={
            "provider": result.assistant_turn.provider,
            "model": result.assistant_turn.model,
        },
    )


@router.post("/{task_id}/secure-input")
async def submit_secure_input(
    task_id: str,
    payload: SecureInputRequest,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """安全输入卡片的提交入口：明文只在这里出现一次，落库即加密，接口只回掩码。

    用户填的密钥**不进 task_messages**：消息表只留一条「已安全存储」的回执，
    正文与 payload 都不含明文，因此后续上传云端上下文时也不会带出密钥。
    """
    task_uuid = _task_id(task_id)
    task = await _owned_task(container, task_uuid, cloud_user_id)
    if task.status in _FINAL_STATUSES:
        raise ConflictError(
            f"任务 {task_id} 已处于终态 {task.status}，不能再提交密钥",
            details={"task_id": task_id, "status": task.status},
        )

    card_uuid = _parse_uuid(payload.message_id, field="message_id")
    card = await container.task_repo.get_message(task_uuid, card_uuid)
    if card is None:
        raise NotFoundError(
            "安全输入卡片不存在或不属于该任务",
            details={"task_id": task_id, "message_id": payload.message_id},
        )
    result = await store_secure_input(container, task=task, card=card, secret=payload.value)
    key = result["key"]
    stored = result["message"]
    return ok(
        {
            "task": _task_dict(task),
            "key": key.to_dict(),
            "message": stored.to_dict(),
        }
    )


@router.post("/{task_id}/messages/{message_id}/actions")
async def submit_component_action(
    task_id: str,
    message_id: str,
    payload: ComponentActionRequest,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """组件卡片的动作回调（决策按钮 / 安全输入 / 选项提交）。

    服务端按 `action_id` 从落库的组件树里找回动作，不执行客户端给的任何 URL 或
    内部动作名。消息必须属于当前用户可访问的任务；动作不存在或已失效一律 404。
    """
    task_uuid = _task_id(task_id)
    task = await _require_open_task(container, task_uuid, task_id, cloud_user_id)
    message_uuid = _parse_uuid(message_id, field="message_id")
    message = await container.task_repo.get_message(task_uuid, message_uuid)
    if message is None:
        raise NotFoundError(
            "消息不存在或不属于该任务",
            details={"task_id": task_id, "message_id": message_id},
        )
    action = find_action(message, payload.action_id)
    if action is None:
        raise NotFoundError(
            "操作不存在或已失效",
            details={"action_id": payload.action_id, "message_id": message_id},
        )
    result = await route_component_action(
        container,
        task=task,
        message=message,
        action=action,
        value=payload.value,
        option=payload.option,
        note=payload.note,
        cloud_user_id=cloud_user_id,
    )
    return ok(result)


@router.get("/{task_id}/stream")
async def stream_task(
    task_id: str,
    after_seq: int = 0,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> StreamingResponse:
    """对话增量流（SSE）：助手逐字回复 + Agent 运行事件。

    连接建立后先补 `after_seq` 之后的缓冲事件，再持续推送，15 秒静默发一次心跳。
    最终消息仍以 GET /tasks/{id}/messages 为准：断线期间漏掉的增量由客户端重拉补齐。
    """
    task_uuid = _task_id(task_id)
    await _owned_task(container, task_uuid, cloud_user_id)
    resume_from = after_seq
    if last_event_id:
        with suppress(ValueError):
            resume_from = max(resume_from, int(last_event_id))

    async def generate() -> AsyncIterator[str]:
        async for event in container.task_stream.stream(task_uuid, after_seq=resume_from):
            yield ": keep-alive\n\n" if event is None else _format_task_sse(event.to_dict())

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{task_id}/events")
async def poll_task_events(
    task_id: str,
    after_seq: int = 0,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """对话增量的轮询版（账号云中转只做请求-响应，不能承载 SSE 长连接）。

    与 `/stream` 共用同一份缓冲：返回 `after_seq` 之后的事件，客户端按回调逐条消费即可。
    """
    if after_seq < 0:
        raise BadRequestError("after_seq 不能为负数", details={"after_seq": after_seq})
    task_uuid = _task_id(task_id)
    await _owned_task(container, task_uuid, cloud_user_id)
    events = container.task_stream.poll(task_uuid, after_seq=after_seq)
    return ok(
        [event.to_dict() for event in events],
        metadata={"count": len(events), "after_seq": after_seq},
    )


@router.post("/{task_id}/continue")
async def continue_task(
    task_id: str,
    payload: TaskMessageCreateRequest,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """同一对话续跑：终态对话收到新消息时，重开任务并起一轮新 Run。

    与 `/messages` 的区别：那个只让助手回话；续跑把新消息当作本轮 Agent 指令，
    让同一个对话接着执行（历史消息、项目关联与标题都保留）。
    """
    task_uuid = _task_id(task_id)
    task = await _owned_task(container, task_uuid, cloud_user_id)
    content = payload.content.strip()
    files = resolve_uploads(container.settings, payload.attachment_ids)
    if not content and not files:
        raise ValidationError(
            "消息内容不能为空", details={"task_id": task_id, "attachments": len(files)}
        )
    if task.status not in _FINAL_STATUSES:
        raise ConflictError(
            f"对话 {task_id} 仍在进行中，直接追加消息即可，不需要续跑",
            details={"task_id": task_id, "status": task.status},
        )
    previous_status = task.status
    task = await container.task_repo.reopen(task_uuid)
    user_message = (
        await container.task_repo.add_messages(
            task_uuid,
            [MessageDraft(role=TaskMessageRole.USER.value, content=content)],
        )
    )[0]
    await container.task_stream.publish(
        task_uuid, "user_message", {"message": user_message.to_dict()}
    )
    await record_attachments(container, task_uuid, files)
    followup = content or attachments_brief(files)
    try:
        result = await container.task_run_service.start(task, followup=followup)
    except Exception:
        # 起不来时退回原终态：用户消息保留，修好 runtime 后可以直接重试，
        # 不留下一个没有 Run 支撑的 running 僵尸。
        current = await container.task_repo.get(task_uuid)
        if current.run_id is None and current.status in {
            TaskStatus.PENDING.value,
            TaskStatus.RUNNING.value,
        }:
            await container.task_repo.set_status(task_uuid, TaskStatus(previous_status))
        raise
    return ok(
        {
            "task": _task_dict(result.task),
            "messages": [user_message.to_dict()],
        }
    )


@router.put("/{task_id}/confirmation")
async def update_confirmation(
    task_id: str,
    payload: ConfirmationUpdateRequest,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """用户修改需求确认（P0-06）：六个维度必须齐全，落库后就是执行依据。

    改完会追加一条 kind=confirmation 的用户消息——用户改了什么必须留在对话轨迹里，
    否则"执行用的是哪一版需求"事后无从解释（TC-103）。
    """
    task_uuid = _task_id(task_id)
    await _require_open_task(container, task_uuid, task_id, cloud_user_id)
    items = validate_confirmation_items([item.model_dump() for item in payload.items])
    confirmation = {"items": items, "actor": "user", "updated_at": _now_iso()}
    task = await container.task_repo.set_confirmation(task_uuid, confirmation)
    message = (
        await container.task_repo.add_messages(
            task_uuid,
            [
                MessageDraft(
                    role=TaskMessageRole.USER.value,
                    kind="confirmation",
                    content=_confirmation_text(items),
                    payload={"confirmation": confirmation},
                )
            ],
        )
    )[0]
    return ok({"task": _task_dict(task), "message": message.to_dict()})


@router.post("/{task_id}/decision-mode")
async def set_decision_mode(
    task_id: str,
    payload: DecisionModeRequest,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """切换任务级决策策略（文档 §5）：auto = AI 默认方案，manual = 由我决定。"""
    task_uuid = _task_id(task_id)
    await _require_open_task(container, task_uuid, task_id, cloud_user_id)
    task = await container.task_repo.set_decision_mode(task_uuid, payload.mode)
    return ok(_task_dict(task))


@router.post("/{task_id}/permission-mode")
async def set_permission_mode(
    task_id: str,
    payload: PermissionModeRequest,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """切换对话级权限模式（Plan / Normal / Full）。

    任何状态都允许切换；正在执行的 Run 按启动时的模式跑完，新模式从下一轮生效
    （响应里的 `effective_from` 明确告诉客户端这一点）。切换本身不执行任何命令、
    也不改动历史消息与已完成的审批记录。
    """
    task_uuid = _task_id(task_id)
    task = await _owned_task(container, task_uuid, cloud_user_id)
    previous = task.permission_mode or "normal"
    task = await container.task_repo.set_permission_mode(task_uuid, payload.mode)
    effective_from = "next_run" if task.status == TaskStatus.RUNNING.value else "next_message"
    result = {
        **_task_dict(task),
        "previous_mode": previous,
        "effective_from": effective_from,
    }
    # 多端一致性：其他设备打开同一对话时靠这个事件同步胶囊状态
    await container.task_stream.publish(
        task_uuid,
        "permission_mode_changed",
        {"permission_mode": str(payload.mode), "previous_mode": previous},
    )
    logger.info(
        "task.permission_mode_changed task=%s from=%s to=%s effective_from=%s",
        task_uuid,
        previous,
        payload.mode,
        effective_from,
    )
    return ok(result)


@router.post("/{task_id}/agent")
async def switch_task_agent(
    task_id: str,
    payload: TaskAgentRequest,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """切换执行该任务的 Agent（移动端对话页的 Agent 下拉）。

    同一任务/对话保留历史消息，后续消息改由新 Agent 承接。
    - 执行中切换：只排队（`pending_agent_id`），本轮 Run 收尾后自动生效，
      当前这一轮仍由原 Agent 跑完；响应 `queued=true`、`effective_from=next_run`。
    - 空闲/终态切换：立即生效，生成一张交接卡（`kind=handoff`）供新 Agent 接手。
    - 预检失败（Agent 不存在 / runtime 未接入）：保留原 Agent，按真实原因报错。
    """
    task_uuid = _task_id(task_id)
    task = await _owned_task(container, task_uuid, cloud_user_id)
    _parse_uuid(payload.agent_id, field="agent_id")
    result = await container.agent_switch.switch(task, payload.agent_id)
    body = {
        **_task_dict(result.task),
        "queued": result.queued,
        "target_agent_id": str(result.target.agent_id),
        "target_agent": result.target.name,
        "effective_from": "next_run" if result.queued else "now",
    }
    logger.info(
        "task.agent_switch task=%s target=%s queued=%s",
        task_uuid,
        result.target.agent_id,
        result.queued,
    )
    return ok(body)


@router.post("/{task_id}/model")
async def switch_task_model(
    task_id: str,
    payload: TaskModelRequest,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """切换任务的执行模型（移动端对话页 Agent 名称旁的下拉）。

    只接受该 Agent runtime 的模型目录里的 id；正在执行时不允许切换。
    切换 Agent 时会清掉本覆盖，回到新 Agent 的默认模型。
    终态对话同样允许切换，理由与切换 Agent 一致：只影响下一次续聊的模型。
    """
    task_uuid = _task_id(task_id)
    task = await _owned_task(container, task_uuid, cloud_user_id)
    if task.status == TaskStatus.RUNNING.value:
        raise ConflictError(
            f"任务 {task_id} 正在执行，先停止 Agent 再切换模型",
            details={"task_id": task_id, "status": task.status},
        )
    runtime = DEFAULT_RUNTIME
    if task.agent_id is not None:
        agent = container.agents.get(str(task.agent_id))
        runtime = getattr(getattr(agent, "spec", None), "runtime", DEFAULT_RUNTIME)
    model, key_id = await container.key_vault.resolve_selection(
        runtime=runtime,
        model=payload.model,
        key_id=payload.key_id,
        cloud_user_id=cloud_user_id,
    )
    task = await container.task_repo.set_model(task_uuid, model, key_id)
    return ok(_task_dict(task))


@router.post("/{task_id}/start")
async def start_task(
    task_id: str,
    payload: TaskStartRequest,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """开始执行（P0-05）：把确认过的需求交给该 Agent 的 runtime 起一次 Run，任务进入 running。

    确认卡可以随请求一起传（前端"改完直接执行"），也可以留空用任务上已保存的那份。
    编排收敛在 `TaskRunService`（P2-1 §6.2）：按 `agent.config["runtime"]` 分发
    Claude Code / Codex / OpenCode，对外响应与消息落库行为保持不变。runtime 不可用
    （CLI 未接入）时不返回"假装开始执行"的 200。
    """
    task_uuid = _task_id(task_id)
    task = await _require_open_task(container, task_uuid, task_id, cloud_user_id)
    if task.run_id:
        raise ConflictError(
            f"任务 {task_id} 已开始执行，不能重复触发",
            details={"task_id": task_id, "run_id": task.run_id},
        )
    if task.status == TaskStatus.WAITING_FOR_USER_DECISION.value:
        raise ConflictError(
            f"任务 {task_id} 正在等待你做出决策，请先完成选择",
            details={"task_id": task_id},
        )

    result = await container.task_run_service.start(task, payload.confirmation)
    return ok(
        {
            "task": _task_dict(result.task),
            "run": result.run,
            "message": result.message.to_dict() if result.message is not None else None,
        }
    )


@router.post("/{task_id}/decisions")
async def raise_decision(
    task_id: str,
    payload: DecisionCreateRequest,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """登记一个决策点（文档 §5）：Agent 执行中撞上可选方案时调用。

    按任务策略分流——auto 模式当场按推荐方案拍板并留痕，manual 模式把任务挂到
    `waiting_for_user_decision`，等用户选完再继续。
    """
    task_uuid = _task_id(task_id)
    task = await _require_open_task(container, task_uuid, task_id, cloud_user_id)
    if not task.run_id:
        raise ConflictError(
            f"任务 {task_id} 尚未开始执行，此时没有执行中的决策点",
            details={"task_id": task_id},
        )
    mode = DecisionMode(task.decision_mode)
    if mode is DecisionMode.MANUAL and task.pending_decision() is not None:
        pending = task.pending_decision() or {}
        raise ConflictError(
            "已有待决策的问题尚未回答，请先完成当前决策",
            details={"decision_id": pending.get("id")},
        )

    record = build_decision(
        question=payload.question,
        options=[option.model_dump() for option in payload.options],
        context=payload.context,
        recommendation=payload.recommendation,
        mode=mode,
    )
    task = await container.task_repo.raise_decision(task_uuid, record)

    if record["status"] == DecisionStatus.PENDING.value:
        task = await container.task_repo.set_status(task_uuid, TaskStatus.WAITING_FOR_USER_DECISION)
        content = f"执行遇到需要你决定的点：{record['question']}"
        kind = "decision"
    else:
        content = f"AI 已按默认方案自行决定：{record['chosen']}（问题：{record['question']}）"
        kind = "decision_auto"
    message = (
        await container.task_repo.add_messages(
            task_uuid,
            [
                MessageDraft(
                    role=TaskMessageRole.ASSISTANT.value,
                    kind=kind,
                    content=content,
                    payload={"decision": record},
                )
            ],
        )
    )[0]
    return ok({"task": _task_dict(task), "decision": record, "message": message.to_dict()})


async def _require_outbound_decision_owned(
    container: Container, pending: dict[str, Any], cloud_user_id: str | None
) -> None:
    """出站审批卡里带 draft_id 时，先确认草稿属于当前登录人。"""
    kind = str(pending.get("kind") or "")
    if not kind.endswith("_outbound"):
        return
    meta = pending.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    raw_draft_id = str(meta.get("draft_id") or "").strip()
    if not raw_draft_id:
        return
    try:
        draft_id = uuid.UUID(raw_draft_id)
    except ValueError as exc:
        raise NotFoundError(
            f"出站草稿 {raw_draft_id} 不存在", details={"draft_id": raw_draft_id}
        ) from exc
    if cloud_user_id is None:
        # 分不清是谁时 fail-closed：别人的审批一律不可见。
        raise NotFoundError(f"出站草稿 {raw_draft_id} 不存在", details={"draft_id": raw_draft_id})
    await container.outbound_drafts.get(draft_id, cloud_user_id=cloud_user_id)


@router.post("/{task_id}/decisions/choose")
async def choose_decision(
    task_id: str,
    payload: DecisionChooseRequest,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """用户对挂起的决策点做选择（文档 §5 模式 B）。

    - `choose`：必须给出候选方案里的 label，任务回到 running，Agent 从决策点继续；
    - `reject`：全部候选都不接受，任务同样回到 running 并把"请重新给方案"写进对话，
      Agent 可以再问一次（TC-202/203）。
    """
    task_uuid = _task_id(task_id)
    task = await _require_open_task(container, task_uuid, task_id, cloud_user_id)
    pending = task.pending_decision()
    if pending is None:
        raise ConflictError("当前没有等待你决策的问题", details={"task_id": task_id})
    if pending.get("id") != payload.decision_id:
        raise NotFoundError(
            f"决策点 {payload.decision_id} 不是当前待决策项",
            details={"decision_id": payload.decision_id, "pending": pending.get("id")},
        )
    # 出站草稿审批同样按人隔离：不能替别人放行 / 拒绝一条回复。
    await _require_outbound_decision_owned(container, pending, cloud_user_id)

    if payload.action == "reject":
        record = reject_decision(pending, note=payload.note)
        task, _ = await container.task_repo.resolve_decision(
            task_uuid,
            payload.decision_id,
            status=DecisionStatus.REJECTED,
            chosen=None,
            note=payload.note,
            resolved_at=str(record["resolved_at"]),
        )
        content = f"你拒绝了全部候选方案：{payload.note or '请 Agent 重新给出方案'}"
    else:
        if not payload.option:
            raise BadRequestError("action=choose 时必须给出 option", details={"field": "option"})
        record = resolve_decision(pending, option=payload.option, note=payload.note)
        task, _ = await container.task_repo.resolve_decision(
            task_uuid,
            payload.decision_id,
            status=DecisionStatus.RESOLVED,
            chosen=payload.option,
            note=payload.note,
            resolved_at=str(record["resolved_at"]),
        )
        content = f"你的选择：{payload.option}" + (f"（{payload.note}）" if payload.note else "")

    task = await container.task_repo.set_status(task_uuid, TaskStatus.RUNNING)
    message = (
        await container.task_repo.add_messages(
            task_uuid,
            [
                MessageDraft(
                    role=TaskMessageRole.USER.value,
                    kind="decision_result",
                    content=content,
                    payload={"decision": record},
                )
            ],
        )
    )[0]
    # 审批结果事件：出站类审批（如飞书草稿）只在 chosen == 允许 时动作。
    # 这里不直接依赖任何具体频道的实现，由订阅方按 kind 自行过滤。
    await container.bus.publish(
        Events.APPROVAL_RESOLVED,
        {
            "task_id": str(task_uuid),
            "decision_id": payload.decision_id,
            "result": "allowed" if record.get("chosen") == APPROVAL_ALLOW else "denied",
            "chosen": record.get("chosen"),
            "kind": pending.get("kind"),
            "meta": pending.get("meta") or {},
        },
    )
    return ok({"task": _task_dict(task), "decision": record, "message": message.to_dict()})


@router.post("/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    task_uuid = _task_id(task_id)
    task = await _owned_task(container, task_uuid, cloud_user_id)
    if task.status in _FINAL_STATUSES:
        raise ConflictError(
            f"任务 {task_id} 已处于终态 {task.status}，无法取消",
            details={"task_id": task_id, "status": task.status},
        )
    # 队列是进程内的易失结构，数据库才是任务状态的权威：
    # 任务在库中但不在队列里（进程重启过、或已被 next() 取出）时取消仍必须成功。
    with contextlib.suppress(NotFoundError):
        container.scheduler.cancel(str(task.id))
    if task.run_id:
        # 取消任务要连它正在跑的 Agent Run 一起停——留一个还在改文件的 Agent 与"已取消"是矛盾的。
        # P2-15：以 Run 的终态为准映射任务状态——进程树确认清理干净才是 cancelled，
        # 清理失败（FAILED）或超时（TIMEOUT）都按 failed 记录，不粉饰成"取消成功"。
        run_status = await _cancel_run(container, task.run_id)
        if run_status is DshRunStatus.COMPLETED:
            # 取消来晚了：Run 已成功结束（竞态窗口：Run 落终态、任务尚未被桥接收尾）。
            # 任务按 Run 的真实结局收尾，绝不把一次成功的执行改判成失败。
            task = await container.task_repo.set_status(task_uuid, TaskStatus.COMPLETED)
            return ok(_task_dict(task))
        if run_status is not None and run_status is not DshRunStatus.CANCELLED:
            task = await container.task_repo.set_status(task_uuid, TaskStatus.FAILED)
            return ok(_task_dict(task))
    task = await container.task_repo.set_status(task_uuid, TaskStatus.CANCELLED)
    return ok(_task_dict(task))


@router.delete("/{task_id}")
async def delete_task(
    task_id: str,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """彻底删除一段对话（任务）：消息与运行记录一并清除，不做软删除。

    正在执行（running / waiting_for_user_decision）的对话必须先停止：
    删掉一个 Agent 仍在改文件的任务是矛盾的。
    """
    task_uuid = _task_id(task_id)
    task = await _owned_task(container, task_uuid, cloud_user_id)
    if task.status in {TaskStatus.RUNNING.value, TaskStatus.WAITING_FOR_USER_DECISION.value}:
        raise ConflictError(
            f"对话 {task_id} 正在执行，先停止后再删除",
            details={"task_id": task_id, "status": task.status},
        )
    # 队列是进程内的易失结构：pending 任务可能还排在队列里，删之前先摘掉，
    # 否则调度器之后取到一条已经不存在的任务。
    with contextlib.suppress(NotFoundError):
        container.scheduler.cancel(str(task.id))
    messages, runs = await container.task_repo.delete(task_uuid)
    # 工作记忆卡与会话绑定同样随任务清除：任务没了，旧会话也不能被继承。
    cards = await container.task_memory.delete_for_task(task_uuid)
    sessions = await container.runtime_sessions.delete_for_task(task_uuid)
    logger.info(
        "删除对话 %s 完成：消息=%d 运行=%d 记忆卡=%d 会话绑定=%d",
        task_id,
        messages,
        runs,
        cards,
        sessions,
    )
    return ok({"id": task_id, "deleted": True, "messages": messages, "runs": runs})


async def _cancel_run(container: Container, run_id: str) -> DshRunStatus | None:
    """取消任务所属的 Run；Run 不存在时返回 None（历史任务没有 Run，不算失败）。"""
    try:
        return await container.run_supervisor.cancel(run_id)
    except NotFoundError:
        return None
