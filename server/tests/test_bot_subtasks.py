"""Bot 后台子任务契约测试。

覆盖：创建即 202、列表/详情、内核 Run 完成结果回父对话、边跑边聊、失败、取消、
超时、4xx、默认任务列表隐藏子任务、工具入口、停机不挂。

子任务跑的是真正的内核路径（Claude Code CLI）：一律注入 fake CLI 替身进程，
不启动真实 runtime、不调用真实模型；Bot 聊天轮次仍用记录型助手替身。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flux.config import Settings
from flux.container import Container
from flux.core.agent_runtime.manager import AgentSpec
from flux.core.bot.subtasks import run_subtask_tool
from flux.core.event.bus import Events
from flux.core.task_engine.assistant import AssistantTurn
from flux.enums import AgentRole
from flux.errors import BadRequestError, ValidationError
from flux.main import create_app
from tests.conftest import create_agent
from tests.fakes import mark_runtime_ready, read_cli_argv, write_cli_script, write_fake_claude

PREFIX = "/api/v1"
TERMINAL = {"completed", "failed", "cancelled"}
SUBTASK_TOOL_NAMES = {"subtask_start", "subtask_get", "subtask_list"}
#: 子任务在测试里必须能拿到 Agent 档案（内核 Run 需要令牌身份）
AGENT_NAME = "subtask-agent"
#: 取消/超时时给进程树清理留的宽限（默认 5s 会让用例白等）
CANCEL_GRACE_SECONDS = 0.3


@pytest.fixture(autouse=True)
def _schema(db_schema: None) -> None:
    """本模块自建 app / container 实例（不走 conftest 的 client 夹具），显式声明建表依赖。"""


class _FakeAssistant:
    """记录型助手替身：Bot 聊天轮次仍在平台模型通道上跑，这里只记录调用。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def respond(self, **kwargs: Any) -> AssistantTurn:
        self.calls.append(kwargs)
        on_delta = kwargs.get("on_delta")
        if on_delta is not None:
            await on_delta("思考中")
        return AssistantTurn(
            reply=f"echo: {kwargs['message']}",
            provider="fake",
            model="fake-model",
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )


#: 内核 Run 直接失败的假 CLI：进程以非 0 退出码结束
_FAILING_CLI = """#!/usr/bin/env python3
import json, sys

print(json.dumps({"type": "system", "subtype": "init", "session_id": "sess-fail"}), flush=True)
sys.exit(1)
"""


def _kernel_settings(
    settings: Settings, tmp_path: Path, *, timeout_seconds: float | None = None
) -> Settings:
    updates: dict[str, Any] = {
        "runtime_home": str(tmp_path / "runtime-home"),
        "runtime_workspace": str(tmp_path / "runtime-ws"),
        "workspace_root": str(tmp_path / "project"),
        "run_cancel_grace_seconds": CANCEL_GRACE_SECONDS,
    }
    if timeout_seconds is not None:
        updates["bot_subtask_timeout_seconds"] = timeout_seconds
    (tmp_path / "project").mkdir(exist_ok=True)
    return settings.model_copy(update=updates)


@contextmanager
def _kernel_app(
    settings: Settings,
    tmp_path: Path,
    *,
    hold: bool = False,
    failing: bool = False,
    timeout_seconds: float | None = None,
) -> Iterator[tuple[TestClient, Path, Path]]:
    """带 fake Claude CLI 的 app：子任务「开始」时起的是替身进程。

    `hold=True` 让替身一直不结束（模拟执行中的 Agent），由用例 touch sentinel 放行；
    `failing=True` 换成一个以非 0 退出码结束的替身（内核 Run 失败路径）。
    """
    log_path = tmp_path / "cli-argv.log"
    sentinel = tmp_path / "release"
    if failing:
        binary = write_cli_script(tmp_path / "claude", _FAILING_CLI)
    else:
        binary = write_fake_claude(
            tmp_path / "claude", log_path=log_path, sentinel=sentinel, hold=hold
        )
    app = create_app(_kernel_settings(settings, tmp_path, timeout_seconds=timeout_seconds))
    previous = os.environ.get("FLUX_CLAUDE_CLI_BINARY")
    os.environ["FLUX_CLAUDE_CLI_BINARY"] = binary
    try:
        with TestClient(app) as client:
            mark_runtime_ready(app.state.container, "claude-code")
            try:
                yield client, log_path, sentinel
            finally:
                sentinel.touch()
    finally:
        if previous is None:
            os.environ.pop("FLUX_CLAUDE_CLI_BINARY", None)
        else:
            os.environ["FLUX_CLAUDE_CLI_BINARY"] = previous


