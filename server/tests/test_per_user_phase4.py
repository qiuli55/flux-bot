"""Phase 4：画像 / 日志与对话历史按 Flux 云用户隔离。

身份权威仍是 Flux 云用户 id（测试用 `tests.cloud_jwt` 签发假令牌）。
历史 NULL 归属只对 `FLUX_OWNER_CLOUD_USER_ID` 的主用户可见，与 Phase 2/3 一致。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flux.config import Settings
from flux.container import Container
from flux.core.bot.subtasks import run_subtask_tool
from flux.core.memory.shared_user import journal_content_hash
from flux.core.task_engine.assistant import AssistantTurn
from flux.errors import NotFoundError
from flux.main import create_app
from flux.models.shared_user_memory import UserJournalEntry, UserProfileEntry
from tests.cloud_jwt import mint_cloud_token

PREFIX = "/api/v1"
MEMORY_PREFIX = f"{PREFIX}/user-memory"
OWNER = "1"
USER_A = "2"
USER_B = "3"


def _auth(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_cloud_token(user)}"}


@contextmanager
def _client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        yield client


class _RecordingAssistant:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def respond(self, **kwargs: Any) -> AssistantTurn:
        self.calls.append(kwargs)
        return AssistantTurn(
            reply="echo",
            provider="fake",
            model="fake-model",
            usage={"input_tokens": 1, "output_tokens": 1},
        )


def _insert_legacy_profile(container: Container, key: str, value: str) -> None:
    async def _insert() -> None:
        async with container.session_factory() as session:
            session.add(UserProfileEntry(key=key, value=value, cloud_user_id=None))
            await session.commit()

    asyncio.run(_insert())


def _insert_legacy_journal(container: Container, content: str) -> str:
    async def _insert() -> str:
        async with container.session_factory() as session:
            row = UserJournalEntry(
                content=content,
                content_hash=journal_content_hash(content),
                source="user",
                cloud_user_id=None,
            )
            session.add(row)
            await session.commit()
            return str(row.id)

    return asyncio.run(_insert())


def _create_bot(client: TestClient, *, name: str = "隔离 Bot") -> dict[str, Any]:
    created = client.post(f"{PREFIX}/bots", json={"name": name, "description": "测试"})
    assert created.status_code == 200, created.text
    return created.json()["data"]


def _wait_turn(
    client: TestClient, bot_id: str, turn_id: str, *, user: str | None = None
) -> dict[str, Any]:
    response = client.get(
        f"{PREFIX}/bots/{bot_id}/turns/{turn_id}",
        params={"wait": 5},
        headers=_auth(user) if user is not None else None,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["turn"]


# --- 1. 画像 / 日志按人隔离 ---


def test_profile_and_journal_are_isolated_between_users(
    settings: Settings, db_schema: None
) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": OWNER})
    with _client(cfg) as client:
        written = client.put(
            f"{MEMORY_PREFIX}/profile", json={"name": "张三"}, headers=_auth(USER_A)
        )
        assert written.status_code == 200, written.text
        journal = client.post(
            f"{MEMORY_PREFIX}/journal",
            json={"content": "A 的观察"},
            headers=_auth(USER_A),
        )
        assert journal.status_code == 200, journal.text
        entry_id = journal.json()["data"]["entry"]["id"]

        # B 看不到 A 的画像 / 日志，也不能改 / 删（404，不泄露存在性）。
        assert (
            client.get(f"{MEMORY_PREFIX}/profile", headers=_auth(USER_B)).json()["data"]["profile"]
            == {}
        )
        assert client.get(f"{MEMORY_PREFIX}/journal", headers=_auth(USER_B)).json()["data"] == []
        assert (
            client.delete(f"{MEMORY_PREFIX}/profile/name", headers=_auth(USER_B)).status_code == 404
        )
        assert (
            client.delete(f"{MEMORY_PREFIX}/journal/{entry_id}", headers=_auth(USER_B)).status_code
            == 404
        )

        # A 自己仍看得到、删得掉。
        assert client.get(f"{MEMORY_PREFIX}/profile", headers=_auth(USER_A)).json()["data"][
            "profile"
        ] == {"name": "张三"}
        assert (
            client.delete(f"{MEMORY_PREFIX}/profile/name", headers=_auth(USER_A)).status_code == 200
        )


def test_same_profile_key_and_journal_text_can_exist_for_two_users(
    settings: Settings, db_schema: None
) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": OWNER})
    with _client(cfg) as client:
        for user in (USER_A, USER_B):
            put = client.put(
                f"{MEMORY_PREFIX}/profile",
                json={"name": f"用户{user}"},
                headers=_auth(user),
            )
            assert put.status_code == 200, put.text
            dup = client.post(
                f"{MEMORY_PREFIX}/journal",
                json={"content": "同一个观察"},
                headers=_auth(user),
            )
            assert dup.status_code == 200, dup.text
            assert dup.json()["data"]["created"] is True

        assert client.get(f"{MEMORY_PREFIX}/profile", headers=_auth(USER_A)).json()["data"][
            "profile"
        ] == {"name": "用户2"}
        assert client.get(f"{MEMORY_PREFIX}/profile", headers=_auth(USER_B)).json()["data"][
            "profile"
        ] == {"name": "用户3"}
        # 同一用户内重复文本仍然去重。
        again = client.post(
            f"{MEMORY_PREFIX}/journal",
            json={"content": "同一个观察"},
            headers=_auth(USER_A),
        )
        assert again.json()["data"]["created"] is False


def test_legacy_null_profile_and_journal_belong_to_owner_only(
    settings: Settings, db_schema: None, container: Container
) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": OWNER})
    _insert_legacy_profile(container, "notes", "历史画像")
    _insert_legacy_journal(container, "历史观察")

    with _client(cfg) as client:
        owner_profile = client.get(f"{MEMORY_PREFIX}/profile", headers=_auth(OWNER)).json()["data"][
            "profile"
        ]
        assert owner_profile == {"notes": "历史画像"}
        owner_journal = client.get(f"{MEMORY_PREFIX}/journal", headers=_auth(OWNER)).json()["data"]
        assert [row["content"] for row in owner_journal] == ["历史观察"]

        assert (
            client.get(f"{MEMORY_PREFIX}/profile", headers=_auth(USER_A)).json()["data"]["profile"]
            == {}
        )
        assert client.get(f"{MEMORY_PREFIX}/journal", headers=_auth(USER_A)).json()["data"] == []


def test_user_memory_requires_cloud_login(settings: Settings, db_schema: None) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": None})
    with _client(cfg) as client:
        for response in (
            client.get(MEMORY_PREFIX),
            client.get(f"{MEMORY_PREFIX}/profile"),
            client.put(f"{MEMORY_PREFIX}/profile", json={"name": "匿名"}),
            client.get(f"{MEMORY_PREFIX}/journal"),
            client.post(f"{MEMORY_PREFIX}/journal", json={"content": "匿名观察"}),
        ):
            assert response.status_code == 403, response.text
            assert response.json()["code"] == "cloud_login_required"


def test_render_block_only_contains_the_current_users_entries(
    settings: Settings, db_schema: None, container: Container
) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": OWNER})
    with _client(cfg) as client:
        client.put(f"{MEMORY_PREFIX}/profile", json={"name": "A 的画像"}, headers=_auth(USER_A))
        client.put(f"{MEMORY_PREFIX}/profile", json={"name": "B 的画像"}, headers=_auth(USER_B))
        client.post(f"{MEMORY_PREFIX}/journal", json={"content": "A 的日志"}, headers=_auth(USER_A))
        client.post(f"{MEMORY_PREFIX}/journal", json={"content": "B 的日志"}, headers=_auth(USER_B))

        block_a = asyncio.run(
            container.shared_user_memory.render_block(
                cloud_user_id=USER_A, owner_cloud_user_id=OWNER
            )
        )
        block_b = asyncio.run(
            container.shared_user_memory.render_block(
                cloud_user_id=USER_B, owner_cloud_user_id=OWNER
            )
        )
        assert "A 的画像" in block_a and "A 的日志" in block_a
        assert "B 的画像" not in block_a and "B 的日志" not in block_a
        assert "B 的画像" in block_b and "B 的日志" in block_b
        assert "A 的画像" not in block_b and "A 的日志" not in block_b


def test_bot_turn_context_uses_only_the_speakers_profile_and_shared_space(
    settings: Settings, db_schema: None
) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": OWNER})
    with _client(cfg) as client:
        container = client.app.state.container  # type: ignore[attr-defined]
        assistant = _RecordingAssistant()
        container.assistant = assistant  # type: ignore[assignment]
        bot = _create_bot(client)

        client.put(f"{MEMORY_PREFIX}/profile", json={"name": "A 的画像"}, headers=_auth(USER_A))
        client.put(f"{MEMORY_PREFIX}/profile", json={"name": "B 的画像"}, headers=_auth(USER_B))
        client.post(f"{MEMORY_PREFIX}/journal", json={"content": "A 的日志"}, headers=_auth(USER_A))
        client.post(f"{MEMORY_PREFIX}/journal", json={"content": "B 的日志"}, headers=_auth(USER_B))

        # A 建共享空间并邀请 B；A 写入的 shared 记忆对两人都可见。
        space = client.post(f"{PREFIX}/spaces", json={"name": "共同空间"}, headers=_auth(USER_A))
        assert space.status_code == 200, space.text
        space_id = space.json()["data"]["id"]
        added = client.post(
            f"{PREFIX}/spaces/{space_id}/members",
            json={"cloud_user_id": USER_B, "role": "member"},
            headers=_auth(USER_A),
        )
        assert added.status_code == 200, added.text
        shared = client.post(
            f"{PREFIX}/spaces/{space_id}/memories",
            json={"content": "两人的共享约定"},
            headers=_auth(USER_A),
        )
        assert shared.status_code == 200, shared.text

        created = client.post(
            f"{PREFIX}/bots/{bot['id']}/turns",
            json={"content": "你好"},
            headers=_auth(USER_A),
        )
        turn_id = created.json()["data"]["turn"]["id"]
        assert _wait_turn(client, bot["id"], turn_id, user=USER_A)["status"] == "completed"

        context = assistant.calls[-1]["context"] or ""
        assert "A 的画像" in context and "A 的日志" in context
        assert "两人的共享约定" in context
        assert "B 的画像" not in context and "B 的日志" not in context


# --- 2. 任务 / 对话按人隔离 ---


def _create_task(client: TestClient, *, user: str | None, description: str) -> dict[str, Any]:
    response = client.post(
        f"{PREFIX}/tasks",
        json={"description": description},
        headers=_auth(user) if user is not None else None,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _insert_legacy_task(container: Container, description: str) -> str:
    async def _insert() -> str:
        task = await container.task_repo.create(
            task_id=uuid.uuid4(),
            description=description,
            priority=100,
            project_id=None,
            agent_id=None,
            cloud_user_id=None,
        )
        return str(task.id)

    return asyncio.run(_insert())


def test_rest_tasks_are_stamped_and_listed_per_user(
    settings: Settings, db_schema: None, container: Container
) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": OWNER})
    with _client(cfg) as client:
        task_a = _create_task(client, user=USER_A, description="A 的任务")
        task_b = _create_task(client, user=USER_B, description="B 的任务")
        assert task_a["cloud_user_id"] == USER_A
        assert task_b["cloud_user_id"] == USER_B

        listed_a = client.get(f"{PREFIX}/tasks", headers=_auth(USER_A)).json()["data"]
        listed_b = client.get(f"{PREFIX}/tasks", headers=_auth(USER_B)).json()["data"]
        assert {row["id"] for row in listed_a} == {task_a["id"]}
        assert {row["id"] for row in listed_b} == {task_b["id"]}

        # B 的任务对 A 一律按不存在处理，不返回正文 / 事件 / 消息。
        for method, path, body in (
            ("get", f"{PREFIX}/tasks/{task_b['id']}", None),
            ("get", f"{PREFIX}/tasks/{task_b['id']}/messages", None),
            ("get", f"{PREFIX}/tasks/{task_b['id']}/events", None),
            ("get", f"{PREFIX}/tasks/{task_b['id']}/stream", None),
            ("post", f"{PREFIX}/tasks/{task_b['id']}/continue", {"content": "续"}),
            ("post", f"{PREFIX}/tasks/{task_b['id']}/cancel", None),
            ("delete", f"{PREFIX}/tasks/{task_b['id']}", None),
        ):
            response = (
                getattr(client, method)(path, json=body, headers=_auth(USER_A))
                if body is not None
                else getattr(client, method)(path, headers=_auth(USER_A))
            )
            assert response.status_code == 404, (path, response.text)

        # 自己的任务仍然正常可读。
        assert (
            client.get(f"{PREFIX}/tasks/{task_a['id']}", headers=_auth(USER_A)).status_code == 200
        )


def test_legacy_null_task_is_visible_only_to_owner(
    settings: Settings, db_schema: None, container: Container
) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": OWNER})
    legacy_id = _insert_legacy_task(container, "历史任务")
    with _client(cfg) as client:
        owner_list = client.get(f"{PREFIX}/tasks", headers=_auth(OWNER)).json()["data"]
        assert legacy_id in {row["id"] for row in owner_list}
        assert client.get(f"{PREFIX}/tasks/{legacy_id}", headers=_auth(OWNER)).status_code == 200

        assert client.get(f"{PREFIX}/tasks", headers=_auth(USER_A)).json()["data"] == []
        assert client.get(f"{PREFIX}/tasks/{legacy_id}", headers=_auth(USER_A)).status_code == 404
        assert (
            client.delete(f"{PREFIX}/tasks/{legacy_id}", headers=_auth(USER_A)).status_code == 404
        )


def test_task_create_without_person_is_rejected(settings: Settings, db_schema: None) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": None})
    with _client(cfg) as client:
        response = client.post(f"{PREFIX}/tasks", json={"description": "匿名任务"})
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "cloud_login_required"


def _send_bot_turn(client: TestClient, bot_id: str, *, user: str, content: str) -> str:
    created = client.post(
        f"{PREFIX}/bots/{bot_id}/turns",
        json={"content": content},
        headers=_auth(user),
    )
    assert created.status_code == 202, created.text
    return str(created.json()["data"]["turn"]["id"])


def test_bot_history_turns_and_chat_tasks_are_per_user(settings: Settings, db_schema: None) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": OWNER})
    with _client(cfg) as client:
        container = client.app.state.container  # type: ignore[attr-defined]
        assistant = _RecordingAssistant()
        container.assistant = assistant  # type: ignore[assignment]
        bot = _create_bot(client)

        turn_a = _send_bot_turn(client, bot["id"], user=USER_A, content="A 的第一句")
        assert _wait_turn(client, bot["id"], turn_a, user=USER_A)["status"] == "completed"
        turn_b = _send_bot_turn(client, bot["id"], user=USER_B, content="B 的第一句")
        assert _wait_turn(client, bot["id"], turn_b, user=USER_B)["status"] == "completed"

        # 轮次列表按人隔离。
        turns_a = client.get(f"{PREFIX}/bots/{bot['id']}/turns", headers=_auth(USER_A)).json()[
            "data"
        ]
        turns_b = client.get(f"{PREFIX}/bots/{bot['id']}/turns", headers=_auth(USER_B)).json()[
            "data"
        ]
        assert [row["id"] for row in turns_a] == [turn_a]
        assert [row["id"] for row in turns_b] == [turn_b]
        # 知道对方的 turn_id 也读不到 / 不能订阅流。
        assert (
            client.get(
                f"{PREFIX}/bots/{bot['id']}/turns/{turn_b}", headers=_auth(USER_A)
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"{PREFIX}/bots/{bot['id']}/turns/{turn_b}/stream", headers=_auth(USER_A)
            ).status_code
            == 404
        )

        # 每个人的 chat_task_id 都是自己的，且互不相同。
        payload_a = client.get(f"{PREFIX}/bots/{bot['id']}", headers=_auth(USER_A)).json()["data"]
        payload_b = client.get(f"{PREFIX}/bots/{bot['id']}", headers=_auth(USER_B)).json()["data"]
        assert payload_a["chat_task_id"]
        assert payload_b["chat_task_id"]
        assert payload_a["chat_task_id"] != payload_b["chat_task_id"]

        # 会话消息也只包含自己的那一句。
        conversation_a = client.get(
            f"{PREFIX}/bots/{bot['id']}/conversation", headers=_auth(USER_A)
        ).json()["data"]
        conversation_b = client.get(
            f"{PREFIX}/bots/{bot['id']}/conversation", headers=_auth(USER_B)
        ).json()["data"]
        assert conversation_a["chat_task_id"] == payload_a["chat_task_id"]
        assert conversation_b["chat_task_id"] == payload_b["chat_task_id"]
        assert [m["content"] for m in conversation_a["messages"] if m["role"] == "user"] == [
            "A 的第一句"
        ]
        assert [m["content"] for m in conversation_b["messages"] if m["role"] == "user"] == [
            "B 的第一句"
        ]

        # 两个用户的消息落在不同的 tasks 行里。
        messages_a = client.get(
            f"{PREFIX}/tasks/{payload_a['chat_task_id']}/messages", headers=_auth(USER_A)
        ).json()["data"]
        messages_b = client.get(
            f"{PREFIX}/tasks/{payload_b['chat_task_id']}/messages", headers=_auth(USER_B)
        ).json()["data"]
        assert [m["content"] for m in messages_a if m["role"] == "user"] == ["A 的第一句"]
        assert [m["content"] for m in messages_b if m["role"] == "user"] == ["B 的第一句"]


def test_im_bound_service_turns_do_not_share_one_chat_task(
    settings: Settings, db_schema: None
) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": OWNER})
    with _client(cfg) as client:
        container = client.app.state.container  # type: ignore[attr-defined]
        container.assistant = _RecordingAssistant()  # type: ignore[assignment]
        bot = _create_bot(client, name="绑定隔离 Bot")
        bot_obj = asyncio.run(container.bots.get(uuid.UUID(bot["id"])))

        async def _create(user: str) -> Any:
            turn, created = await container.bot_conversation.create_turn(
                bot_obj, content=f"{user} 的 IM 消息", cloud_user_id=user
            )
            assert created
            return turn

        turn_a = asyncio.run(_create(USER_A))
        turn_b = asyncio.run(_create(USER_B))

        # 两条 IM 消息落在不同人的对话任务里，绝不共用 bots.chat_task_id。
        assert turn_a.task_id != turn_b.task_id
        assert turn_a.cloud_user_id == USER_A and turn_b.cloud_user_id == USER_B

        async def _owned(task_id: Any, user: str) -> Any:
            return await container.task_repo.get_owned(
                task_id, cloud_user_id=user, owner_cloud_user_id=OWNER
            )

        assert asyncio.run(_owned(turn_a.task_id, USER_A)).cloud_user_id == USER_A
        assert asyncio.run(_owned(turn_b.task_id, USER_B)).cloud_user_id == USER_B
        with pytest.raises(NotFoundError):
            asyncio.run(_owned(turn_b.task_id, USER_A))


def test_subtask_parents_to_the_speakers_chat_task(
    settings: Settings, db_schema: None, monkeypatch: Any
) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": OWNER})
    with _client(cfg) as client:
        container = client.app.state.container  # type: ignore[attr-defined]
        container.assistant = _RecordingAssistant()  # type: ignore[assignment]
        bot = _create_bot(client, name="子任务隔离 Bot")

        turn_a = _send_bot_turn(client, bot["id"], user=USER_A, content="A 先说话")
        assert _wait_turn(client, bot["id"], turn_a, user=USER_A)["status"] == "completed"
        turn_b = _send_bot_turn(client, bot["id"], user=USER_B, content="B 先说话")
        assert _wait_turn(client, bot["id"], turn_b, user=USER_B)["status"] == "completed"

        payload_a = client.get(f"{PREFIX}/bots/{bot['id']}", headers=_auth(USER_A)).json()["data"]
        payload_b = client.get(f"{PREFIX}/bots/{bot['id']}", headers=_auth(USER_B)).json()["data"]
        assert payload_a["chat_task_id"] != payload_b["chat_task_id"]

        # 不让子任务真的去起内核 Run：只验证归属与父子关系。
        async def _noop(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(container.bot_subtasks, "_run", _noop)
        bot_obj = asyncio.run(container.bots.get(uuid.UUID(bot["id"])))
        started = asyncio.run(
            run_subtask_tool(
                container.bot_subtasks,
                bot_obj,
                "subtask_start",
                {"instruction": "帮 A 整理资料"},
                cloud_user_id=USER_A,
                owner_cloud_user_id=OWNER,
            )
        )
        subtask_id = started["subtask_id"]
        subtask = asyncio.run(container.task_repo.get(uuid.UUID(subtask_id)))
        assert str(subtask.parent_task_id) == payload_a["chat_task_id"]
        assert subtask.cloud_user_id == USER_A

        # A 能看自己的子任务，B 看不到（404）。
        assert (
            client.get(
                f"{PREFIX}/bots/{bot['id']}/subtasks/{subtask_id}", headers=_auth(USER_A)
            ).status_code
            == 200
        )
        assert (
            client.get(
                f"{PREFIX}/bots/{bot['id']}/subtasks/{subtask_id}", headers=_auth(USER_B)
            ).status_code
            == 404
        )
