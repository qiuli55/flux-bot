"""内置 Agent 的 token 级旁路（LLM SSE tee 代理）。

DSH 的 stdio SDK 只投递 durable 事件（`assistant/message` 是整段），但 runtime 自己
调模型时发的是 `stream: true` 的 SSE（2026-10-25 实测：`POST /chat/completions`，
`accept: text/event-stream`）。与其等 SDK 开放 delta，不如把 DSH 的 base_url 指到
本进程的旁路端点：请求原样转发给上游，顺流解析增量，按 run_id 发到事件总线。

约束：

- 只对「Agent 轮次」请求开增量（带工具集或 DSH 系统提示词）。标题生成之类的
  辅助请求照常转发但不外发，避免把内部提示词吐进对话。
- 上游响应逐行透传，字节内容不改；解析失败只记 debug，绝不影响转发。
- 任何 runtime 都不允许用「整段生成再切块回放」冒充流式；本模块负责内置 Agent，
  opencode / codex 各有自己的原生增量通道。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from flux.core.event.bus import EventBus, Events
from flux.logging import get_logger

logger = get_logger(__name__)

#: 攒批上限：到 8 个字符就推一帧（比整段回放细得多，又不至于一 token 一帧刷屏）
FLUSH_CHARS = 8
#: 最长等这么久就把手上的字符推出去（模型停顿时不至于卡住一个孤零零的字）
FLUSH_INTERVAL_SECONDS = 0.05

#: 请求侧不能透传的头（host/长度由 httpx 重算；压缩关掉才能边转发边解析）
_DROP_REQUEST_HEADERS = frozenset(
    {"host", "content-length", "accept-encoding", "connection", "transfer-encoding"}
)
#: 响应侧不能透传的头（httpx 已解压；长度/分块由 Starlette 重算）
_DROP_RESPONSE_HEADERS = frozenset(
    {"content-length", "content-encoding", "transfer-encoding", "connection"}
)

#: DSH 只接 DeepSeek 协议（见 model_catalog），没有显式通道时的官方上游
DEFAULT_UPSTREAM = "https://api.deepseek.com"


def tap_base_url(mcp_url: str) -> str:
    """由 MCP 端点推导旁路根地址：同机同端口，只换路径。"""
    parsed = urlsplit(mcp_url or "")
    if not parsed.scheme or not parsed.netloc:
        return "http://127.0.0.1:8801/internal/llm-tap"
    return urlunsplit((parsed.scheme, parsed.netloc, "/internal/llm-tap", "", ""))


@dataclass(slots=True)
class TapTarget:
    """一次 Run 的旁路目标；`streamed_text` 供 DSH 客户端抑制整段重复外发。"""

    run_id: str
    upstream: str
    bus: EventBus
    streamed_text: bool = False
    emitted_chars: int = 0

    def join(self, path: str, query: str = "") -> str:
        url = f"{self.upstream.rstrip('/')}/{path.lstrip('/')}"
        return f"{url}?{query}" if query else url


class LlmStreamTap:
    """按 token（随机串）登记旁路目标，并负责 SSE 透传与增量发布。"""

    def __init__(self) -> None:
        self._targets: dict[str, TapTarget] = {}
        self._client: httpx.AsyncClient | None = None

    # --- 登记 ---
    def register(self, *, run_id: str, upstream: str, bus: EventBus) -> str:
        token = secrets.token_urlsafe(18)
        self._targets[token] = TapTarget(
            run_id=run_id, upstream=upstream or DEFAULT_UPSTREAM, bus=bus
        )
        return token

    def unregister(self, token: str | None) -> None:
        if token is None:
            return
        self._targets.pop(token, None)

    def target(self, token: str) -> TapTarget | None:
        return self._targets.get(token)

    def has_streamed(self, token: str | None) -> bool:
        target = self._targets.get(token) if token else None
        return bool(target and target.streamed_text)

    # --- 转发 ---
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
                follow_redirects=False,
            )
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def relay(
        self, target: TapTarget, response: httpx.Response, *, tap: bool
    ) -> AsyncIterator[bytes]:
        """把上游响应逐行透传给 DSH；`tap=True` 时顺流把增量发到事件总线。"""
        buffer = _DeltaBuffer(target)
        is_sse = "text/event-stream" in (response.headers.get("content-type") or "")
        pump: asyncio.Task[None] | None = None
        try:
            if tap and is_sse:
                # 读行放到独立任务里：wait_for 超时只取消 queue.get，不会掐断上游流
                lines: asyncio.Queue[str | None] = asyncio.Queue()

                async def _pump_lines() -> None:
                    try:
                        async for line in response.aiter_lines():
                            await lines.put(line)
                    finally:
                        await lines.put(None)

                pump = asyncio.create_task(_pump_lines())
                while True:
                    try:
                        line = await asyncio.wait_for(lines.get(), timeout=FLUSH_INTERVAL_SECONDS)
                    except asyncio.TimeoutError:
                        # 模型停顿：先把手上的字符推出去，观感上不出现「卡住半个字」
                        await buffer.flush()
                        continue
                    if line is None:
                        break
                    yield (line + "\n").encode("utf-8")
                    if line.startswith("data:"):
                        payload = _parse_data(line[5:].strip())
                        if payload is not None:
                            buffer.add(payload)
                            await buffer.maybe_flush()
            else:
                async for chunk in response.aiter_bytes():
                    yield chunk
        finally:
            if pump is not None:
                pump.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump
            await buffer.flush()
            await response.aclose()


class _DeltaBuffer:
    """按字符上限/时间上限攒批，把 delta 压成较少的 SSE 帧。"""

    def __init__(self, target: TapTarget) -> None:
        self._target = target
        self._pending: list[tuple[str, str]] = []
        self._chars = 0
        self._opened = 0.0

    def add(self, payload: dict[str, Any]) -> None:
        for kind, text in deltas_from_payload(payload):
            if not text:
                continue
            if self._pending and self._pending[-1][0] == kind:
                self._pending[-1] = (kind, self._pending[-1][1] + text)
            else:
                self._pending.append((kind, text))
            self._chars += len(text)
            if not self._opened:
                self._opened = time.monotonic()

    async def maybe_flush(self) -> None:
        if not self._pending:
            return
        if self._chars >= FLUSH_CHARS or (
            time.monotonic() - self._opened >= FLUSH_INTERVAL_SECONDS
        ):
            await self.flush()

    async def flush(self) -> None:
        pending, self._pending = self._pending, []
        self._chars = 0
        self._opened = 0.0
        for kind, text in pending:
            if not text:
                continue
            await self._target.bus.publish(
                Events.DSH_EVENT,
                {
                    "run_id": self._target.run_id,
                    "runtime_id": "dsh",
                    "event_type": "message" if kind == "text" else "thinking",
                    "text": text,
                    "streamed": True,
                },
            )
            self._target.emitted_chars += len(text)
            if kind == "text":
                self._target.streamed_text = True


def _parse_data(raw: str) -> dict[str, Any] | None:
    if not raw or raw == "[DONE]":
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def deltas_from_payload(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """从一条 SSE JSON 里取 (kind, text)：kind 为 text / thinking。"""
    deltas: list[tuple[str, str]] = []

    # OpenAI Chat Completions（DeepSeek 走这个）
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                deltas.append(("thinking", reasoning))
            content = delta.get("content")
            if isinstance(content, str) and content:
                deltas.append(("text", content))
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str) and text:
                            deltas.append(("text", text))

    # Anthropic Messages
    if payload.get("type") == "content_block_delta":
        delta = payload.get("delta")
        if isinstance(delta, dict):
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                text = delta.get("text")
                if isinstance(text, str) and text:
                    deltas.append(("text", text))
            elif delta_type == "thinking_delta":
                text = delta.get("thinking")
                if isinstance(text, str) and text:
                    deltas.append(("thinking", text))

    # OpenAI Responses API
    event_type = payload.get("type")
    if event_type == "response.output_text.delta":
        delta = payload.get("delta")
        if isinstance(delta, str) and delta:
            deltas.append(("text", delta))
    elif event_type in (
        "response.reasoning_summary_text.delta",
        "response.reasoning_text.delta",
    ):
        delta = payload.get("delta")
        if isinstance(delta, str) and delta:
            deltas.append(("thinking", delta))

    return deltas


def is_agent_request(payload: dict[str, Any]) -> bool:
    """判断这次模型请求是不是 Agent 轮次（而不是标题生成等辅助调用）。"""
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        return True
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        content = message.get("content")
        text = (
            content
            if isinstance(content, str)
            else json.dumps(content, ensure_ascii=False, default=str)
        )
        if "DeepSeek Harness" in text:
            return True
    return False


def filter_request_headers(headers: Any) -> dict[str, str]:
    return {
        key: value for key, value in headers.items() if key.lower() not in _DROP_REQUEST_HEADERS
    }


def filter_response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        key: value for key, value in headers.items() if key.lower() not in _DROP_RESPONSE_HEADERS
    }


#: 进程内单例：路由与 DSH 客户端共用
llm_stream_tap = LlmStreamTap()


__all__ = [
    "DEFAULT_UPSTREAM",
    "LlmStreamTap",
    "TapTarget",
    "deltas_from_payload",
    "filter_request_headers",
    "filter_response_headers",
    "is_agent_request",
    "llm_stream_tap",
    "tap_base_url",
]