@contextmanager
def _kernel_container(
    settings: Settings,
    tmp_path: Path,
    *,
    hold: bool = False,
    timeout_seconds: float | None = None,
) -> Iterator[tuple[Container, Path, Path]]:
    """直接装配容器（不经 HTTP）：异步用例共用同一套 fake Claude CLI。"""
    log_path = tmp_path / "cli-argv.log"
    sentinel = tmp_path / "release"
    binary = write_fake_claude(tmp_path / "claude", log_path=log_path, sentinel=sentinel, hold=hold)
    container = Container(_kernel_settings(settings, tmp_path, timeout_seconds=timeout_seconds))
    previous = os.environ.get("FLUX_CLAUDE_CLI_BINARY")
    os.environ["FLUX_CLAUDE_CLI_BINARY"] = binary
    try:
        mark_runtime_ready(container, "claude-code")
        try:
            yield container, log_path, sentinel
        finally:
            sentinel.touch()
    finally:
        if previous is None:
            os.environ.pop("FLUX_CLAUDE_CLI_BINARY", None)
        else:
            os.environ["FLUX_CLAUDE_CLI_BINARY"] = previous


def _create_bot(client: TestClient, **overrides: object) -> dict:
    payload: dict[str, object] = {
        "name": "小助",
        "description": "负责日常问答",
        "agent_id": create_agent(client, name=AGENT_NAME),
    }
    payload.update(overrides)
    response = client.post(f"{PREFIX}/bots", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def _async_bot(container: Container, *, name: str = "小助") -> Any:
    """直接建一个绑定了 Agent 档案的 Bot（容器用例没有 REST 入口）。"""
    agent = await container.agents.create(
        AgentSpec(name=f"{name}-{uuid.uuid4().hex[:6]}", role=AgentRole.DEVELOPER)
    )
    return await container.bots.create(name=name, description="做长活", agent_id=str(agent.id))


def _fake(client: TestClient) -> _FakeAssistant:
    fake = _FakeAssistant()
    client.app.state.container.assistant = fake  # type: ignore[attr-defined]
    return fake


def _container(client: TestClient) -> Container:
    return client.app.state.container  # type: ignore[attr-defined]


def _create_subtask(
    client: TestClient,
    bot_id: str,
    *,
    instruction: str = "整理本周周报",
    title: str | None = "周报",
):
    body: dict[str, Any] = {"instruction": instruction}
    if title is not None:
        body["title"] = title
    return client.post(f"{PREFIX}/bots/{bot_id}/subtasks", json=body)


def _get_subtask(client: TestClient, bot_id: str, subtask_id: str) -> dict:
    response = client.get(f"{PREFIX}/bots/{bot_id}/subtasks/{subtask_id}")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _wait_subtask(
    client: TestClient,
    bot_id: str,
    subtask_id: str,
    *,
    statuses: set[str] | None = None,
    timeout: float = 10.0,
) -> dict:
    wanted = statuses or TERMINAL
    deadline = time.monotonic() + timeout
    last: dict | None = None
    while time.monotonic() < deadline:
        last = _get_subtask(client, bot_id, subtask_id)
        # 状态由内核桥接先落库、结果随后补写：终态 + 结果都到位才算收完。
        if last["status"] in wanted and last["result"] is not None:
            return last
        time.sleep(0.02)
    raise AssertionError(f"子任务 {subtask_id} 未在 {timeout}s 内到达 {wanted}：{last}")


def _wait_kernel_run_started(
    client: TestClient, bot_id: str, subtask_id: str, *, timeout: float = 10.0
) -> dict:
    """等到内核 Run 真的起来了（进程已拉起，`run_started` 流事件已落）。

    不能只看任务行上的 run_id：`TaskRunService.start` 在拉起 CLI 之前就先写了 run_id，
    这时候取消会撞上「进程刚起来还没人读管道」的窗口，进程清理会偶发失败。
    """
    deadline = time.monotonic() + timeout
    last: dict | None = None
    while time.monotonic() < deadline:
        events = _container(client).task_stream.poll(uuid.UUID(subtask_id))
        if any(event.type == "run_started" for event in events):
            last = _get_subtask(client, bot_id, subtask_id)
            if last["status"] == "running" and last["run_id"]:
                return last
        last = _get_subtask(client, bot_id, subtask_id)
        if last["status"] in TERMINAL:
            raise AssertionError(f"子任务在内核 Run 起来前就结束了：{last}")
        time.sleep(0.02)
    raise AssertionError(f"子任务 {subtask_id} 未在 {timeout}s 内起内核 Run：{last}")


async def _wait_task(
    container: Container,
    task_id: uuid.UUID,
    *,
    timeout: float = 10.0,
    statuses: set[str] | None = None,
) -> Any:
    wanted = statuses or TERMINAL
    deadline = time.monotonic() + timeout
    task: Any = None
    while time.monotonic() < deadline:
        task = await container.task_repo.get(task_id)
        if task.status in wanted and task.result is not None:
            return task
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"任务 {task_id} 未在 {timeout}s 内到达 {wanted}：{task.status}/{task.result}"
    )


