"""任务对话流式链路测试：进程内事件流、运行事件转接、回复投影与上游 SSE 解析。"""

from __future__ import annotations

import asyncio

import httpx

from flux.core.event.bus import EventBus, Events
from flux.core.model_gateway.base import ChatMessage, ChatResult, TokenUsage
from flux.core.model_gateway.providers.deepseek import DeepSeekProvider
from flux.core.task_engine.assistant import TaskAssistant, visible_reply_prefix
from flux.core.task_engine.stream import TaskStreamHub, TaskStreamRelay
from flux.enums import ModelProvider


def test_hub_replays_history_then_broadcasts_live_events() -> None:
    """订阅时先补 after_seq 之后的缓冲，再收到后续实时事件。"""

    async def scenario() -> tuple[int, str, str]:
        hub = TaskStreamHub()
        await hub.publish("t1", "assistant_delta", {"text": "你"})
        await hub.publish("t1", "assistant_delta", {"text": "好"})

        stream = hub.stream("t1", after_seq=1)
        replayed = await anext(stream)
        assert replayed is not None and replayed.seq == 2
        assert replayed.data["text"] == "好"

        await hub.publish("t1", "assistant_message", {"message": {"id": "m1"}})
        live = await anext(stream)
        await stream.aclose()
        assert live is not None
        return replayed.seq, live.type, live.data["message"]["id"]

    seq, event_type, message_id = asyncio.run(scenario())
    assert seq == 2
    assert event_type == "assistant_message"
    assert message_id == "m1"


def test_hub_poll_returns_events_after_seq() -> None:
    """轮询版接口：只返回 after_seq 之后的事件，且按任务隔离。"""

    async def scenario() -> tuple[list[int], list[int]]:
        hub = TaskStreamHub()
        await hub.publish("t1", "assistant_delta", {"text": "a"})
        await hub.publish("t1", "assistant_delta", {"text": "b"})
        await hub.publish("t2", "assistant_delta", {"text": "other"})
        return (
            [event.seq for event in hub.poll("t1", after_seq=1)],
            [event.seq for event in hub.poll("t1", after_seq=2)],
        )

    after_first, after_last = asyncio.run(scenario())
    assert after_first == [2]
    assert after_last == []


def test_hub_keeps_sequences_independent_per_task() -> None:
    async def scenario() -> tuple[int, int]:
        hub = TaskStreamHub()
        first = await hub.publish("t1", "x", {})
        second = await hub.publish("t2", "x", {})
        return first.seq, second.seq

    assert asyncio.run(scenario()) == (1, 1)


def test_relay_forwards_runtime_text_and_unbinds_on_terminal() -> None:
    """运行事件按 run → 对话映射转发；Run 终态后解除绑定。"""

    async def scenario() -> tuple[str, str, str | None]:
        hub = TaskStreamHub()
        hub.bind_run("r1", "t1")
        relay = TaskStreamRelay(hub)
        bus = EventBus()
        relay.attach(bus)

        await bus.publish(
            Events.DSH_EVENT,
            {"run_id": "r1", "event_type": "assistant/message", "text": "正在改文件"},
        )
        stream = hub.stream("t1")
        forwarded = await anext(stream)

        await bus.publish(Events.DSH_RUN_COMPLETED, {"run_id": "r1", "status": "completed"})
        finished = await anext(stream)
        await stream.aclose()
        assert forwarded is not None and finished is not None
        return forwarded.type, forwarded.data["text"], hub.task_for_run("r1")

    event_type, text, mapping = asyncio.run(scenario())
    assert event_type == "run_event"
    assert text == "正在改文件"
    assert mapping is None


def test_relay_skips_noise_without_text() -> None:
    async def scenario() -> list[str]:
        hub = TaskStreamHub()
        hub.bind_run("r1", "t1")
        bus = EventBus()
        TaskStreamRelay(hub).attach(bus)
        await bus.publish(Events.DSH_EVENT, {"run_id": "r1", "event_type": "session/heartbeat"})
        stream = hub.stream("t1")
        # 心跳类事件不转发：下一个事件只能靠主动发布制造
        await hub.publish("t1", "sentinel", {})
        event = await anext(stream)
        await stream.aclose()
        return [event.type] if event else []

    assert asyncio.run(scenario()) == ["sentinel"]


