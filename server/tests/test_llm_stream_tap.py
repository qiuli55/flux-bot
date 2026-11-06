"""内置 Agent token 流式旁路：增量解析、Agent 请求识别、SSE 透传。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from flux.core.agent_runtime.llm_stream_tap import (
    LlmStreamTap,
    deltas_from_payload,
    is_agent_request,
    tap_base_url,
)


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(self, _event: str, payload: dict[str, Any]) -> None:
        self.published.append(payload)


def _sse(*chunks: dict[str, Any]) -> bytes:
    body = "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks)
    return (body + "data: [DONE]\n\n").encode("utf-8")


def _openai_delta(content: str | None = None, reasoning: str | None = None) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    return {"choices": [{"delta": delta}]}


def test_deltas_from_openai_chat_chunk() -> None:
    assert deltas_from_payload(_openai_delta(content="你")) == [("text", "你")]
    assert deltas_from_payload(_openai_delta(reasoning="先想")) == [("thinking", "先想")]


def test_deltas_from_anthropic_and_responses_forms() -> None:
    anthropic = {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": "嗨"},
    }
    thinking = {
        "type": "content_block_delta",
        "delta": {"type": "thinking_delta", "thinking": "推理"},
    }
    responses = {"type": "response.output_text.delta", "delta": "！"}
    assert deltas_from_payload(anthropic) == [("text", "嗨")]
    assert deltas_from_payload(thinking) == [("thinking", "推理")]
    assert deltas_from_payload(responses) == [("text", "！")]


def test_is_agent_request_filters_helper_calls() -> None:
    assert is_agent_request({"tools": [{"name": "shell"}], "messages": []})
    assert is_agent_request(
        {"messages": [{"role": "system", "content": "You are DeepSeek Harness"}]}
    )
    # 标题生成这类辅助请求：没有工具、也不是 Agent 系统提示词
    assert not is_agent_request({"messages": [{"role": "system", "content": "生成一个标题"}]})


def test_tap_base_url_derives_from_mcp_url() -> None:
    assert tap_base_url("http://127.0.0.1:8801/mcp") == "http://127.0.0.1:8801/internal/llm-tap"
    assert tap_base_url("") == "http://127.0.0.1:8801/internal/llm-tap"


@pytest.mark.asyncio
async def test_relay_tees_sse_deltas_and_keeps_bytes() -> None:
    payload = _sse(
        _openai_delta(reasoning="先看"),
        _openai_delta(content="你好"),
        _openai_delta(content="，世界"),
    )
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=payload
        )
    )
    bus = _RecordingBus()
    tap = LlmStreamTap()
    token = tap.register(run_id="run-1", upstream="http://upstream", bus=bus)  # type: ignore[arg-type]
    try:
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.send(
                client.build_request("POST", "http://upstream/chat/completions"), stream=True
            )
            target = tap.target(token)
            assert target is not None
            forwarded = b"".join([chunk async for chunk in tap.relay(target, response, tap=True)])
        assert forwarded == payload  # 原样透传，DSH 侧不受影响
        kinds = [(item["event_type"], item["text"]) for item in bus.published]
        # 相邻同类 delta 会合并；攒批在流结束时会兜底 flush
        # event_type 用总线词表的 thinking / message（TaskStreamRelay 再归一成 text）
        assert kinds == [("thinking", "先看"), ("message", "你好，世界")]
        assert all(item["run_id"] == "run-1" for item in bus.published)
        assert tap.has_streamed(token)
    finally:
        tap.unregister(token)


@pytest.mark.asyncio
async def test_relay_does_not_emit_for_helper_requests() -> None:
    payload = _sse(_openai_delta(content="标题"))
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=payload
        )
    )
    bus = _RecordingBus()
    tap = LlmStreamTap()
    token = tap.register(run_id="run-2", upstream="http://upstream", bus=bus)  # type: ignore[arg-type]
    try:
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.send(
                client.build_request("POST", "http://upstream/chat/completions"), stream=True
            )
            target = tap.target(token)
            assert target is not None
            forwarded = b"".join([chunk async for chunk in tap.relay(target, response, tap=False)])
        assert forwarded == payload
        assert bus.published == []
        assert not tap.has_streamed(token)
    finally:
        tap.unregister(token)
