"""codex app-server 会话的确定性测试。

不启动真实 codex：直接往会话的通知队列里塞 JSON-RPC 通知，验证
「连续 willRetry + 整轮零输出」会快速失败（而不是干等 10 分钟 idle 超时），
以及正常增量仍按 text / thinking 逐段外发。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from flux.core.agent_runtime.runtimes.codex_server import (
    CodexAppServerSession,
    _TextPump,
)


def _session(
    retry_fail_seconds: float = 0.15,
    permission_mode: str = "normal",
    approval_handler: Any = None,
    context_compact_tokens: int = 0,
) -> tuple[CodexAppServerSession, dict]:
    captured: dict[str, list[str]] = {"text": [], "thinking": []}

    async def on_text(text: str) -> None:
        captured["text"].append(text)

    async def on_thinking(text: str) -> None:
        captured["thinking"].append(text)

    async def on_tool(fields: dict, state: str) -> None:
        captured.setdefault("tool", []).append(state)

    session = CodexAppServerSession(
        SimpleNamespace(popen=SimpleNamespace(poll=lambda: None)),
    )
    session.bind(
        cwd="/tmp",
        on_text=on_text,
        on_thinking=on_thinking,
        on_tool=on_tool,
        retry_fail_seconds=retry_fail_seconds,
        permission_mode=permission_mode,
        approval_handler=approval_handler,
        context_compact_tokens=context_compact_tokens,
    )
    session._pump = _TextPump(on_text, on_thinking)
    return session, captured


def _push(session: CodexAppServerSession, *notifications: dict) -> None:
    for notification in notifications:
        session._notifications.put_nowait(notification)


def _retry(message: str = "workspace routing discovery failed") -> dict:
    return {
        "method": "error",
        "params": {"error": {"message": message}, "willRetry": True},
    }


async def test_retry_errors_without_output_fail_fast() -> None:
    session, captured = _session(retry_fail_seconds=0.1)
    _push(session, _retry(), _retry())
    result = await asyncio.wait_for(session._consume_turn(), timeout=5)

    assert result.status == "failed"
    assert "连续重试 2 次" in (result.error or "")
    assert "workspace routing discovery failed" in (result.error or "")
    assert captured["text"] == []


async def test_retry_errors_after_streaming_do_not_fail() -> None:
    """已经有正文增量时，偶发重试不算失败——codex 会自己续上。"""
    session, captured = _session(retry_fail_seconds=0.05)
    _push(
        session,
        {
            "method": "item/agentMessage/delta",
            "params": {"itemId": "a1", "delta": "杭州是"},
        },
        _retry(),
        {
            "method": "item/agentMessage/delta",
            "params": {"itemId": "a1", "delta": "浙江省会。"},
        },
        {
            "method": "turn/completed",
            "params": {"turn": {"id": "u1", "status": "completed"}},
        },
    )
    result = await asyncio.wait_for(session._consume_turn(), timeout=5)

    assert result.status == "completed"
    assert result.error is None
    assert "".join(captured["text"]) == "杭州是浙江省会。"


async def test_error_without_retry_returns_failure() -> None:
    session, _ = _session()
    _push(
        session,
        {"method": "error", "params": {"error": {"message": "model not found"}}},
    )
    result = await asyncio.wait_for(session._consume_turn(), timeout=5)

    assert result.status == "failed"
    assert result.error == "model not found"


def test_plan_mode_thread_is_read_only() -> None:
    """Plan 模式必须把 codex 的 sandbox 收到 read-only：原生 shell 不走 Flux 审批闸门。"""
    plan, _ = _session(permission_mode="plan")
    params = plan._thread_start_params()
    assert params["sandbox"] == "read-only"
    assert params["approvalPolicy"] == "never"

    normal, _ = _session(permission_mode="normal")
    assert normal._thread_start_params()["sandbox"] == "workspace-write"
    # 正常模式要 codex 把"它自己也不确定"的命令交上来，由 Flux 决定问不问
    assert normal._thread_start_params()["approvalPolicy"] == "untrusted"

    full, _ = _session(permission_mode="full")
    assert full._thread_start_params()["sandbox"] == "workspace-write"
    assert full._thread_start_params()["approvalPolicy"] == "untrusted"


async def test_native_command_approval_decline() -> None:
    """审批回调返回 False 时回 decline，命令不执行。"""
    seen: list[str] = []

    async def handler(command: str) -> bool:
        seen.append(command)
        return False

    session, _ = _session(approval_handler=handler)
    writes: list[dict] = []
    session._write = _capture_write(writes)
    await session._answer_server_request(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "rm -rf /tmp/x"},
        }
    )

    assert seen == ["rm -rf /tmp/x"]
    assert writes[0]["result"] == {"decision": "decline"}


async def test_native_command_approval_allow_legacy_array() -> None:
    """旧版 execCommandApproval 的命令是数组，回 approved。"""
    seen: list[str] = []

    async def handler(command: str) -> bool:
        seen.append(command)
        return True

    session, _ = _session(approval_handler=handler)
    writes: list[dict] = []
    session._write = _capture_write(writes)
    await session._answer_server_request(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "execCommandApproval",
            "params": {"command": ["git", "push", "origin", "main"]},
        }
    )

    assert seen == ["git push origin main"]
    assert writes[0]["result"] == {"decision": "approved"}


async def test_native_command_approval_plan_denies() -> None:
    """Plan 模式即使收到审批请求也一律拒绝（只读）。"""
    session, _ = _session(permission_mode="plan", approval_handler=_never_called)
    writes: list[dict] = []
    session._write = _capture_write(writes)
    await session._answer_server_request(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "touch probe.txt"},
        }
    )

    assert writes[0]["result"] == {"decision": "decline"}


async def _never_called(command: str) -> bool:  # pragma: no cover - 被调用即失败
    raise AssertionError(f"Plan 模式不应调用审批回调：{command}")


def _capture_write(sink: list[dict]):
    async def write(payload: dict) -> None:
        sink.append(payload)

    return write


# --- 上下文预算（P1-6）：thread 超阈值先压缩再执行 ---


def _usage(total: int) -> dict:
    """`thread/tokenUsage/updated`：total 反映本次请求的上下文规模。"""
    return {
        "method": "thread/tokenUsage/updated",
        "params": {
            "tokenUsage": {
                "total": {"totalTokens": total, "inputTokens": total - 5, "outputTokens": 5},
                "last": {"totalTokens": total, "inputTokens": total - 5, "outputTokens": 5},
                "modelContextWindow": 258400,
            }
        },
    }


def _completed(turn_id: str = "u1") -> dict:
    return {
        "method": "turn/completed",
        "params": {"turn": {"id": turn_id, "status": "completed"}},
    }


async def test_turn_reports_context_tokens() -> None:
    """每轮都要回报上下文规模，下一轮才有依据判断压缩。"""
    session, _ = _session()
    _push(session, _usage(4321), _completed())
    result = await asyncio.wait_for(session._consume_turn(), timeout=5)

    assert result.status == "completed"
    assert result.context_tokens == 4321


async def test_compaction_turn_is_swallowed_and_reports_new_size() -> None:
    """压缩是独立的一轮 turn：它的正文/状态不能漏进时间线，只取压缩后的规模。"""
    session, captured = _session()
    _push(
        session,
        {"method": "turn/started", "params": {"turn": {"id": "c1", "status": "inProgress"}}},
        {"method": "item/started", "params": {"item": {"type": "contextCompaction", "id": "x"}}},
        {"method": "item/agentMessage/delta", "params": {"itemId": "cmp", "delta": "压缩摘要"}},
        _usage(5285),
        {"method": "item/completed", "params": {"item": {"type": "contextCompaction", "id": "x"}}},
        _completed("c1"),
    )
    after = await asyncio.wait_for(session._consume_compaction(), timeout=5)

    assert after == 5285
    assert captured["text"] == []


async def test_compaction_failure_raises_so_caller_can_continue() -> None:
    session, _ = _session()
    _push(
        session,
        {"method": "turn/completed", "params": {"turn": {"id": "c1", "status": "failed"}}},
    )
    try:
        await asyncio.wait_for(session._consume_compaction(), timeout=5)
    except RuntimeError as exc:
        assert "failed" in str(exc)
    else:  # pragma: no cover - 必须抛错，调用方才好降级
        raise AssertionError("压缩失败应当抛错")


def _fake_run(
    *,
    previous: str,
    previous_tokens: int,
    budget: int,
    compact_calls: list[dict] | None = None,
    compact_fails: bool = False,
):
    """跑一轮 `run()`：只替换 JSON-RPC 收发，通知由 fake 直接塞进队列。"""

    async def _run():
        session, captured = _session(context_compact_tokens=budget)
        calls: list[str] = []
        # 不启真正的 stdout 泵：本用例只验证协议时序
        session._read_stdout = lambda loop: None  # type: ignore[method-assign]

        async def fake_request(method: str, params: dict, *, timeout: float):
            calls.append(method)
            if method == "thread/resume":
                return {"thread": {"id": "thread-old"}}
            if method == "thread/start":
                return {"thread": {"id": "thread-new"}}
            if method == "thread/compact/start":
                if compact_calls is not None:
                    compact_calls.append(params)
                if compact_fails:
                    raise RuntimeError("method not found")
                _push(
                    session,
                    {
                        "method": "item/agentMessage/delta",
                        "params": {"itemId": "cmp", "delta": "压缩摘要"},
                    },
                    _usage(5000),
                    _completed("c1"),
                )
                return {}
            if method == "turn/start":
                _push(
                    session,
                    {
                        "method": "item/agentMessage/delta",
                        "params": {"itemId": "m1", "delta": "好的"},
                    },
                    _usage(6000),
                    _completed("u1"),
                )
                return {}
            return {}

        session._request = fake_request  # type: ignore[method-assign]
        result = await asyncio.wait_for(
            session.run("继续", thread_id=previous, context_tokens=previous_tokens),
            timeout=5,
        )
        return captured, calls, result

    return _run()


async def test_over_budget_resumed_thread_is_compacted_before_the_turn() -> None:
    compacted: list[dict] = []
    captured, calls, result = await _fake_run(
        previous="thread-old", previous_tokens=90000, budget=80000, compact_calls=compacted
    )

    assert calls == ["initialize", "thread/resume", "thread/compact/start", "turn/start"]
    assert compacted == [{"threadId": "thread-old"}]
    # 压缩轮的增量不进时间线，正式回合照常输出
    assert captured["text"] == ["好的"]
    assert result.text == "好的"
    assert result.context_tokens == 6000


async def test_within_budget_resumed_thread_is_not_compacted() -> None:
    _, calls, result = await _fake_run(previous="thread-old", previous_tokens=1000, budget=80000)

    assert calls == ["initialize", "thread/resume", "turn/start"]
    assert result.context_tokens == 6000


async def test_new_thread_ignores_stale_context_size() -> None:
    """没有可复用的 thread 时上下文是空的：即使记住的旧规模很大也不压缩。"""
    _, calls, _ = await _fake_run(previous="", previous_tokens=999999, budget=80000)

    assert calls == ["initialize", "thread/start", "turn/start"]


async def test_budget_zero_disables_compaction() -> None:
    _, calls, _ = await _fake_run(previous="thread-old", previous_tokens=999999, budget=0)

    assert calls == ["initialize", "thread/resume", "turn/start"]


async def test_compaction_failure_does_not_block_the_turn() -> None:
    """压缩失败（旧版 codex 没这个方法 / 压缩轮失败）只降级，不拖垮本轮。"""
    captured, calls, result = await _fake_run(
        previous="thread-old", previous_tokens=90000, budget=80000, compact_fails=True
    )

    assert calls == ["initialize", "thread/resume", "thread/compact/start", "turn/start"]
    assert captured["text"] == ["好的"]
    assert result.status == "completed"