def test_relay_forwards_tool_and_thinking_fields() -> None:
    """命令卡要带上名字/命令/输出/状态；思考事件单独成类，不与答复混淆。"""

    async def scenario() -> tuple[dict, dict, dict]:
        hub = TaskStreamHub()
        hub.bind_run("r1", "t1")
        bus = EventBus()
        TaskStreamRelay(hub).attach(bus)
        await bus.publish(
            Events.DSH_EVENT,
            {
                "run_id": "r1",
                "event_type": "tool_call",
                "name": "bash",
                "detail": "ls -la",
                "output": "a.txt\n",
                "status": "completed",
                "call_id": "c1",
            },
        )
        await bus.publish(
            Events.DSH_EVENT,
            {"run_id": "r1", "event_type": "thinking", "text": "先看目录"},
        )
        await bus.publish(
            Events.DSH_EVENT,
            {"run_id": "r1", "event_type": "thinking", "phase": "step_start"},
        )
        stream = hub.stream("t1")
        tool = await anext(stream)
        thinking = await anext(stream)
        step = await anext(stream)
        await stream.aclose()
        assert tool is not None and thinking is not None and step is not None
        return tool.data, thinking.data, step.data

    tool, thinking, step = asyncio.run(scenario())
    assert tool["kind"] == "tool"
    assert tool["name"] == "bash"
    assert tool["detail"] == "ls -la"
    assert tool["output"] == "a.txt"
    assert tool["status"] == "completed"
    assert tool["call_id"] == "c1"
    assert thinking["kind"] == "thinking" and thinking["text"] == "先看目录"
    # 没有推理文本的 step_start 也保留一个刻度，手机端据此显示「思考中」
    assert step["kind"] == "thinking" and step["text"] == "" and step["phase"] == "step_start"


def test_relay_redacts_secrets_in_tool_output() -> None:
    """工具输出可能带凭据：进 SSE 前必须打码。"""

    async def scenario() -> dict:
        hub = TaskStreamHub()
        hub.bind_run("r1", "t1")
        bus = EventBus()
        TaskStreamRelay(hub).attach(bus)
        await bus.publish(
            Events.DSH_EVENT,
            {
                "run_id": "r1",
                "event_type": "tool_call",
                "name": "bash",
                "detail": "cat .env",
                "output": "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz012345",
                "status": "completed",
            },
        )
        stream = hub.stream("t1")
        event = await anext(stream)
        await stream.aclose()
        assert event is not None
        return event.data

    data = asyncio.run(scenario())
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz012345" not in data["output"]


def test_relay_paces_long_text_before_terminal_event() -> None:
    """长正文按块推：客户端逐段长出，run_status 必须排在所有分块之后。"""

    async def scenario() -> tuple[list[str], str]:
        hub = TaskStreamHub()
        hub.bind_run("r1", "t1")
        bus = EventBus()
        TaskStreamRelay(hub).attach(bus)
        text = "西湖" * 60  # 120 字，超过单帧直发阈值

        await bus.publish(
            Events.DSH_EVENT,
            {"run_id": "r1", "event_type": "message", "text": text},
        )
        await bus.publish(Events.DSH_RUN_COMPLETED, {"run_id": "r1", "status": "completed"})

        stream = hub.stream("t1")
        chunks: list[str] = []
        last_type = ""
        while True:
            event = await anext(stream)
            assert event is not None
            last_type = event.type
            if event.type == "run_status":
                break
            chunks.append(event.data["text"])
        await stream.aclose()
        return chunks, last_type

    chunks, last_type = asyncio.run(scenario())
    assert last_type == "run_status"
    assert len(chunks) > 1
    assert all(chunk for chunk in chunks)
    assert "".join(chunks) == "西湖" * 60