async def _wait_run_started(
    container: Container, task_id: uuid.UUID, *, timeout: float = 10.0
) -> Any:
    """等 CLI 进程真的拉起来（run_started 流事件落地），不只是任务行被标成 running。"""
    deadline = time.monotonic() + timeout
    task: Any = None
    while time.monotonic() < deadline:
        events = container.task_stream.poll(task_id)
        task = await container.task_repo.get(task_id)
        if any(event.type == "run_started" for event in events) and task.status == "running":
            return task
        if task.status in TERMINAL:
            raise AssertionError(f"任务在内核 Run 起来前就结束了：{task.status}")
        await asyncio.sleep(0.02)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内起内核 Run")


async def _dispose(container: Container) -> None:
    await container.dispose()


def _wait_turn(client: TestClient, bot_id: str, turn_id: str, timeout: float = 10) -> dict:
    response = client.get(f"{PREFIX}/bots/{bot_id}/turns/{turn_id}", params={"wait": timeout})
    assert response.status_code == 200, response.text
    return response.json()["data"]["turn"]


def _conversation(client: TestClient, bot_id: str) -> dict:
    response = client.get(f"{PREFIX}/bots/{bot_id}/conversation", params={"limit": 200})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _subtask_messages(conversation: dict) -> list[dict]:
    return [message for message in conversation["messages"] if message["kind"] == "subtask"]


def _wait_subtask_messages(
    client: TestClient,
    bot_id: str,
    subtask_id: str,
    status: str,
    *,
    timeout: float = 5.0,
) -> list[dict]:
    """等父对话里该子任务的最后一条状态消息到达期望状态。

    `BotSubtaskService._finish` 先落终态与结果、再由 `_publish_parent` 追加父对话消息，
    所以 `_wait_subtask` 返回时消息可能还差最后一小段；这里补一个有界轮询。
    """
    deadline = time.monotonic() + timeout
    messages: list[dict] = []
    while time.monotonic() < deadline:
        messages = _subtask_messages(_conversation(client, bot_id))
        own = [message for message in messages if message["payload"]["subtask_id"] == subtask_id]
        if own and own[-1]["payload"]["status"] == status:
            return messages
        time.sleep(0.02)
    raise AssertionError(
        f"子任务 {subtask_id} 的父对话消息未在 {timeout}s 内变成 {status}：{messages}"
    )


def _wait_bus_event(container: Container, name: str, *, timeout: float = 5.0) -> None:
    """等总线事件落地：流事件与总线事件都在父对话消息之后才发，先读会撞窗口。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(event_name == name for event_name, _payload in container.bus.history):
            return
        time.sleep(0.02)
    event_names = [event_name for event_name, _payload in container.bus.history]
    raise AssertionError(f"总线事件 {name} 未在 {timeout}s 内出现：{event_names}")


def _runs(client: TestClient, task_id: str) -> list[dict]:
    response = client.get(f"{PREFIX}/tasks/{task_id}/runs")
    assert response.status_code == 200, response.text
    return response.json()["data"]


@contextmanager
def _capture_subtask_logs() -> Iterator[list[logging.LogRecord]]:
    """抓子任务模块自己的日志。

    不走 caplog：`create_app` 会重建根 logger 的 handler，测试里再挂一个专属 handler
    最稳，也不受全局日志级别影响。
    """
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    module_logger = logging.getLogger("flux.core.bot.subtasks")
    previous_level = module_logger.level
    module_logger.addHandler(handler)
    module_logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        module_logger.removeHandler(handler)
        module_logger.setLevel(previous_level)


def _log_text(records: list[logging.LogRecord]) -> list[str]:
    return [record.getMessage() for record in records]


def _wait_log(
    records: list[logging.LogRecord],
    marker: str,
    bot_id: str,
    subtask_id: str,
    *,
    timeout: float = 15.0,
) -> bool:
    """等某条带 bot/subtask id 的日志出现（状态与结果先落库，日志紧随其后）。

    终态与结果先落库，收尾日志要等父对话消息与总线事件发完才写；全量测试并行
    负载高时这段收尾可能明显变慢，故等待窗口留足余量，避免按机器负载偶发失败。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(
            marker in record.getMessage()
            and bot_id in record.getMessage()
            and subtask_id in record.getMessage()
            for record in records
        ):
            return True
        time.sleep(0.02)
    return False


