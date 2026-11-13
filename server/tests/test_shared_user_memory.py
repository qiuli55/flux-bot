"""跨 Bot 共享用户记忆测试。

覆盖：画像 put/get/delete 与校验、日志追加与规范化去重、组合视图、
每轮上下文叠加共享块、共享层故障降级，以及服务层的写入策略。

真实模型一律用记录型替身，不触网。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flux.container import Container
from flux.core.memory.shared_user import BLOCK_MAX_CHARS
from flux.core.task_engine.assistant import AssistantTurn
from flux.errors import ValidationError

MEMORY_PREFIX = "/api/v1/user-memory"
PREFIX = "/api/v1"


class _FakeAssistant:
    """记录每次调用参数的助手替身。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def respond(self, **kwargs: Any) -> AssistantTurn:
        self.calls.append(kwargs)
        return AssistantTurn(
            reply=f"echo: {kwargs['message']}",
            provider="fake",
            model="fake-model",
            usage={"input_tokens": 10, "output_tokens": 5},
        )


def _fake(client: TestClient) -> _FakeAssistant:
    fake = _FakeAssistant()
    client.app.state.container.assistant = fake  # type: ignore[attr-defined]
    return fake


def _create_bot(client: TestClient, **overrides: object) -> dict:
    payload: dict[str, object] = {"name": "小助", "description": "负责日常问答"}
    payload.update(overrides)
    response = client.post(f"{PREFIX}/bots", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _wait(client: TestClient, bot_id: str, turn_id: str, timeout: float = 5) -> dict:
    response = client.get(f"{PREFIX}/bots/{bot_id}/turns/{turn_id}", params={"wait": timeout})
    assert response.status_code == 200, response.text
    return response.json()["data"]["turn"]


def _append_journal(client: TestClient, content: str, **overrides: object) -> dict:
    payload: dict[str, object] = {"content": content}
    payload.update(overrides)
    response = client.post(f"{MEMORY_PREFIX}/journal", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["data"]


# --- 画像 ---


def test_profile_put_get_delete(client: TestClient) -> None:
    put = client.put(
        f"{MEMORY_PREFIX}/profile",
        json={"name": "张三", "locale": "zh-CN", "timezone": "Asia/Shanghai"},
    )
    assert put.status_code == 200, put.text
    assert put.json()["data"]["profile"] == {
        "name": "张三",
        "locale": "zh-CN",
        "timezone": "Asia/Shanghai",
    }

    got = client.get(f"{MEMORY_PREFIX}/profile")
    assert got.status_code == 200, got.text
    assert got.json()["data"]["profile"]["name"] == "张三"

    # 再次 PUT 只覆盖提供的 key，其余保留
    merged = client.put(f"{MEMORY_PREFIX}/profile", json={"name": "李四"})
    assert merged.status_code == 200, merged.text
    profile = merged.json()["data"]["profile"]
    assert profile["name"] == "李四"
    assert profile["locale"] == "zh-CN"

    deleted = client.delete(f"{MEMORY_PREFIX}/profile/locale")
    assert deleted.status_code == 200, deleted.text
    assert client.get(f"{MEMORY_PREFIX}/profile").json()["data"]["profile"] == {
        "name": "李四",
        "timezone": "Asia/Shanghai",
    }
    # 再删一次不存在 -> 404
    assert client.delete(f"{MEMORY_PREFIX}/profile/locale").status_code == 404


def test_profile_rejects_unknown_empty_and_oversize_values(client: TestClient) -> None:
    unknown = client.put(f"{MEMORY_PREFIX}/profile", json={"nickname": "老王"})
    assert unknown.status_code == 422, unknown.text
    assert "allowed_keys" in unknown.text

    empty_key = client.put(f"{MEMORY_PREFIX}/profile", json={"": "值"})
    assert empty_key.status_code == 422, empty_key.text

    empty_value = client.put(f"{MEMORY_PREFIX}/profile", json={"name": "   "})
    assert empty_value.status_code == 422, empty_value.text

    oversize = client.put(f"{MEMORY_PREFIX}/profile", json={"name": "x" * 2001})
    assert oversize.status_code == 422, oversize.text

    # 删除未知 key 同样是可读的 4xx
    assert client.delete(f"{MEMORY_PREFIX}/profile/nickname").status_code == 422


# --- 共享日志 ---


def test_journal_append_and_normalized_dedupe(client: TestClient) -> None:
    first = _append_journal(client, "  Alice   prefers Short Answers  ")
    assert first["created"] is True
    first_id = first["entry"]["id"]
    assert first["entry"]["content"] == "Alice   prefers Short Answers"
    assert first["entry"]["source"] == "user"

    duplicate = _append_journal(client, "alice prefers short answers")
    assert duplicate["created"] is False
    assert duplicate["entry"]["id"] == first_id

    rows = client.get(f"{MEMORY_PREFIX}/journal", params={"limit": 200}).json()["data"]
    assert len(rows) == 1
    assert rows[0]["id"] == first_id


def test_journal_rejects_bad_source_and_oversize(client: TestClient) -> None:
    bad_source = client.post(
        f"{MEMORY_PREFIX}/journal", json={"content": "一条观察", "source": "agent"}
    )
    assert bad_source.status_code == 422, bad_source.text

    oversize = client.post(f"{MEMORY_PREFIX}/journal", json={"content": "x" * 4001})
    assert oversize.status_code == 422, oversize.text

    blank = client.post(f"{MEMORY_PREFIX}/journal", json={"content": "   "})
    assert blank.status_code == 422, blank.text


def test_journal_delete_and_source_bot(client: TestClient) -> None:
    bot = _create_bot(client)
    appended = _append_journal(
        client, "这个用户偏好先看结论", source="bot", source_bot_id=bot["id"]
    )
    assert appended["entry"]["source"] == "bot"
    assert appended["entry"]["source_bot_id"] == bot["id"]

    entry_id = appended["entry"]["id"]
    assert client.delete(f"{MEMORY_PREFIX}/journal/{entry_id}").status_code == 200
    assert client.delete(f"{MEMORY_PREFIX}/journal/{entry_id}").status_code == 404


def test_combined_view(client: TestClient) -> None:
    client.put(f"{MEMORY_PREFIX}/profile", json={"name": "张三", "language": "zh"})
    _append_journal(client, "用户喜欢简短回答")

    combined = client.get(MEMORY_PREFIX).json()["data"]
    assert combined["profile"] == {"name": "张三", "language": "zh"}
    assert [row["content"] for row in combined["journal"]] == ["用户喜欢简短回答"]


# --- 注入 ---


def test_turn_context_contains_shared_space_and_bot_memory(client: TestClient) -> None:
    fake = _fake(client)
    bot = _create_bot(client, name="小方", description="负责巡检")
    # 全局共享画像 / 日志不再注入 Bot 对话（它们没有按人隔离）。
    client.put(f"{MEMORY_PREFIX}/profile", json={"name": "张三", "notes": "偏好中文汇报"})
    _append_journal(client, "用户对磁盘告警很敏感")

    space = client.post(f"{PREFIX}/spaces", json={"name": "我们俩"}).json()["data"]
    shared = client.post(
        f"{PREFIX}/spaces/{space['id']}/memories",
        json={"content": "周末一起整理磁盘"},
    )
    assert shared.status_code == 200, shared.text

    remembered = client.post(
        f"{PREFIX}/bots/{bot['id']}/memories", json={"content": "我负责每天检查磁盘"}
    )
    assert remembered.status_code == 200, remembered.text

    created = client.post(f"{PREFIX}/bots/{bot['id']}/turns", json={"content": "今天做什么"})
    turn_id = created.json()["data"]["turn"]["id"]
    done = _wait(client, bot["id"], turn_id)
    assert done["status"] == "completed"

    context = fake.calls[-1]["context"] or ""
    # 每 Bot 记忆仍然在（没有被共享层替换）
    assert "我负责每天检查磁盘" in context
    # 共享空间记忆按成员身份注入
    assert "共享空间「我们俩」" in context
    assert "周末一起整理磁盘" in context
    # 未按人隔离的全局画像 / 日志不再出现
    assert "偏好中文汇报" not in context
    assert "用户对磁盘告警很敏感" not in context


def test_shared_memory_failure_does_not_fail_turn(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake(client)
    bot = _create_bot(client)
    client.post(f"{PREFIX}/bots/{bot['id']}/memories", json={"content": "我负责巡检磁盘"})

    container = client.app.state.container  # type: ignore[attr-defined]

    async def boom(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise RuntimeError("shared memory unavailable")

    monkeypatch.setattr(container.spaces, "memberships", boom)

    created = client.post(f"{PREFIX}/bots/{bot['id']}/turns", json={"content": "在吗"})
    turn_id = created.json()["data"]["turn"]["id"]
    done = _wait(client, bot["id"], turn_id)
    assert done["status"] == "completed"
    # 降级为只有 Bot 记忆，本轮照常完成
    assert "我负责巡检磁盘" in (fake.calls[-1]["context"] or "")


# --- 服务层写入策略 ---


def test_service_rejects_unknown_profile_key_and_bad_journal_source(container: Container) -> None:
    async def scenario() -> None:
        with pytest.raises(ValidationError):
            await container.shared_user_memory.put_profile({"nickname": "老王"})
        with pytest.raises(ValidationError):
            await container.shared_user_memory.put_profile({"name": "x" * 2001})
        with pytest.raises(ValidationError):
            await container.shared_user_memory.append_journal(content="观察", source="agent")
        with pytest.raises(ValidationError):
            await container.shared_user_memory.append_journal(content="   ")

    asyncio.run(scenario())


def test_service_dedupe_returns_existing_row(container: Container) -> None:
    async def scenario() -> None:
        first, created_first = await container.shared_user_memory.append_journal(
            content=" Same   Observation "
        )
        second, created_second = await container.shared_user_memory.append_journal(
            content="same observation"
        )
        assert created_first is True
        assert created_second is False
        assert first.id == second.id
        assert len(await container.shared_user_memory.list_journal()) == 1

    asyncio.run(scenario())


def test_render_block_respects_budget(container: Container) -> None:
    async def scenario() -> None:
        await container.shared_user_memory.put_profile({"notes": "n" * 2000})
        for index in range(12):
            await container.shared_user_memory.append_journal(
                content=f"observation-{uuid.uuid4()}-{index}-" + "x" * 350
            )
        block = await container.shared_user_memory.render_block()
        assert block
        assert len(block) <= BLOCK_MAX_CHARS
        # 共享块必须与 Bot 记忆区分开
        assert "用户共享记忆" in block

    asyncio.run(scenario())