def test_visible_reply_prefix_hides_json_protocol() -> None:
    assert visible_reply_prefix('{"reply": "你好') == "你好"
    assert visible_reply_prefix('{"reply": "a\\"b"}') == 'a"b'
    assert visible_reply_prefix('{"reply": "第一行\\n第二行"}') == "第一行\n第二行"
    # reply 还没开始输出：不能把协议外壳当正文
    assert visible_reply_prefix('{"questions": [') == ""
    # 纯文本输出原样展示
    assert visible_reply_prefix("直接说人话") == "直接说人话"


def test_assistant_streams_only_the_visible_reply() -> None:
    """助手流式只把 reply 正文推给前端，协议 JSON 外壳不外泄。"""

    class _Provider:
        provider = ModelProvider.DEEPSEEK
        model_name = "fake-model"

        async def stream(self, messages: list[ChatMessage]):
            yield '{"reply": "你'
            yield '好"}'

    class _Router:
        def select(self) -> _Provider:
            return _Provider()

        async def chat(self, messages: list[ChatMessage]) -> ChatResult:  # pragma: no cover
            raise AssertionError("流式成功时不应回退")

    async def scenario() -> tuple[list[str], str, str]:
        deltas: list[str] = []

        async def on_delta(text: str) -> None:
            deltas.append(text)

        turn = await TaskAssistant(_Router()).respond(
            description="加过滤接口", message="帮我加过滤", on_delta=on_delta
        )
        return deltas, turn.reply, turn.model

    deltas, reply, model = asyncio.run(scenario())
    assert deltas == ["你", "好"]
    assert reply == "你好"
    assert model == "fake-model"


def test_assistant_falls_back_when_stream_fails_before_any_chunk() -> None:
    """上游流式不可用时回退非流式，用户仍能拿到完整回复。"""

    class _Provider:
        provider = ModelProvider.DEEPSEEK
        model_name = "fake-model"

        async def stream(self, messages: list[ChatMessage]):
            raise RuntimeError("stream unsupported")
            yield ""  # pragma: no cover - 让类型上是异步生成器

    class _Router:
        def select(self) -> _Provider:
            return _Provider()

        async def chat(self, messages: list[ChatMessage]) -> ChatResult:
            return ChatResult(
                content='{"reply": "回退成功"}',
                provider=ModelProvider.DEEPSEEK,
                model="fake-model",
                usage=TokenUsage(),
                latency_ms=12,
            )

    async def scenario() -> tuple[list[str], str]:
        deltas: list[str] = []

        async def on_delta(text: str) -> None:
            deltas.append(text)

        turn = await TaskAssistant(_Router()).respond(
            description="随便", message="hi", on_delta=on_delta
        )
        return deltas, turn.reply

    deltas, reply = asyncio.run(scenario())
    assert deltas == ["回退成功"]
    assert reply == "回退成功"


def test_openai_compatible_stream_parses_sse_chunks() -> None:
    body = (
        'data: {"choices":[{"delta":{"content":"你"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )

    async def collect() -> str:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            provider = DeepSeekProvider(
                "deepseek-chat", api_key="test-key", base_url="https://example.test", client=client
            )
            return "".join(
                [chunk async for chunk in provider.stream([ChatMessage(role="user", content="hi")])]
            )
        finally:
            await client.aclose()

    assert asyncio.run(collect()) == "你好"


def test_openai_compatible_stream_reports_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    async def collect() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            provider = DeepSeekProvider(
                "deepseek-chat", api_key="test-key", base_url="https://example.test", client=client
            )
            async for _ in provider.stream([ChatMessage(role="user", content="hi")]):
                raise AssertionError("不应产出任何增量")
        finally:
            await client.aclose()

    try:
        asyncio.run(collect())
    except Exception as exc:  # ProviderError
        assert "429" in str(exc)
    else:  # pragma: no cover - 没有抛出就是行为变了
        raise AssertionError("上游 429 必须抛出 ProviderError")