# --- 正常路径（内核 Run 完成）---


def test_subtask_completes_on_kernel_and_posts_result(settings: Settings, tmp_path: Path) -> None:
    with _kernel_app(settings, tmp_path) as (client, log_path, _sentinel):
        fake = _fake(client)
        bot = _create_bot(client)

        created = _create_subtask(client, bot["id"])
        assert created.status_code == 202, created.text
        subtask = created.json()["data"]
        assert subtask["origin"] == "subtask"
        assert subtask["parent_task_id"]
        assert subtask["description"] == "整理本周周报"
        assert subtask["status"] in {"pending", "running"}
        assert subtask["bot_id"] == bot["id"]

        listed = client.get(f"{PREFIX}/bots/{bot['id']}/subtasks")
        assert listed.status_code == 200, listed.text
        assert [item["id"] for item in listed.json()["data"]] == [subtask["id"]]

        done = _wait_subtask(client, bot["id"], subtask["id"])
        assert done["status"] == "completed"
        # 结果正文来自内核 Run 的最终回复（fake CLI 固定回 "done"）
        assert done["result"]["reply"] == "done"
        assert done["result"]["run_id"] == done["run_id"]
        assert [run["status"] for run in _runs(client, subtask["id"])] == ["completed"]
        # 内核收到的就是这条指令（prompt 固定是 argv 的最后一段）
        argv = read_cli_argv(log_path)
        assert argv and "整理本周周报" in argv[-1][-1]

        completed_only = client.get(
            f"{PREFIX}/bots/{bot['id']}/subtasks", params={"status": "completed"}
        ).json()["data"]
        assert [item["id"] for item in completed_only] == [subtask["id"]]
        failed_only = client.get(
            f"{PREFIX}/bots/{bot['id']}/subtasks", params={"status": "failed"}
        ).json()["data"]
        assert failed_only == []

        conversation = _conversation(client, bot["id"])
        assert conversation["chat_task_id"] == subtask["parent_task_id"]
        messages = _wait_subtask_messages(client, bot["id"], subtask["id"], "completed")
        assert [message["payload"]["status"] for message in messages] == ["running", "completed"]
        assert all(message["payload"]["subtask_id"] == subtask["id"] for message in messages)
        assert all(message["payload"]["title"] == "周报" for message in messages)
        assert "已开始" in messages[0]["content"]
        assert "已完成" in messages[1]["content"]
        assert "done" in messages[1]["content"]

        # 事件总线与父对话流事件都要能看到起止
        container = _container(client)
        _wait_bus_event(container, Events.SUBTASK_COMPLETED)
        event_names = [name for name, _payload in container.bus.history]
        assert Events.SUBTASK_STARTED in event_names
        assert Events.SUBTASK_COMPLETED in event_names
        stream_events = container.task_stream.poll(uuid.UUID(subtask["parent_task_id"]))
        assert [event.type for event in stream_events].count("subtask_update") >= 2

        # 之后的聊天轮次必须能在模型上下文里看到子任务结果
        turn_response = client.post(
            f"{PREFIX}/bots/{bot['id']}/turns", json={"content": "刚才的结果呢"}
        )
        turn_id = turn_response.json()["data"]["turn"]["id"]
        assert _wait_turn(client, bot["id"], turn_id)["status"] == "completed"
        history = [message.content for message in fake.calls[-1]["history"]]
        assert any("已完成" in content for content in history)


def test_chat_turn_can_run_while_subtask_is_still_running(
    settings: Settings, tmp_path: Path
) -> None:
    with _kernel_app(settings, tmp_path, hold=True) as (client, _log, sentinel):
        _fake(client)
        bot = _create_bot(client)

        created = _create_subtask(
            client, bot["id"], instruction="慢任务：整理全部资料", title="慢活"
        )
        assert created.status_code == 202, created.text
        subtask_id = created.json()["data"]["id"]

        # 等内核 Run 真正进入执行，再发一条聊天消息。
        started = _wait_kernel_run_started(client, bot["id"], subtask_id)
        assert started["run_id"]

        turn_response = client.post(
            f"{PREFIX}/bots/{bot['id']}/turns", json={"content": "先回我一句"}
        )
        turn_id = turn_response.json()["data"]["turn"]["id"]
        done_turn = _wait_turn(client, bot["id"], turn_id)
        assert done_turn["status"] == "completed"

        # 聊天回复先到，子任务还在跑：子任务没有占住对话轮次。
        assert _get_subtask(client, bot["id"], subtask_id)["status"] == "running"

        # 放行内核替身进程，子任务随后自行收尾。
        sentinel.touch()
        completed = _wait_subtask(client, bot["id"], subtask_id)
        assert completed["status"] == "completed"
        messages = _wait_subtask_messages(client, bot["id"], subtask_id, "completed")
        assert [message["payload"]["status"] for message in messages] == ["running", "completed"]


