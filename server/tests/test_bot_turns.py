"""Bot 异步对话轮次（bot_turns）契约测试。

覆盖：创建即 202、长轮询拿回复、回复进对话、记忆进助手上下文、幂等键、
预算耗尽、助手失败、同 Bot 串行、重启中断恢复，以及 legacy /tasks/{id}/messages
行为不变。真实模型一律替换成记录型替身，不触网。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi.testclient import TestClient

from flux.container import Container
from flux.core.task_engine.assistant import AssistantTurn
from flux.enums import DecisionMode, PermissionMode

PREFIX = "/api/v1"


class _FakeAssistant:
    """记录每次调用的助手替身；可按需失败或放慢，用于观察串行与上下文。"""

    def __init__(
        self,
        *,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.error = error
        self.delay = delay
        self.calls: list[dict[str, Any]] = []
        self.active = 0
        self.max_active = 0

    async def respond(self, **kwargs: Any) -> AssistantTurn:
        self.calls.append(kwargs)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
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


def _wait(client: TestClient, bot_id: str, turn_id: str, timeout: float = 5) -> dict:
    response = client.get(f"{PREFIX}/bots/{bot_id}/turns/{turn_id}", params={"wait": timeout})
    assert response.status_code == 200, response.text
    return response.json()["data"]["turn"]


async def _seed_queued_turn(container: Container, *, name: str = "清理") -> tuple[Any, Any]:
    bot = await container.bots.create(name=name, description="检查磁盘")
    task = await container.task_repo.create(
        task_id=uuid.uuid4(),
        description=bot.name,
        priority=100,
        project_id=None,
        agent_id=None,
        decision_mode=DecisionMode.AUTO,
        permission_mode=PermissionMode.UNATTENDED,
        bot_id=bot.id,
    )
    turn, created = await container.bot_conversation.repository.create(
        bot_id=bot.id, task_id=task.id
    )
    assert created
    return bot, turn


def test_create_turn_is_queued_then_completes_with_reply(client: TestClient) -> None:
    fake = _fake(client)
    bot = _create_bot(client)

    created = client.post(f"{PREFIX}/bots/{bot['id']}/turns", json={"content": "你好"})
    assert created.status_code == 202, created.text
    turn = created.json()["data"]["turn"]
    assert turn["status"] in {"queued", "running", "completed"}
    assert turn["id"]

    done = _wait(client, bot["id"], turn["id"])
    assert done["status"] == "completed"
    assert done["error"] is None
    assert done["user_message"]["content"] == "你好"
    assert done["reply_message"]["role"] == "assistant"
    assert "你好" in done["reply_message"]["content"]
    assert fake.calls[-1]["description"] == "负责日常问答"

    # 聊天任务与 bot 绑定，且与 last_task_id（定时开工）互不影响
    refreshed = client.get(f"{PREFIX}/bots/{bot['id']}").json()["data"]
    assert refreshed["chat_task_id"] is not None
    assert refreshed["last_task_id"] is None


def test_reply_is_persisted_in_conversation(client: TestClient) -> None:
    _fake(client)
    bot = _create_bot(client)
    created = client.post(f"{PREFIX}/bots/{bot['id']}/turns", json={"content": "第一句"})
    turn_id = created.json()["data"]["turn"]["id"]
    _wait(client, bot["id"], turn_id)

    conversation = client.get(f"{PREFIX}/bots/{bot['id']}/conversation")
    assert conversation.status_code == 200, conversation.text
    data = conversation.json()["data"]
    assert data["chat_task_id"]
    assert [message["role"] for message in data["messages"]] == ["user", "assistant"]
    assert data["messages"][0]["content"] == "第一句"
    assert "第一句" in data["messages"][1]["content"]

    # 没有对话的 Bot 返回空页，而不是 404
    fresh = _create_bot(client, name="还没聊过")
    empty = client.get(f"{PREFIX}/bots/{fresh['id']}/conversation").json()["data"]
    assert empty == {"chat_task_id": None, "messages": []}


def test_bot_memory_is_injected_into_assistant_context(client: TestClient) -> None:
    fake = _fake(client)
    bot = _create_bot(client, name="小方", description="负责巡检")
    remembered = client.post(
        f"{PREFIX}/bots/{bot['id']}/memories", json={"content": "我负责每天检查磁盘"}
    )
    assert remembered.status_code == 200, remembered.text

    created = client.post(f"{PREFIX}/bots/{bot['id']}/turns", json={"content": "今天做什么"})
    turn_id = created.json()["data"]["turn"]["id"]
    done = _wait(client, bot["id"], turn_id)
    assert done["status"] == "completed"

    context = fake.calls[-1]["context"] or ""
    assert "我负责每天检查磁盘" in context
    assert fake.calls[-1]["description"] == "负责巡检"


def test_client_turn_id_is_idempotent(client: TestClient) -> None:
    _fake(client)
    bot = _create_bot(client)
    payload = {"content": "同一条", "client_turn_id": "android-retry-1"}

    first = client.post(f"{PREFIX}/bots/{bot['id']}/turns", json=payload)
    second = client.post(f"{PREFIX}/bots/{bot['id']}/turns", json=payload)
    assert first.status_code == 202, first.text
    assert second.status_code == 200, second.text
    first_turn = first.json()["data"]["turn"]
    second_turn = second.json()["data"]["turn"]
    assert second_turn["id"] == first_turn["id"]
    _wait(client, bot["id"], first_turn["id"])

    turns = client.get(f"{PREFIX}/bots/{bot['id']}/turns").json()["data"]
    assert [item["id"] for item in turns] == [first_turn["id"]]


def test_budget_exhausted_returns_409_code(client: TestClient) -> None:
    _fake(client)
    bot = _create_bot(client, token_budget=10)
    created = client.post(f"{PREFIX}/bots/{bot['id']}/turns", json={"content": "先聊一句"})
    turn_id = created.json()["data"]["turn"]["id"]
    assert _wait(client, bot["id"], turn_id)["status"] == "completed"

    # 上一条已按聊天任务归因记入账本（10+5 tokens），预算 10 已耗尽
    refreshed = client.get(f"{PREFIX}/bots/{bot['id']}").json()["data"]
    assert refreshed["tokens_used_today"] >= 15

    blocked = client.post(f"{PREFIX}/bots/{bot['id']}/turns", json={"content": "还能聊吗"})
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "budget_exhausted"


def test_assistant_failure_marks_turn_failed(client: TestClient) -> None:
    _fake(client, error=RuntimeError("模型炸了"))
    bot = _create_bot(client)
    created = client.post(f"{PREFIX}/bots/{bot['id']}/turns", json={"content": "会失败"})
    turn_id = created.json()["data"]["turn"]["id"]

    done = _wait(client, bot["id"], turn_id)
    assert done["status"] == "failed"
    assert done["error_code"] == "assistant_error"
    assert "模型炸了" in (done["error"] or "")
    assert done["reply_message"] is None


def test_same_bot_turns_are_processed_sequentially(client: TestClient) -> None:
    fake = _fake(client, delay=0.05)
    bot = _create_bot(client)
    first = client.post(f"{PREFIX}/bots/{bot['id']}/turns", json={"content": "第一轮"})
    second = client.post(f"{PREFIX}/bots/{bot['id']}/turns", json={"content": "第二轮"})
    first_id = first.json()["data"]["turn"]["id"]
    second_id = second.json()["data"]["turn"]["id"]

    first_done = _wait(client, bot["id"], first_id)
    second_done = _wait(client, bot["id"], second_id)
    assert first_done["status"] == "completed"
    assert second_done["status"] == "completed"
    assert fake.max_active == 1
    assert [call["message"] for call in fake.calls] == ["第一轮", "第二轮"]

    conversation = client.get(f"{PREFIX}/bots/{bot['id']}/conversation").json()["data"]
    assert [message["content"] for message in conversation["messages"][:2]] == [
        "第一轮",
        "echo: 第一轮",
    ]


def test_interrupted_turn_recovery(container: Container) -> None:
    async def scenario() -> None:
        bot = await container.bots.create(name="巡检", description="检查磁盘")
        task = await container.task_repo.create(
            task_id=uuid.uuid4(),
            description=bot.name,
            priority=100,
            project_id=None,
            agent_id=None,
            decision_mode=DecisionMode.AUTO,
            permission_mode=PermissionMode.UNATTENDED,
            bot_id=bot.id,
        )
        turn, created = await container.bot_conversation.repository.create(
            bot_id=bot.id, task_id=task.id
        )
        assert created
        await container.bot_conversation.repository.set_running(turn.id)

        assert await container.bot_conversation.recover_interrupted() == 1
        recovered = await container.bot_conversation.get_turn(bot.id, turn.id)
        assert recovered.status == "failed"
        assert recovered.error_code == "interrupted"
        assert recovered.completed_at is not None

    asyncio.run(scenario())


def test_terminal_turn_releases_waiter_signal_state(container: Container) -> None:
    async def scenario() -> None:
        bot, turn = await _seed_queued_turn(container)
        service = container.bot_conversation
        waiter = asyncio.create_task(service.wait_for_turn(bot.id, turn.id, wait_seconds=5))
        try:
            for _ in range(100):
                if service._waiters.get(turn.id) == 1:
                    break
                await asyncio.sleep(0.01)
            assert service._waiters[turn.id] == 1

            await service._fail(turn.id, "停止", "interrupted")
            done = await asyncio.wait_for(waiter, timeout=2)
            assert done.status == "failed"
            assert service._conditions == {}
            assert service._revisions == {}
            assert service._waiters == {}
            assert service._terminal_turns == set()
        finally:
            if not waiter.done():
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)

    asyncio.run(scenario())


def test_terminal_notify_without_waiter_releases_signal_state(container: Container) -> None:
    async def scenario() -> None:
        _bot, turn = await _seed_queued_turn(container, name="没人等")
        service = container.bot_conversation

        await service._fail(turn.id, "停止", "interrupted")

        assert service._conditions == {}
        assert service._revisions == {}
        assert service._waiters == {}
        assert service._terminal_turns == set()

    asyncio.run(scenario())


class _BlockingAssistant:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def respond(self, **_kwargs: Any) -> AssistantTurn:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def test_shutdown_marks_running_turn_interrupted_immediately(container: Container) -> None:
    async def scenario() -> None:
        assistant = _BlockingAssistant()
        container.assistant = assistant  # type: ignore[assignment]
        bot = await container.bots.create(name="运行中", description="等取消")
        turn, created = await container.bot_conversation.create_turn(bot, content="开始")
        assert created
        await asyncio.wait_for(assistant.started.wait(), timeout=2)

        await container.bot_conversation.shutdown()

        saved = await container.bot_conversation.get_turn(bot.id, turn.id)
        assert saved.status == "failed"
        assert saved.error_code == "interrupted"
        assert saved.completed_at is not None
        assert container.bot_conversation._active_turns == {}
        assert container.bot_conversation._workers == {}
        assert container.bot_conversation._queues == {}

    asyncio.run(scenario())


def test_legacy_task_message_endpoint_is_unchanged(client: TestClient) -> None:
    task = client.post(f"{PREFIX}/tasks", json={"description": "旧对话接口"}).json()["data"]
    response = client.post(f"{PREFIX}/tasks/{task['id']}/messages", json={"content": "还在吗"})
    assert response.status_code == 200, response.text
    body = response.json()
    user_message, reply = body["data"]["messages"]
    assert user_message["role"] == "user"
    assert user_message["seq"] == 1
    assert reply["role"] == "assistant"
    assert reply["seq"] == 2
    assert "还在吗" in reply["content"]
    assert body["data"]["task"]["status"] == "running"
    assert body["metadata"]["provider"] == "local"
