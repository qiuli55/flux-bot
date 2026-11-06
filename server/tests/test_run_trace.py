"""执行轨迹测试：DSH 工具事件映射、工具分类、思考/命令合并、run 摘要接口。"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from deepseek_harness import Notification
from fastapi.testclient import TestClient

from flux.core.agent_runtime.dsh_events import to_flux_event
from flux.core.agent_runtime.run_repository import utcnow
from flux.core.agent_runtime.trace import (
    RunTraceRecorder,
    classify_tool,
    extract_paths,
    summarize_trace,
)
from flux.core.event.bus import EventBus, Events
from flux.core.task_engine.stream import TaskStreamHub, TaskStreamRelay
from flux.enums import DshRunStatus

PREFIX = "/api/v1"


class _MemoryTraceRepository:
    """记录调用参数的假仓储：单元测试不碰数据库。"""

    def __init__(self) -> None:
        self.replaced: list[tuple[str, str | None, list[dict[str, Any]]]] = []

    async def replace(
        self, run_id: str, task_id: str | uuid.UUID | None, items: list[dict[str, Any]]
    ) -> None:
        self.replaced.append((run_id, task_id, list(items)))


def test_to_flux_event_maps_tool_call_and_result() -> None:
    """DSH 的 tool/call 与 tool/result 必须映射成命令卡字段，否则内置 Agent 没有轨迹。"""
    _, call = to_flux_event(
        Notification(
            method="session.event",
            payload={
                "sessionId": "s1",
                "event": {
                    "type": "tool/call",
                    "data": {
                        "turn": 1,
                        "step": 1,
                        "callId": "call_1",
                        "name": "bash",
                        "arguments": '{"command": "ls -la", "description": "列目录"}',
                    },
                },
            },
        )
    )
    assert call["event_type"] == "tool/call"
    assert call["name"] == "bash"
    assert call["status"] == "running"
    assert call["call_id"] == "call_1"
    assert '"ls -la"' in call["detail"]

    _, result = to_flux_event(
        Notification(
            method="session.event",
            payload={
                "sessionId": "s1",
                "event": {
                    "type": "tool/result",
                    "data": {
                        "turn": 1,
                        "step": 1,
                        "callId": "call_1",
                        "message": {
                            "role": "user",
                            "source": {"kind": "tool", "callId": "call_1"},
                            "content": [
                                {
                                    "type": "tool-result",
                                    "toolCallId": "call_1",
                                    "isError": False,
                                    "content": [{"type": "text", "text": "a.txt\n"}],
                                }
                            ],
                        },
                    },
                },
            },
        )
    )
    assert result["event_type"] == "tool/result"
    assert result["call_id"] == "call_1"
    assert result["output"] == "a.txt\n"
    assert result["status"] == "completed"

    _, failed = to_flux_event(
        Notification(
            method="session.event",
            payload={
                "sessionId": "s1",
                "event": {
                    "type": "tool/result",
                    "data": {
                        "callId": "call_2",
                        "message": {
                            "content": [
                                {
                                    "type": "tool-result",
                                    "isError": True,
                                    "content": [{"type": "text", "text": "boom"}],
                                }
                            ]
                        },
                    },
                },
            },
        )
    )
    assert failed["status"] == "error"
    assert failed["output"] == "boom"


def test_classify_tool_and_extract_paths() -> None:
    """命令/读取/改动按工具名归类；路径从 JSON 入参与 apply_patch 文本里取。"""
    assert classify_tool("bash", '{"command": "ls"}') == "command"
    assert classify_tool("shell", "ls -la") == "command"
    assert classify_tool("read", '{"file_path": "/a.py"}') == "read"
    assert classify_tool("edit", '{"file_path": "/a.py"}') == "write"
    assert classify_tool("file_change", '["/a.py", "/b.py"]') == "write"
    assert classify_tool("grep", "TODO") == "other"
    # MCP 工具名带命名空间与哈希后缀：按语义词元归类
    assert classify_tool("mcp__flux__workspace_read_many_8bbf1909455c", '{"paths": []}') == "read"
    assert classify_tool("mcp__flux__workspace_write_1a2b", '{"path": "/a.py"}') == "write"
    assert classify_tool("mcp__flux__flux_context", '{"task_id": "t"}') == "other"

    assert extract_paths('{"file_path": "/a.py", "limit": 20}') == ["/a.py"]
    assert extract_paths('["/a.py", "/b.py"]') == ["/a.py", "/b.py"]
    assert extract_paths(
        "*** Begin Patch\n*** Update File: /x/a.py\n*** Add File: /x/b.py\n*** End Patch"
    ) == ["/x/a.py", "/x/b.py"]
    # 同一路径出现多次只算一个文件
    assert extract_paths('{"paths": ["/a.py", "/a.py"]}') == ["/a.py"]


def test_summarize_trace_counts_commands_and_distinct_files() -> None:
    rows = [
        {"run_id": "r", "category": "command", "paths": [], "call_id": "c1"},
        {"run_id": "r", "category": "command", "paths": [], "call_id": "c2"},
        {"run_id": "r", "category": "read", "paths": ["/a.py"], "call_id": "c3"},
        {"run_id": "r", "category": "read", "paths": ["/a.py"], "call_id": "c4"},
        {"run_id": "r", "category": "write", "paths": ["/b.py", "/c.py"], "call_id": "c5"},
        # 取不到路径的写操作按「一次调用一个文件」兜底
        {"run_id": "r", "category": "write", "paths": [], "call_id": "c6"},
    ]
    stats = summarize_trace(rows)
    assert stats == {"commands": 2, "files_read": 1, "files_changed": 3}


def test_recorder_merges_thinking_deltas_and_tool_events() -> None:
    """连续思考增量并成一段；同一 call_id 的 started/completed 并成一张命令卡。"""

    async def scenario() -> list[dict[str, Any]]:
        repo = _MemoryTraceRepository()
        recorder = RunTraceRecorder(repo)  # type: ignore[arg-type]
        recorder.record("r1", "t1", {"kind": "thinking", "text": "先看目录"})
        recorder.record("r1", "t1", {"kind": "thinking", "text": "，再读文件"})
        # 空思考刻度不占历史行
        recorder.record("r1", "t1", {"kind": "thinking", "text": "", "phase": "step_start"})
        recorder.record(
            "r1",
            "t1",
            {
                "kind": "tool",
                "name": "read",
                "detail": '{"file_path": "/a.py"}',
                "status": "running",
                "call_id": "c1",
            },
        )
        recorder.record(
            "r1",
            "t1",
            {"kind": "tool", "output": "print(1)", "status": "completed", "call_id": "c1"},
        )
        recorder.record("r1", "t1", {"kind": "thinking", "text": "读完了"})
        recorder.record("r1", "t1", {"kind": "error", "text": "命令失败"})
        await recorder.flush("r1")
        return repo.replaced[0][2]

    items = asyncio.run(scenario())
    assert [item["kind"] for item in items] == ["thinking", "tool", "thinking", "error"]
    assert items[0]["text"] == "先看目录，再读文件"
    assert items[1]["category"] == "read"
    assert items[1]["name"] == "read"
    assert items[1]["output"] == "print(1)"
    assert items[2]["text"] == "读完了"
    assert items[3]["text"] == "命令失败"


def test_relay_flushes_trace_on_terminal_event() -> None:
    async def scenario() -> tuple[int, int]:
        repo = _MemoryTraceRepository()
        recorder = RunTraceRecorder(repo)  # type: ignore[arg-type]
        hub = TaskStreamHub()
        hub.bind_run("r1", "t1")
        bus = EventBus()
        TaskStreamRelay(hub, recorder).attach(bus)
        await bus.publish(
            Events.DSH_EVENT,
            {"run_id": "r1", "event_type": "tool_call", "name": "bash", "detail": "ls"},
        )
        assert recorder.buffered("r1") == 1
        await bus.publish(Events.DSH_RUN_COMPLETED, {"run_id": "r1", "status": "completed"})
        return len(repo.replaced), recorder.buffered("r1")

    flushed, buffered = asyncio.run(scenario())
    assert flushed == 1
    assert buffered == 0


def test_runs_and_trace_endpoints_expose_duration_and_overview(client: TestClient) -> None:
    """任务详情接口：耗时来自 Run 时间戳，概览来自轨迹分类统计。"""
    task_id = client.post(f"{PREFIX}/tasks", json={"description": "带轨迹的对话"}).json()["data"][
        "id"
    ]
    task_uuid = uuid.UUID(task_id)
    container = client.app.state.container

    async def _seed() -> None:
        await container.run_repo.create(
            run_id="run-trace-1",
            session_id="session-trace-1",
            instruction="x",
            status=DshRunStatus.COMPLETED,
            task_id=task_uuid,
            started_at=utcnow(),
        )
        await container.run_trace_repo.replace(
            "run-trace-1",
            task_uuid,
            [
                {"kind": "thinking", "text": "先看看目录"},
                {
                    "kind": "tool",
                    "name": "bash",
                    "detail": '{"command": "ls -la"}',
                    "output": "a.txt\n",
                    "status": "completed",
                    "category": "command",
                    "call_id": "c1",
                },
                {
                    "kind": "tool",
                    "name": "read",
                    "detail": '{"file_path": "/a.py"}',
                    "status": "completed",
                    "category": "read",
                    "paths": ["/a.py"],
                    "call_id": "c2",
                },
                {
                    "kind": "tool",
                    "name": "edit",
                    "detail": '{"file_path": "/b.py"}',
                    "status": "completed",
                    "category": "write",
                    "paths": ["/b.py"],
                    "call_id": "c3",
                },
            ],
        )

    asyncio.run(_seed())

    runs = client.get(f"{PREFIX}/tasks/{task_id}/runs")
    assert runs.status_code == 200
    summary = runs.json()["data"][0]
    assert summary["run_id"] == "run-trace-1"
    assert summary["commands"] == 1
    assert summary["files_read"] == 1
    assert summary["files_changed"] == 1
    assert summary["duration_ms"] is not None

    trace = client.get(f"{PREFIX}/tasks/{task_id}/runs/run-trace-1/trace")
    assert trace.status_code == 200
    detail = trace.json()["data"]
    assert [item["kind"] for item in detail["items"]] == ["thinking", "tool", "tool", "tool"]
    assert detail["items"][1]["output"] == "a.txt\n"

    # 跨任务访问轨迹必须 404，轨迹不能借道别的对话读到
    other = client.post(f"{PREFIX}/tasks", json={"description": "别的对话"}).json()["data"]["id"]
    assert client.get(f"{PREFIX}/tasks/{other}/runs/run-trace-1/trace").status_code == 404

    # 删除对话：轨迹一并清除
    assert client.delete(f"{PREFIX}/tasks/{task_id}").status_code == 200
    assert client.get(f"{PREFIX}/tasks/{task_id}/runs").status_code == 404