# --- 失败路径 ---


def test_failed_kernel_run_posts_failure(settings: Settings, tmp_path: Path) -> None:
    with _kernel_app(settings, tmp_path, failing=True) as (client, _log, _sentinel):
        _fake(client)
        bot = _create_bot(client)

        created = _create_subtask(client, bot["id"], instruction="会失败的任务")
        subtask_id = created.json()["data"]["id"]
        failed = _wait_subtask(client, bot["id"], subtask_id)

        assert failed["status"] == "failed"
        # 失败原因来自内核 Run 的终态（非 0 退出码），不写 traceback
        assert "退出码" in failed["result"]["error"]
        assert [run["status"] for run in _runs(client, subtask_id)] == ["failed"]
        messages = _wait_subtask_messages(client, bot["id"], subtask_id, "failed")
        assert messages[-1]["payload"]["status"] == "failed"
        assert "退出码" in messages[-1]["content"]
        assert "Traceback" not in messages[-1]["content"]
        assert "Traceback" not in str(messages[-1]["payload"])
        container = _container(client)
        _wait_bus_event(container, Events.SUBTASK_FAILED)
        event_names = [name for name, _payload in container.bus.history]
        assert Events.SUBTASK_FAILED in event_names


def test_start_failure_marks_subtask_failed(settings: Settings, tmp_path: Path) -> None:
    with _kernel_app(settings, tmp_path) as (client, _log, _sentinel):
        _fake(client)
        container = _container(client)
        bot = _create_bot(client)

        async def _boom(task: Any, **_kwargs: Any) -> None:
            raise RuntimeError("内核不可用")

        original = container.task_run_service.start
        container.task_run_service.start = _boom  # type: ignore[assignment]
        try:
            with _capture_subtask_logs() as records:
                subtask_id = _create_subtask(client, bot["id"], instruction="起不来").json()[
                    "data"
                ]["id"]
                failed = _wait_subtask(client, bot["id"], subtask_id)
        finally:
            container.task_run_service.start = original  # type: ignore[assignment]

        assert failed["status"] == "failed"
        assert failed["result"]["error"] == "内核不可用"
        messages = _wait_subtask_messages(client, bot["id"], subtask_id, "failed")
        assert messages[-1]["payload"]["status"] == "failed"
        assert "内核不可用" in messages[-1]["content"]
        logs = _log_text(records)
        assert any(
            "启动内核 Run 失败" in message and bot["id"] in message and subtask_id in message
            for message in logs
        ), logs


def test_subtask_timeout_fails_and_stops_kernel_run(settings: Settings, tmp_path: Path) -> None:
    with _kernel_app(settings, tmp_path, hold=True, timeout_seconds=0.3) as (client, _log, _sent):
        _fake(client)
        bot = _create_bot(client)

        subtask_id = _create_subtask(client, bot["id"], instruction="跑不完的活").json()["data"][
            "id"
        ]
        timed_out = _wait_subtask(client, bot["id"], subtask_id)

        assert timed_out["status"] == "failed"
        assert "执行超过" in timed_out["result"]["error"]
        messages = _wait_subtask_messages(client, bot["id"], subtask_id, "failed")
        assert messages[-1]["payload"]["status"] == "failed"
        assert "执行超过" in messages[-1]["content"]


# --- 日志 ---


def test_created_running_completed_transitions_are_logged(
    settings: Settings, tmp_path: Path
) -> None:
    with _kernel_app(settings, tmp_path) as (client, _log, _sentinel):
        _fake(client)
        bot = _create_bot(client)

        with _capture_subtask_logs() as records:
            subtask_id = _create_subtask(client, bot["id"]).json()["data"]["id"]
            _wait_subtask(client, bot["id"], subtask_id)
            # 完成日志在终态与父对话消息之后才写：必须留在抓取上下文里等它落地，
            # 否则 handler 已被移除，_wait_log 只能看到进入终态前的那几条。
            for marker in ("已创建", "开始", "完成"):
                assert _wait_log(records, marker, bot["id"], subtask_id), (
                    f"{marker} 日志缺少 bot/subtask id：{_log_text(records)}"
                )


