"""任务对话的实时事件流（SSE 的进程内广播层）。

只承载"正在发生"的增量：助手回复的分段文本、Agent 运行中的工具/文本事件、
Run 起止。最终消息仍以 task_messages 表为唯一真相源——进程重启丢缓冲不会丢数据，
客户端重连后先按 `after_seq` 补能补到的增量，再拉一次消息列表即可对齐。

事件序号按任务单调递增，供 `Last-Event-ID` / `after_seq` 续传。
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from flux.core.agent_runtime.trace import RunTraceRecorder, classify_tool, extract_paths
from flux.core.context.redact import redact
from flux.core.event.bus import EventBus, Events
from flux.logging import get_logger

logger = get_logger(__name__)

#: 每个任务保留的增量事件条数：只用于短暂断线续传，不追求完整历史
BUFFER_LIMIT = 500
#: SSE 心跳间隔（秒）：反向代理与移动网络会把长时间静默的连接掐掉
HEARTBEAT_SECONDS = 15.0
#: Run 终态事件：收到后解除 run → task 绑定，避免映射表无限增长
TERMINAL_RUN_EVENTS = (
    Events.DSH_RUN_COMPLETED,
    Events.DSH_RUN_FAILED,
    Events.DSH_RUN_CANCELLED,
    Events.DSH_RUN_TIMEOUT,
    Events.DSH_RUN_INTERRUPTED,
)
#: 工具输出进 SSE 的展示上限：完整输出可能几十 MB，手机端只需要看个大概
OUTPUT_LIMIT = 4000
#: 工具命令行/参数的展示上限
DETAIL_LIMIT = 1200
#: 正文分块兜底：所有 runtime 都已原生逐 token 流式（base.RUNTIME_NATIVE_STREAMING），
#: 正常情况这里不会触发；只有 runtime 意外吐出整段正文时才按节奏摊开，避免手机端
#: 「一下子蹦出全部文字」。触发时会打 warning，提醒该 runtime 的增量通道退化了。
TEXT_CHUNK_MIN_CHARS = 60
TEXT_CHUNK_PARTS = 24
TEXT_CHUNK_INTERVAL = 0.045


@dataclass(frozen=True)
class TaskStreamEvent:
    """一条任务增量事件；seq 在同一任务内单调递增。"""

    seq: int
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "type": self.type, "data": self.data}


class TaskStreamHub:
    """按任务广播增量事件，并保留最近若干条供断线续传。"""

    def __init__(self, limit: int = BUFFER_LIMIT) -> None:
        self._limit = limit
        self._buffers: dict[str, deque[TaskStreamEvent]] = {}
        self._subscribers: dict[str, set[asyncio.Queue[TaskStreamEvent]]] = {}
        self._seq: dict[str, int] = {}
        #: run_id → task_id：运行事件只带 run_id，转发到任务流前要换算出对话
        self._run_tasks: dict[str, str] = {}
        #: 分块推送的排队任务：同任务后续事件必须等它推完，seq 顺序才不乱
        self._paced: dict[str, asyncio.Task[None]] = {}

    def bind_run(self, run_id: str, task_id: str) -> None:
        self._run_tasks[run_id] = str(task_id)

    def unbind_run(self, run_id: str) -> None:
        self._run_tasks.pop(run_id, None)

    def task_for_run(self, run_id: str) -> str | None:
        return self._run_tasks.get(run_id)

    async def publish(
        self, task_id: Any, type: str, data: dict[str, Any] | None = None
    ) -> TaskStreamEvent:
        key = str(task_id)
        # 分块还没推完就来了终态事件（例如 run_finished）：先等它，避免正文被跳过后整段出现
        await self._settle_paced(key)
        return await self._emit(key, type, dict(data or {}))

    async def publish_paced(
        self,
        task_id: Any,
        type: str,
        data: dict[str, Any],
        chunks: list[str],
        *,
        interval: float,
    ) -> None:
        """把一次性的长正文按块摊开发布，客户端才能看到"逐段生成"。

        SSE 是单向广播，没法慢放已经发出的帧，所以这里用后台任务按节奏补发，
        其它事件（run_finished 等）在 `publish` 里会先等这批分块推完再入队。
        """
        if not chunks:
            return
        key = str(task_id)
        previous = self._paced.get(key)

        async def run() -> None:
            if previous is not None and not previous.done():
                try:
                    await previous
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - 上一批失败不该连坐本批
                    logger.exception("上一批正文分块推送失败 task=%s", key)
            try:
                for chunk in chunks:
                    await self._emit(key, type, {**data, "text": chunk})
                    await asyncio.sleep(interval)
            finally:
                if self._paced.get(key) is asyncio.current_task():
                    self._paced.pop(key, None)

        self._paced[key] = asyncio.create_task(run())

    async def _settle_paced(self, key: str) -> None:
        task = self._paced.get(key)
        if task is None or task.done():
            return
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 分块失败不阻塞后续事件
            logger.exception("正文分块推送失败 task=%s", key)

    async def _emit(self, key: str, type: str, data: dict[str, Any]) -> TaskStreamEvent:
        seq = self._seq.get(key, 0) + 1
        self._seq[key] = seq
        event = TaskStreamEvent(seq=seq, type=type, data=data)
        self._buffers.setdefault(key, deque(maxlen=self._limit)).append(event)
        for queue in list(self._subscribers.get(key, ())):
            queue.put_nowait(event)
        return event

    def poll(self, task_id: Any, *, after_seq: int = 0) -> list[TaskStreamEvent]:
        """一次取回 `after_seq` 之后的缓冲事件（账号云中转不支持 SSE 时轮询用）。"""
        key = str(task_id)
        return [event for event in list(self._buffers.get(key, ())) if event.seq > after_seq]

    async def stream(
        self, task_id: Any, *, after_seq: int = 0
    ) -> AsyncIterator[TaskStreamEvent | None]:
        """先补历史再等增量；`None` 表示该发心跳了。"""
        key = str(task_id)
        queue: asyncio.Queue[TaskStreamEvent] = asyncio.Queue()
        # 先订阅再回放：回放期间的发布直接进队列，不会漏事件
        self._subscribers.setdefault(key, set()).add(queue)
        try:
            for event in list(self._buffers.get(key, ())):
                if event.seq > after_seq:
                    yield event
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield None
        finally:
            subscribers = self._subscribers.get(key)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(key, None)


class TaskStreamRelay:
    """把运行事件总线上的 Agent 事件转成任务流事件。"""

    def __init__(self, hub: TaskStreamHub, recorder: RunTraceRecorder | None = None) -> None:
        self._hub = hub
        #: 执行轨迹记录器：把思考/命令增量合并后落库，供任务详情回看（可选装配，单测可省）
        self._recorder = recorder

    def attach(self, bus: EventBus) -> None:
        bus.subscribe(Events.DSH_EVENT, self._on_runtime_event)
        for event in TERMINAL_RUN_EVENTS:
            bus.subscribe(event, self._on_run_terminal)

    async def _on_runtime_event(self, event: str, body: dict[str, Any]) -> None:
        run_id = body.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return
        task_id = self._hub.task_for_run(run_id)
        if task_id is None:
            return
        event_type = str(body.get("event_type") or body.get("method") or "")
        payload = _event_payload(body, event_type)
        # 心跳/用量类事件不推给手机端：时间线只收思考、命令、正文、错误
        if not payload:
            return
        data = {"run_id": run_id, "event_type": event_type, **payload}
        if self._recorder is not None:
            # 轨迹先记账再推流：SSE 阻塞或客户端不在线都不影响历史可回看
            self._recorder.record(run_id, task_id, payload)
        text = payload.get("text") or ""
        if payload.get("kind") == "text" and len(text) >= TEXT_CHUNK_MIN_CHARS:
            # 兜底：整段正文按节奏摊开（所有 runtime 都该走原生增量，走到这里说明退化了）
            logger.warning(
                "runtime 未提供原生增量，正文按分段回放 run=%s runtime=%s chars=%d",
                run_id,
                body.get("runtime_id"),
                len(text),
            )
            await self._hub.publish_paced(
                task_id,
                "run_event",
                {key: value for key, value in data.items() if key != "text"},
                _split_text(text),
                interval=TEXT_CHUNK_INTERVAL,
            )
            return
        await self._hub.publish(task_id, "run_event", data)

    async def _on_run_terminal(self, event: str, body: dict[str, Any]) -> None:
        run_id = body.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return
        task_id = self._hub.task_for_run(run_id)
        if task_id is None:
            return
        await self._hub.publish(
            task_id,
            "run_status",
            {
                "run_id": run_id,
                "status": body.get("status"),
                "finish_reason": body.get("finish_reason"),
            },
        )
        if self._recorder is not None:
            await self._recorder.flush(run_id)
        self._hub.unbind_run(run_id)


def _event_text(body: dict[str, Any]) -> str:
    for key in ("text", "content", "message"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _redacted(text: str) -> str:
    """工具出入参可能带凭据：进手机端之前统一打码（与上下文摄入同一套规则）。"""
    try:
        return redact(text).text
    except Exception:  # noqa: BLE001 - 脱敏失败绝不能把原文推出去
        logger.exception("运行事件脱敏失败，按空文本处理")
        return ""


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _split_text(text: str, parts: int = TEXT_CHUNK_PARTS) -> list[str]:
    """按固定份数切正文：份数有上限，再长的答复也不会拖太久。"""
    size = max(1, -(-len(text) // parts))
    return [text[index : index + size] for index in range(0, len(text), size)]


def _event_kind(event_type: str) -> str:
    """把运行时事件归成手机端时间线的四类：thinking / tool / text / error。"""
    lowered = event_type.strip().lower()
    if lowered in ("thinking", "reasoning") or "reasoning" in lowered:
        return "thinking"
    if "tool" in lowered or lowered in ("command", "command_execution"):
        return "tool"
    if lowered in ("message", "assistant", "text") or lowered.endswith("/message"):
        return "text"
    if lowered == "error":
        return "error"
    return "status"


def _tool_payload(
    body: dict[str, Any], part: dict[str, Any], item: dict[str, Any]
) -> dict[str, Any]:
    """工具事件的可展示字段：名称、命令行/参数、状态、输出、调用 id。

    CLI 归一化层已经把这些摊到事件顶层；这里保留一层 part/item 兜底，
    其它 runtime（含以后接入的）按各自原生结构吐事件时也能显示成命令卡。
    """
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    payload = state.get("input") if isinstance(state.get("input"), dict) else {}
    name = str(body.get("name") or part.get("tool") or item.get("tool") or item.get("name") or "")
    detail = body.get("detail") or body.get("command") or state.get("title") or ""
    if not detail and payload:
        detail = _json_compact(payload)
    elif not isinstance(detail, str):
        # codex 的 fileChange 直接给路径列表：统一压成 JSON，客户端与轨迹统计都能解析
        detail = _json_compact(detail)
    output = body.get("output")
    if not isinstance(output, str) or not output.strip():
        output = state.get("output")
    fields: dict[str, Any] = {
        # 名称未知时留空：客户端显示兜底名，轨迹合并也不会用「tool」把真实名字覆盖掉
        "name": _clip(str(name), 80) if name else "",
        "detail": _clip(_redacted(str(detail)), DETAIL_LIMIT) if detail else "",
        "status": str(body.get("status") or body.get("state") or state.get("status") or ""),
    }
    # 分类与文件路径随事件一起下发：客户端据此实时累加「概览」的命令/文件数
    category = classify_tool(fields["name"], fields["detail"])
    fields["category"] = category
    paths = extract_paths(fields["detail"])
    if paths:
        fields["paths"] = paths
    if isinstance(output, str) and output.strip():
        fields["output"] = _clip(_redacted(output), OUTPUT_LIMIT)
    title = body.get("title") or state.get("title")
    if isinstance(title, str) and title.strip():
        fields["title"] = _clip(_redacted(title), DETAIL_LIMIT)
    call_id = body.get("call_id") or part.get("callID") or item.get("id")
    if isinstance(call_id, str) and call_id.strip():
        fields["call_id"] = call_id
    return fields


def _json_compact(value: Any) -> str:
    """紧凑 JSON：非字符串入参（对象/数组）压成一行展示。"""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return str(value)


def _event_payload(body: dict[str, Any], event_type: str) -> dict[str, Any]:
    """一条运行事件 → 手机端时间线载荷；返回空 dict 表示这类事件不值得推。"""
    kind = _event_kind(event_type)
    part = body.get("part") if isinstance(body.get("part"), dict) else {}
    item = body.get("item") if isinstance(body.get("item"), dict) else {}

    if kind == "tool":
        fields = _tool_payload(body, part, item)
        # 连工具名都没有的空中间态不单独成卡；但带 call_id 的收尾事件必须放行，
        # 否则轨迹里的命令永远停在「运行中」（DSH 的 tool/result 不带名称与参数）
        if (
            not fields["name"]
            and not fields["detail"]
            and not fields.get("output")
            and not fields.get("call_id")
        ):
            return {}
        return {"kind": kind, **fields}

    text = _event_text(body) if kind != "thinking" else str(body.get("text") or "")
    if kind == "thinking" and not text.strip():
        # 没有推理文本时也留一个「思考中」刻度（opencode 的 step_start）
        phase = str(body.get("phase") or "step_start")
        return {"kind": kind, "text": "", "phase": phase}
    if not text.strip():
        return {}
    if kind == "status":
        # 未知事件类型但带正文：按正文处理，保持旧行为（不丢增量文字）
        kind = "text"
    payload: dict[str, Any] = {"kind": kind, "text": _clip(_redacted(text), OUTPUT_LIMIT)}
    if kind == "thinking" and body.get("phase"):
        payload["phase"] = str(body["phase"])
    return payload


__all__ = [
    "BUFFER_LIMIT",
    "HEARTBEAT_SECONDS",
    "TaskStreamEvent",
    "TaskStreamHub",
    "TaskStreamRelay",
]
