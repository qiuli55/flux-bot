"""Phase 4：画像 / 日志与对话历史按 Flux 云用户隔离。

身份权威仍是 Flux 云用户 id（测试用 `tests.cloud_jwt` 签发假令牌）。
历史 NULL 归属只对 `FLUX_OWNER_CLOUD_USER_ID` 的主用户可见，与 Phase 2/3 一致。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient

from flux.config import Settings
from flux.container import Container
from flux.core.memory.shared_user import journal_content_hash
from flux.core.task_engine.assistant import AssistantTurn
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


def _wait_turn(client: TestClient, bot_id: str, turn_id: str) -> dict[str, Any]:
    response = client.get(f"{PREFIX}/bots/{bot_id}/turns/{turn_id}", params={"wait": 5})
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
        assert _wait_turn(client, bot["id"], turn_id)["status"] == "completed"

        context = assistant.calls[-1]["context"] or ""
        assert "A 的画像" in context and "A 的日志" in context
        assert "两人的共享约定" in context
        assert "B 的画像" not in context and "B 的日志" not in context


# --- 对话 / 任务隔离在后续增量补充 ---