def test_failed_and_cancelled_transitions_are_logged(settings: Settings, tmp_path: Path) -> None:
    with _kernel_app(settings, tmp_path) as (client, _log, _sentinel):
        _fake(client)
        with _capture_subtask_logs() as records:
            failing_client_bot = _create_bot(client, name="会失败的 Bot")
            # 起 Run 失败路径同样必须留一条带 bot/subtask id 的失败日志
            container = _container(client)

            async def _boom(task: Any, **_kwargs: Any) -> None:
                raise RuntimeError("内核炸了")

            original = container.task_run_service.start
            container.task_run_service.start = _boom  # type: ignore[assignment]
            try:
                failed_id = _create_subtask(
                    client, failing_client_bot["id"], instruction="会失败"
                ).json()["data"]["id"]
                _wait_subtask(client, failing_client_bot["id"], failed_id)
            finally:
                container.task_run_service.start = original  # type: ignore[assignment]
        failed_logs = _log_text(records)
        assert any(
            "失败" in message and failing_client_bot["id"] in message and failed_id in message
            for message in failed_logs
        ), failed_logs
        # 失败原因进日志，但整段指令不进（日志不承载 prompt）
        assert any("内核炸了" in message for message in failed_logs)
        assert not any("会失败" in message for message in failed_logs)


# --- 取消 ---


