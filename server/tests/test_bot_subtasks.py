"""Bot 后台子任务契约测试。

覆盖：创建即 202、列表/详情、完成结果回父对话、边跑边聊、失败、取消、4xx、
默认任务列表隐藏子任务、工具入口、停机不挂。真实模型一律替换成记录型替身。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flux.container import Container
from flux.core.bot.subtasks import run_subtask_tool
from flux.core.event.bus import Events
from flux.core.task_engine.assistant import AssistantTurn
from flux.errors import BadRequestError, ValidationError

PREFIX = "/api/v1"
TERMINAL = {"completed", "failed", "cancelled"}
SUBTASK_TOOL_NAMES = {"subtask_start", "subtask_get", "subtask_list"}


class _FakeAssistant:
    """记录型助手替身：可按消息内容放慢/失败，并暴露「慢调用已进入」信号。"""

    def __init__(
        self,
        *,
        error: Exception | None = None,
        delay: float = 0.0,
        delays: dict[str, float] | None = None,
    ) -> None:
        self.error = error
        self.delay = delay
        self.delays = delays or {}
        self.calls: list[dict[str, Any]] = []
        self.active = 0
        self.max_active = 0
        #: 跨线程信号：同步测试线程/事件循环都能观察慢调用是否已经开始。
        self.entered = threading.Event()

    def _delay_for(self, message: str) -> float:
        for needle, value in self.delays.items():
            if needle in message:
                return value
        return self.delay

    async def respond(self, **kwargs: Any) -> AssistantTurn:
        self.calls.append(kwargs)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            delay = self._delay_for(str(kwargs.get("message") or ""))
            if delay >= 0.1:
                self.entered.set()
            if delay:
                await asyncio.sleep(delay)
            if self.error is not None:
                raise self.error
            on_delta = kwargs.get("on_delta")
            if on_delta is not None:
                await on_delta("思考中")
            return AssistantTurn(
                reply=f"echo: {kwargs['message']}",
                provider="fake",
                model="fake-model",
                usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            )
        finally:
            self.active -= 1


def _create_bot(client: TestClient, **overrides: object) -> dict:
    payload: dict[str, object] = {"name": "小助", "description": "负责日常问答"}
    payload.update(overrides)
    response = client.post(f"{PREFIX}/bots", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _fake(client: TestClient, **kwargs: Any) -> _FakeAssistant:
    fake = _FakeAssistant(**kwargs)
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
    timeout: float = 5.0,
) -> dict:
    wanted = statuses or TERMINAL
    deadline = time.monotonic() + timeout
    last: dict | None = None
    while time.monotonic() < deadline:
        last = _get_subtask(client, bot_id, subtask_id)
        if last["status"] in wanted:
            return last
        time.sleep(0.02)
    raise AssertionError(f"子任务 {subtask_id} 未在 {timeout}s 内到达 {wanted}：{last}")


def _wait_turn(client: TestClient, bot_id: str, turn_id: str, timeout: float = 5) -> dict:
    response = client.get(f"{PREFIX}/bots/{bot_id}/turns/{turn_id}", params={"wait": timeout})
    assert response.status_code == 200, response.text
    return response.json()["data"]["turn"]


def _conversation(client: TestClient, bot_id: str) -> dict:
    response = client.get(f"{PREFIX}/bots/{bot_id}/conversation", params={"limit": 200})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _subtask_messages(conversation: dict) -> list[dict]:
    return [message for message in conversation["messages"] if message["kind"] == "subtask"]


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


# --- 正常路径 ---


def test_subtask_completes_and_posts_status_to_parent_conversation(client: TestClient) -> None:
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
    assert "整理本周周报" in done["result"]["reply"]

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
    messages = _subtask_messages(conversation)
    assert [message["payload"]["status"] for message in messages] == ["running", "completed"]
    assert all(message["payload"]["subtask_id"] == subtask["id"] for message in messages)
    assert all(message["payload"]["title"] == "周报" for message in messages)
    assert "已开始" in messages[0]["content"]
    assert "已完成" in messages[1]["content"]
    assert "整理本周周报" in messages[1]["content"]

    # 事件总线与父对话流事件都要能看到起止
    container = _container(client)
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


def test_chat_turn_can_run_while_subtask_is_still_running(client: TestClient) -> None:
    fake = _fake(client, delays={"慢任务": 0.8})
    bot = _create_bot(client)

    created = _create_subtask(client, bot["id"], instruction="慢任务：整理全部资料", title="慢活")
    assert created.status_code == 202, created.text
    subtask_id = created.json()["data"]["id"]

    # 等到模型调用真正进入（子任务已在跑），再发一条聊天消息。
    assert fake.entered.wait(5)
    turn_response = client.post(f"{PREFIX}/bots/{bot['id']}/turns", json={"content": "先回我一句"})
    turn_id = turn_response.json()["data"]["turn"]["id"]
    done_turn = _wait_turn(client, bot["id"], turn_id)
    assert done_turn["status"] == "completed"

    # 聊天回复先到，子任务还在跑；两者确实并发调用过模型。
    assert _get_subtask(client, bot["id"], subtask_id)["status"] == "running"
    assert fake.max_active >= 2

    completed = _wait_subtask(client, bot["id"], subtask_id)
    assert completed["status"] == "completed"
    messages = _subtask_messages(_conversation(client, bot["id"]))
    statuses = [message["payload"]["status"] for message in messages]
    assert statuses == ["running", "completed"]
    # 两条调用是并发的，落库顺序不保证：按内容找聊天那一轮，而不是假设 calls[-2]。
    call_messages = [str(call.get("message") or "") for call in fake.calls]
    assert any("先回我一句" in message for message in call_messages)


def test_failed_subtask_surfaces_error_without_traceback(client: TestClient) -> None:
    _fake(client, error=RuntimeError("模型炸了"))
    bot = _create_bot(client)

    created = _create_subtask(client, bot["id"], instruction="会失败的任务")
    subtask_id = created.json()["data"]["id"]
    failed = _wait_subtask(client, bot["id"], subtask_id)

    assert failed["status"] == "failed"
    assert failed["result"]["error"] == "模型炸了"
    messages = _subtask_messages(_conversation(client, bot["id"]))
    assert messages[-1]["payload"]["status"] == "failed"
    assert "模型炸了" in messages[-1]["content"]
    assert "Traceback" not in messages[-1]["content"]
    assert "Traceback" not in str(messages[-1]["payload"])
    event_names = [name for name, _payload in _container(client).bus.history]
    assert Events.SUBTASK_FAILED in event_names


# --- 日志 ---


def test_created_running_completed_transitions_are_logged(client: TestClient) -> None:
    _fake(client)
    bot = _create_bot(client)

    with _capture_subtask_logs() as records:
        subtask_id = _create_subtask(client, bot["id"]).json()["data"]["id"]
        _wait_subtask(client, bot["id"], subtask_id)

    messages = _log_text(records)
    for marker in ("已创建", "开始", "完成"):
        assert any(
            marker in message and bot["id"] in message and subtask_id in message
            for message in messages
        ), f"{marker} 日志缺少 bot/subtask id：{messages}"


def test_failed_and_cancelled_transitions_are_logged(client: TestClient) -> None:
    _fake(client, error=RuntimeError("模型炸了"))
    bot = _create_bot(client)
    with _capture_subtask_logs() as records:
        failed_id = _create_subtask(client, bot["id"], instruction="会失败").json()["data"]["id"]
        _wait_subtask(client, bot["id"], failed_id)
    failed_logs = _log_text(records)
    assert any(
        "失败" in message and bot["id"] in message and failed_id in message
        for message in failed_logs
    ), failed_logs
    # 失败原因进日志，但整段指令不进（日志不承载 prompt）
    assert any("模型炸了" in message for message in failed_logs)
    assert not any("会失败" in message for message in failed_logs)

    slow = _fake(client, delay=5.0)
    with _capture_subtask_logs() as records:
        cancelled_id = _create_subtask(
            client, bot["id"], instruction="很慢的任务", title="慢活"
        ).json()["data"]["id"]
        assert slow.entered.wait(5)
        response = client.post(f"{PREFIX}/bots/{bot['id']}/subtasks/{cancelled_id}/cancel")
        assert response.status_code == 200, response.text
    cancelled_logs = _log_text(records)
    assert any(
        "已取消" in message and bot["id"] in message and cancelled_id in message
        for message in cancelled_logs
    ), cancelled_logs


# --- 取消 ---


def test_cancel_inflight_subtask_and_reject_second_cancel(client: TestClient) -> None:
    fake = _fake(client, delay=5.0)
    bot = _create_bot(client)

    created = _create_subtask(client, bot["id"], instruction="很慢的任务", title="慢活")
    subtask_id = created.json()["data"]["id"]
    assert fake.entered.wait(5)

    cancelled = client.post(f"{PREFIX}/bots/{bot['id']}/subtasks/{subtask_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["status"] == "cancelled"

    messages = _subtask_messages(_conversation(client, bot["id"]))
    assert messages[-1]["payload"]["status"] == "cancelled"
    assert "已取消" in messages[-1]["content"]

    again = client.post(f"{PREFIX}/bots/{bot['id']}/subtasks/{subtask_id}/cancel")
    assert again.status_code == 409, again.text
    assert again.json()["code"] == "conflict"

    asserted = _get_subtask(client, bot["id"], subtask_id)
    assert asserted["status"] == "cancelled"


def test_cancel_immediately_after_create_stays_cancelled(client: TestClient) -> None:
    """取消可能赶在后台协程启动之前：running 的写入不能把 cancelled 顶回去。"""
    _fake(client, delay=5.0)
    bot = _create_bot(client)
    subtask_id = _create_subtask(client, bot["id"], instruction="还没开始").json()["data"]["id"]

    cancelled = client.post(f"{PREFIX}/bots/{bot['id']}/subtasks/{subtask_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["status"] == "cancelled"

    # 给后台 job 一点时间；它若还在跑，也不能把状态改回 running/completed。
    time.sleep(0.2)
    assert _get_subtask(client, bot["id"], subtask_id)["status"] == "cancelled"


# --- 4xx ---


def test_invalid_requests_return_4xx(client: TestClient) -> None:
    _fake(client)
    bot = _create_bot(client)
    unknown = uuid.uuid4()

    missing_bot = client.post(f"{PREFIX}/bots/{unknown}/subtasks", json={"instruction": "还有人吗"})
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


def test_subtask_is_scoped_to_its_own_bot(client: TestClient) -> None:
    """别的 Bot 拿着 id 也查不到/取消不了：按不存在处理，不泄露归属。"""
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


def test_subtasks_are_hidden_from_default_task_lists(client: TestClient) -> None:
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


def test_bot_chat_turn_injects_subtask_tools(client: TestClient) -> None:
    fake = _fake(client)
    bot = _create_bot(client)
    turn_response = client.post(f"{PREFIX}/bots/{bot['id']}/turns", json={"content": "你好"})
    turn_id = turn_response.json()["data"]["turn"]["id"]
    assert _wait_turn(client, bot["id"], turn_id)["status"] == "completed"

    call = fake.calls[-1]
    names = {tool["function"]["name"] for tool in call["tools"]}
    assert names >= SUBTASK_TOOL_NAMES
    assert "subtask_start" in call["tools_note"]


def test_legacy_task_turn_does_not_get_subtask_tools(client: TestClient) -> None:
    fake = _fake(client)
    task = client.post(f"{PREFIX}/tasks", json={"description": "旧对话"}).json()["data"]
    response = client.post(f"{PREFIX}/tasks/{task['id']}/messages", json={"content": "在吗"})
    assert response.status_code == 200, response.text

    call = fake.calls[-1]
    assert call.get("tools") is None
    assert "subtask" not in str(call.get("tools_note") or "")


def test_subtask_tool_runner_starts_gets_and_lists(container: Container) -> None:
    async def scenario() -> None:
        container.assistant = _FakeAssistant()  # type: ignore[assignment]
        bot = await container.bots.create(name="工具", description="做长活")

        started = await run_subtask_tool(
            container.bot_subtasks,
            bot,
            "subtask_start",
            {"instruction": "整理资料", "title": "资料"},
        )
        subtask_id = uuid.UUID(started["subtask_id"])
        assert started["status"] in {"pending", "running"}

        for _ in range(500):
            task = await container.task_repo.get(subtask_id)
            if task.status in TERMINAL:
                break
            await asyncio.sleep(0.01)
        assert task.status == "completed"

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

    asyncio.run(scenario())


def test_subtasks_do_not_block_bot_launch_lock(container: Container) -> None:
    """子任务在跑时，手动/定时开工的忙闲判断不能被它锁住。"""

    async def scenario() -> None:
        fake = _FakeAssistant(delay=10.0)
        container.assistant = fake  # type: ignore[assignment]
        bot = await container.bots.create(name="巡检", description="检查磁盘")
        subtask = await container.bot_subtasks.create(bot, instruction="很长的工作")

        # 事件循环里不能用阻塞的 Event.wait：它会挡住刚 create 的 job，永远进不到模型调用。
        assert await asyncio.to_thread(fake.entered.wait, 5)
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

    asyncio.run(scenario())


# --- 停机 ---


def test_shutdown_cancels_inflight_subtask(container: Container) -> None:
    async def scenario() -> None:
        fake = _FakeAssistant(delay=30.0)
        container.assistant = fake  # type: ignore[assignment]
        bot = await container.bots.create(name="停机", description="长活")
        subtask = await container.bot_subtasks.create(bot, instruction="慢慢来")

        assert await asyncio.to_thread(fake.entered.wait, 5)
        with _capture_subtask_logs() as records:
            await asyncio.wait_for(container.bot_subtasks.shutdown(), timeout=5)

        saved = await container.task_repo.get(subtask.id)
        assert saved.status == "cancelled"
        # 停机收尾也必须留一条带 bot/subtask id 的 cancelled 日志
        assert any(
            "cancelled" in message and str(bot.id) in message and str(subtask.id) in message
            for message in _log_text(records)
        ), _log_text(records)
        assert container.bot_subtasks._jobs == set()
        assert container.bot_subtasks._tasks_by_subtask == {}

    asyncio.run(scenario())


def test_dispose_does_not_hang_with_inflight_subtask(container: Container) -> None:
    async def scenario() -> None:
        fake = _FakeAssistant(delay=30.0)
        container.assistant = fake  # type: ignore[assignment]
        bot = await container.bots.create(name="退出", description="长活")
        await container.bot_subtasks.create(bot, instruction="慢慢来")

        assert await asyncio.to_thread(fake.entered.wait, 5)
        await asyncio.wait_for(container.dispose(), timeout=10)

    asyncio.run(scenario())