def test_cancel_inflight_subtask_cancels_kernel_run(settings: Settings, tmp_path: Path) -> None:
    with _kernel_app(settings, tmp_path, hold=True) as (client, _log, _sentinel):
        _fake(client)
        bot = _create_bot(client)

        created = _create_subtask(client, bot["id"], instruction="很慢的任务", title="慢活")
        subtask_id = created.json()["data"]["id"]
        started = _wait_kernel_run_started(client, bot["id"], subtask_id)
        run_id = started["run_id"]

        cancelled = client.post(f"{PREFIX}/bots/{bot['id']}/subtasks/{subtask_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["data"]["status"] == "cancelled"

        # 内核 Run 必须一起停：留一个还在改文件的 Agent 与「已取消」是矛盾的。
        runs = _runs(client, subtask_id)
        assert [run["run_id"] for run in runs] == [run_id]
        assert runs[0]["status"] == "cancelled"

        messages = _wait_subtask_messages(client, bot["id"], subtask_id, "cancelled")
        assert messages[-1]["payload"]["status"] == "cancelled"
        assert "已取消" in messages[-1]["content"]

        again = client.post(f"{PREFIX}/bots/{bot['id']}/subtasks/{subtask_id}/cancel")
        assert again.status_code == 409, again.text
        assert again.json()["code"] == "conflict"

        asserted = _get_subtask(client, bot["id"], subtask_id)
        assert asserted["status"] == "cancelled"


def test_cancel_immediately_after_create_stays_cancelled(
    settings: Settings, tmp_path: Path
) -> None:
    """取消可能赶在后台协程启动之前：running 的写入不能把 cancelled 顶回去。"""
    with _kernel_app(settings, tmp_path, hold=True) as (client, _log, _sentinel):
        _fake(client)
        bot = _create_bot(client)
        subtask_id = _create_subtask(client, bot["id"], instruction="还没开始").json()["data"]["id"]

        cancelled = client.post(f"{PREFIX}/bots/{bot['id']}/subtasks/{subtask_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["data"]["status"] == "cancelled"

        # 给后台 job 一点时间；它若还在跑，也不能把状态改回 running/completed。
        time.sleep(0.3)
        assert _get_subtask(client, bot["id"], subtask_id)["status"] == "cancelled"


# --- 4xx ---


def test_invalid_requests_return_4xx(settings: Settings, tmp_path: Path) -> None:
    with _kernel_app(settings, tmp_path) as (client, _log, _sentinel):
        _fake(client)
        bot = _create_bot(client)
        unknown = uuid.uuid4()

        missing_bot = client.post(
            f"{PREFIX}/bots/{unknown}/subtasks", json={"instruction": "还有人吗"}
        )
        assert missing_bot.status_code == 404, missing_bot.text

        missing_list = client.get(f"{PREFIX}/bots/{unknown}/subtasks")
        assert missing_list.status_code == 404, missing_list.text

        missing_get = client.get(f"{PREFIX}/bots/{unknown}/subtasks/{unknown}")
        assert missing_get.status_code == 404, missing_get.text

        missing_cancel = client.post(f"{PREFIX}/bots/{unknown}/subtasks/{unknown}/cancel")
        assert missing_cancel.status_code == 404, missing_cancel.text

        empty = _create_subtask(client, bot["id"], instruction="")
        assert empty.status_code == 422, empty.text

        whitespace = _create_subtask(client, bot["id"], instruction="   ")
        assert whitespace.status_code == 422, whitespace.text

        missing_instruction = client.post(f"{PREFIX}/bots/{bot['id']}/subtasks", json={})
        assert missing_instruction.status_code == 422, missing_instruction.text

        too_long_title = _create_subtask(client, bot["id"], title="长" * 201)
        assert too_long_title.status_code == 422, too_long_title.text

        unknown_subtask = client.get(f"{PREFIX}/bots/{bot['id']}/subtasks/{unknown}")
        assert unknown_subtask.status_code == 404, unknown_subtask.text

        unknown_cancel = client.post(f"{PREFIX}/bots/{bot['id']}/subtasks/{unknown}/cancel")
        assert unknown_cancel.status_code == 404, unknown_cancel.text

        bad_status = client.get(f"{PREFIX}/bots/{bot['id']}/subtasks", params={"status": "done"})
        assert bad_status.status_code == 400, bad_status.text

        bad_limit = client.get(f"{PREFIX}/bots/{bot['id']}/subtasks", params={"limit": 0})
        assert bad_limit.status_code == 400, bad_limit.text


def test_subtask_is_scoped_to_its_own_bot(settings: Settings, tmp_path: Path) -> None:
    """别的 Bot 拿着 id 也查不到/取消不了：按不存在处理，不泄露归属。"""
    with _kernel_app(settings, tmp_path) as (client, _log, _sentinel):
        _fake(client)
        owner = _create_bot(client, name="主人")
        other = _create_bot(client, name="路人")
        subtask_id = _create_subtask(client, owner["id"]).json()["data"]["id"]
        _wait_subtask(client, owner["id"], subtask_id)

        fetched = client.get(f"{PREFIX}/bots/{other['id']}/subtasks/{subtask_id}")
        assert fetched.status_code == 404, fetched.text

        cancelled = client.post(f"{PREFIX}/bots/{other['id']}/subtasks/{subtask_id}/cancel")
        assert cancelled.status_code == 404, cancelled.text

        # 子任务仍然属于原 Bot，状态没被上面的请求改动
        assert _get_subtask(client, owner["id"], subtask_id)["status"] == "completed"


# --- 列表过滤 ---


def test_subtasks_are_hidden_from_default_task_lists(settings: Settings, tmp_path: Path) -> None:
    with _kernel_app(settings, tmp_path) as (client, _log, _sentinel):
        _fake(client)
        bot = _create_bot(client)
        created = _create_subtask(client, bot["id"])
        subtask_id = created.json()["data"]["id"]
        _wait_subtask(client, bot["id"], subtask_id)

        home = client.get(f"{PREFIX}/tasks").json()["data"]
        assert subtask_id not in {item["id"] for item in home}

        bot_tasks = client.get(f"{PREFIX}/bots/{bot['id']}/tasks").json()["data"]
        assert subtask_id not in {item["id"] for item in bot_tasks}

        # 首页列表默认还会挡掉 Bot 任务；显式按 origin 查子任务时要连 Bot 任务一起看。
        explicit = client.get(
            f"{PREFIX}/tasks", params={"origin": "subtask", "include_bot_tasks": True}
        ).json()["data"]
        assert subtask_id in {item["id"] for item in explicit}

        listed = client.get(f"{PREFIX}/bots/{bot['id']}/subtasks").json()["data"]
        assert [item["id"] for item in listed] == [subtask_id]


# --- 工具入口 ---


def test_bot_chat_turn_injects_subtask_tools(settings: Settings, tmp_path: Path) -> None:
    with _kernel_app(settings, tmp_path) as (client, _log, _sentinel):
        fake = _fake(client)
        bot = _create_bot(client)
        turn_response = client.post(f"{PREFIX}/bots/{bot['id']}/turns", json={"content": "你好"})
        turn_id = turn_response.json()["data"]["turn"]["id"]
        assert _wait_turn(client, bot["id"], turn_id)["status"] == "completed"

        call = fake.calls[-1]
        names = {tool["function"]["name"] for tool in call["tools"]}
        assert names >= SUBTASK_TOOL_NAMES
        assert "subtask_start" in call["tools_note"]


def test_legacy_task_turn_does_not_get_subtask_tools(settings: Settings, tmp_path: Path) -> None:
    with _kernel_app(settings, tmp_path) as (client, _log, _sentinel):
        fake = _fake(client)
        task = client.post(f"{PREFIX}/tasks", json={"description": "旧对话"}).json()["data"]
        response = client.post(f"{PREFIX}/tasks/{task['id']}/messages", json={"content": "在吗"})
        assert response.status_code == 200, response.text

        call = fake.calls[-1]
        assert call.get("tools") is None
        assert "subtask" not in str(call.get("tools_note") or "")


def test_subtask_tool_runner_starts_gets_and_lists(settings: Settings, tmp_path: Path) -> None:
    with _kernel_container(settings, tmp_path) as (container, _log, _sentinel):

        async def scenario() -> None:
            try:
                bot = await _async_bot(container, name="工具")
                started = await run_subtask_tool(
                    container.bot_subtasks,
                    bot,
                    "subtask_start",
                    {"instruction": "整理资料", "title": "资料"},
                )
                subtask_id = uuid.UUID(started["subtask_id"])
                assert started["status"] in {"pending", "running"}

                task = await _wait_task(container, subtask_id)
                assert task.status == "completed"
                assert task.result is not None and task.result["reply"] == "done"

                fetched = await run_subtask_tool(
                    container.bot_subtasks, bot, "subtask_get", {"subtask_id": str(subtask_id)}
                )
                assert fetched["id"] == str(subtask_id)
                assert fetched["origin"] == "subtask"

                listed = await run_subtask_tool(container.bot_subtasks, bot, "subtask_list", {})
                assert [item["id"] for item in listed["subtasks"]] == [str(subtask_id)]

                with pytest.raises(BadRequestError):
                    await run_subtask_tool(container.bot_subtasks, bot, "subtask_delete", {})
                with pytest.raises(ValidationError):
                    await run_subtask_tool(
                        container.bot_subtasks, bot, "subtask_start", {"instruction": "  "}
                    )
            finally:
                await _dispose(container)

        asyncio.run(scenario())


def test_subtasks_do_not_block_bot_launch_lock(settings: Settings, tmp_path: Path) -> None:
    """子任务在跑时，手动/定时开工的忙闲判断不能被它锁住。"""
    with _kernel_container(settings, tmp_path, hold=True) as (container, _log, _sentinel):

        async def scenario() -> None:
            try:
                bot = await _async_bot(container, name="巡检")
                subtask = await container.bot_subtasks.create(bot, instruction="很长的工作")
                await _wait_run_started(container, subtask.id)

                assert await container.bots._repo.is_busy(bot.id) is False
                assert subtask.parent_task_id is not None

                # 手动开工仍能拿到一条新的 tasks 行（把真正的 Run 启动替换成空操作）。
                starts: list[uuid.UUID] = []

                async def _noop_start(task: Any, **_kwargs: Any) -> None:
                    starts.append(task.id)

                original = container.task_run_service.start
                container.task_run_service.start = _noop_start  # type: ignore[assignment]
                try:
                    launched = await container.bots.run_now(bot.id, instruction="定时巡检")
                finally:
                    container.task_run_service.start = original  # type: ignore[assignment]
                assert launched.id != subtask.id
                assert starts == [launched.id]
            finally:
                await _dispose(container)

        asyncio.run(scenario())


# --- 停机 ---


def test_shutdown_cancels_inflight_subtask(settings: Settings, tmp_path: Path) -> None:
    with _kernel_container(settings, tmp_path, hold=True) as (container, _log, _sentinel):

        async def scenario() -> None:
            bot = await _async_bot(container, name="停机")
            subtask = await container.bot_subtasks.create(bot, instruction="慢慢来")
            started = await _wait_run_started(container, subtask.id)
            run_id = started.run_id
            with _capture_subtask_logs() as records:
                await asyncio.wait_for(container.bot_subtasks.shutdown(), timeout=10)

            saved = await container.task_repo.get(subtask.id)
            assert saved.status == "cancelled"
            run = await container.run_repo.get(run_id)
            assert run is not None and run.status == "cancelled"
            # 停机收尾也必须留一条带 bot/subtask id 的 cancelled 日志
            assert any(
                "cancelled" in message and str(bot.id) in message and str(subtask.id) in message
                for message in _log_text(records)
            ), _log_text(records)
            assert container.bot_subtasks._jobs == set()
            assert container.bot_subtasks._tasks_by_subtask == {}
            await _dispose(container)

        asyncio.run(scenario())


def test_dispose_does_not_hang_with_inflight_subtask(settings: Settings, tmp_path: Path) -> None:
    with _kernel_container(settings, tmp_path, hold=True) as (container, _log, _sentinel):

        async def scenario() -> None:
            bot = await _async_bot(container, name="退出")
            subtask = await container.bot_subtasks.create(bot, instruction="慢慢来")
            await _wait_run_started(container, subtask.id)
            await asyncio.wait_for(container.dispose(), timeout=15)
            saved = await container.task_repo.get(subtask.id)
            assert saved.status == "cancelled"

        asyncio.run(scenario())
