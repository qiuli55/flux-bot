"""Phase 3：IM 账号绑定、按绑定人格运行，以及共享空间 / shared 记忆。

身份权威仍然是 Flux 云用户 id（测试用 `tests.cloud_jwt` 签发假令牌）。历史 NULL
归属只对 `FLUX_OWNER_CLOUD_USER_ID` 的主用户可见，与 Phase 2 一致。
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from flux.config import Settings
from flux.container import Container
from flux.core.task_engine.assistant import AssistantTurn
from flux.db.session import create_engine, create_session_factory
from flux.main import create_app
from flux.models.channel_binding import ChannelBindCode
from flux.models.github import GithubConnection
from tests.cloud_jwt import mint_cloud_token
from tests.test_dingtalk_channel import (
    APP_KEY as DINGTALK_APP_KEY,
)
from tests.test_dingtalk_channel import (
    STAFF_ID as DINGTALK_STAFF_ID,
)
from tests.test_dingtalk_channel import (
    WEBHOOK as DINGTALK_WEBHOOK,
)
from tests.test_dingtalk_channel import (
    _dingtalk_settings,
    _encrypted_request,
    _message_payload,
)
from tests.test_feishu_channel import (
    VERIFY_TOKEN as FEISHU_VERIFY_TOKEN,
)
from tests.test_feishu_channel import (
    WEBHOOK as FEISHU_WEBHOOK,
)
from tests.test_feishu_channel import (
    _plain_body as _feishu_body,
)
from tests.test_wecom_channel import (
    CORP_ID as WECOM_CORP_ID,
)
from tests.test_wecom_channel import (
    ENCODING_AES_KEY as WECOM_ENCODING_KEY,
)
from tests.test_wecom_channel import (
    TOKEN as WECOM_TOKEN,
)
from tests.test_wecom_channel import (
    USER_ID as WECOM_USER_ID,
)
from tests.test_wecom_channel import (
    WEBHOOK as WECOM_WEBHOOK,
)
from tests.test_wecom_channel import (
    _encrypted_body,
    _signed_query,
    _text_message_xml,
)

PREFIX = "/api/v1"
OWNER = "1"
USER_A = "2"
USER_B = "3"
USER_C = "4"


def _auth(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_cloud_token(user)}"}


@contextmanager
def _client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        yield client


def _create_bot(client: TestClient, *, name: str = "绑定测试 Bot") -> dict[str, Any]:
    created = client.post(f"{PREFIX}/bots", json={"name": name, "description": "测试"})
    assert created.status_code == 200, created.text
    return created.json()["data"]


def _issue_code(client: TestClient, user: str) -> str:
    response = client.post(f"{PREFIX}/channel-bindings/codes", headers=_auth(user))
    assert response.status_code == 200, response.text
    return response.json()["data"]["code"]


def _bind_code_hashes(settings: Settings) -> list[str]:
    async def _read() -> list[str]:
        engine = create_engine(settings.database_url)
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                return list(await session.scalars(select(ChannelBindCode.code_hash)))
        finally:
            await engine.dispose()

    return asyncio.run(_read())


def _insert_github(container: Container, account_key: str, login: str) -> None:
    async def _insert() -> None:
        async with container.session_factory() as session:
            session.add(
                GithubConnection(
                    account_key=account_key,
                    login=login,
                    name=login,
                    avatar_url="",
                    token_ciphertext=container.key_vault.seal_text("gho_test_token"),
                    scopes="repo",
                    token_kind="bearer",
                )
            )
            await session.commit()

    asyncio.run(_insert())


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


class _FakeFeishuSender:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send_text(self, chat_id: str, text: str) -> None:
        self.messages.append((chat_id, text))

    async def aclose(self) -> None:
        return None


class _FakeWeComSender:
    def __init__(self) -> None:
        self.messages: list[tuple[dict[str, Any], str]] = []

    async def send_text(self, dest: dict[str, Any], text: str) -> None:
        self.messages.append((dict(dest), text))

    async def aclose(self) -> None:
        return None


class _FakeDingTalkSender:
    def __init__(self) -> None:
        self.messages: list[tuple[dict[str, Any], str]] = []

    async def send_text(self, dest: dict[str, Any], text: str) -> None:
        self.messages.append((dict(dest), text))

    async def aclose(self) -> None:
        return None


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


# --- 1. 三个频道的绑定码 ---


def test_feishu_bind_code_binds_sender_without_creating_turn(
    settings: Settings, db_schema: None
) -> None:
    cfg = settings.model_copy(update={"feishu_verification_token": FEISHU_VERIFY_TOKEN})
    with _client(cfg) as client:
        container = client.app.state.container  # type: ignore[attr-defined]
        bot = _create_bot(client)
        container.feishu_bot._bot_id = uuid.UUID(bot["id"])  # noqa: SLF001
        sender = _FakeFeishuSender()
        container.feishu_bot._sender = sender  # noqa: SLF001

        code = _issue_code(client, USER_B)
        response = client.post(
            FEISHU_WEBHOOK,
            json=_feishu_body(text=code, message_id="om_bind_feishu"),
        )
        assert response.status_code == 200, response.text
        assert sender.messages and "绑定成功" in sender.messages[-1][1]

        binding = asyncio.run(container.channel_bindings.get_binding("feishu", "ou_test"))
        assert binding is not None and binding.cloud_user_id == USER_B
        # 绑定消息不是一轮对话
        assert asyncio.run(container.bot_conversation.list_turns(uuid.UUID(bot["id"]))) == []
        # 明文不落库：库里只有 code 的大写 sha256
        assert _bind_code_hashes(cfg) == [hashlib.sha256(code.upper().encode()).hexdigest()]


def test_wecom_bind_code_binds_from_user(settings: Settings, db_schema: None) -> None:
    cfg = settings.model_copy(
        update={
            "wecom_corp_id": WECOM_CORP_ID,
            "wecom_token": WECOM_TOKEN,
            "wecom_encoding_aes_key": WECOM_ENCODING_KEY,
        }
    )
    with _client(cfg) as client:
        container = client.app.state.container  # type: ignore[attr-defined]
        bot = _create_bot(client)
        container.wecom_bot._bot_id = uuid.UUID(bot["id"])  # noqa: SLF001
        sender = _FakeWeComSender()
        container.wecom_bot._sender = sender  # noqa: SLF001

        code = _issue_code(client, USER_A)
        inner = _text_message_xml(content=code, msg_id="wecom_bind_1")
        encrypted, raw = _encrypted_body(inner)
        response = client.post(
            WECOM_WEBHOOK,
            params=_signed_query(encrypted),
            content=raw,
            headers={"Content-Type": "application/xml"},
        )
        assert response.status_code == 200, response.text
        assert sender.messages and "绑定成功" in sender.messages[-1][1]

        binding = asyncio.run(container.channel_bindings.get_binding("wecom", WECOM_USER_ID))
        assert binding is not None and binding.cloud_user_id == USER_A
        assert asyncio.run(container.bot_conversation.list_turns(uuid.UUID(bot["id"]))) == []


def test_dingtalk_bind_code_uses_staff_id(settings: Settings, db_schema: None) -> None:
    cfg = _dingtalk_settings(settings)
    with _client(cfg) as client:
        container = client.app.state.container  # type: ignore[attr-defined]
        bot = _create_bot(client)
        container.dingtalk_bot._bot_id = uuid.UUID(bot["id"])  # noqa: SLF001
        sender = _FakeDingTalkSender()
        container.dingtalk_bot._sender = sender  # noqa: SLF001

        code = _issue_code(client, USER_B)
        payload = _message_payload(msg_id="ding_bind_1", text=code, robot_code=DINGTALK_APP_KEY)
        query, raw = _encrypted_request(payload)
        response = client.post(
            DINGTALK_WEBHOOK,
            params=query,
            content=raw,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200, response.text
        assert sender.messages and "绑定成功" in sender.messages[-1][1]

        binding = asyncio.run(container.channel_bindings.get_binding("dingtalk", DINGTALK_STAFF_ID))
        assert binding is not None and binding.cloud_user_id == USER_B
        assert asyncio.run(container.bot_conversation.list_turns(uuid.UUID(bot["id"]))) == []


def test_invalid_expired_and_reused_codes_do_not_bind(settings: Settings, db_schema: None) -> None:
    cfg = settings.model_copy(update={"feishu_verification_token": FEISHU_VERIFY_TOKEN})
    with _client(cfg) as client:
        container = client.app.state.container  # type: ignore[attr-defined]
        bot = _create_bot(client)
        container.feishu_bot._bot_id = uuid.UUID(bot["id"])  # noqa: SLF001
        sender = _FakeFeishuSender()
        container.feishu_bot._sender = sender  # noqa: SLF001

        # 未绑定的普通消息：回中文绑定指引，不建 turn。
        response = client.post(
            FEISHU_WEBHOOK, json=_feishu_body(text="你好", message_id="om_unbound")
        )
        assert response.status_code == 200
        assert "还没有绑定 Flux 账号" in sender.messages[-1][1]
        assert asyncio.run(container.bot_conversation.list_turns(uuid.UUID(bot["id"]))) == []

        # 形状正确但未知的码
        response = client.post(
            FEISHU_WEBHOOK,
            json=_feishu_body(text="ZZZZZZZZ", message_id="om_wrong"),
        )
        assert response.status_code == 200
        assert "绑定码无效" in sender.messages[-1][1]
        assert asyncio.run(container.channel_bindings.get_binding("feishu", "ou_test")) is None

        # 过期码：手动把 expires_at 改到过去
        expired = _issue_code(client, USER_A)

        async def _expire() -> None:
            async with container.session_factory() as session:
                row = await session.scalar(
                    select(ChannelBindCode).where(
                        ChannelBindCode.code_hash
                        == hashlib.sha256(expired.upper().encode()).hexdigest()
                    )
                )
                assert row is not None
                row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                await session.commit()

        asyncio.run(_expire())
        response = client.post(
            FEISHU_WEBHOOK,
            json=_feishu_body(text=expired, message_id="om_expired"),
        )
        assert response.status_code == 200
        assert "绑定码无效" in sender.messages[-1][1]
        assert asyncio.run(container.channel_bindings.get_binding("feishu", "ou_test")) is None

        # 成功绑定后再用同一个码（单次有效）
        code = _issue_code(client, USER_B)
        body = _feishu_body(text=code, message_id="om_reuse_1")
        assert client.post(FEISHU_WEBHOOK, json=body).status_code == 200
        assert (
            asyncio.run(container.channel_bindings.get_binding("feishu", "ou_test")).cloud_user_id
            == USER_B
        )
        # 另一个外部用户拿已用的码来兑换：不会绑到他身上
        reused = _feishu_body(text=code, message_id="om_reuse_2")
        reused["event"]["sender"]["sender_id"]["open_id"] = "ou_other"
        assert client.post(FEISHU_WEBHOOK, json=reused).status_code == 200
        assert asyncio.run(container.channel_bindings.get_binding("feishu", "ou_other")) is None


def test_missing_sender_id_does_not_run_as_owner(settings: Settings, db_schema: None) -> None:
    cfg = settings.model_copy(update={"feishu_verification_token": FEISHU_VERIFY_TOKEN})
    with _client(cfg) as client:
        container = client.app.state.container  # type: ignore[attr-defined]
        bot = _create_bot(client)
        container.feishu_bot._bot_id = uuid.UUID(bot["id"])  # noqa: SLF001
        sender = _FakeFeishuSender()
        container.feishu_bot._sender = sender  # noqa: SLF001

        body = _feishu_body(text="你好", message_id="om_no_sender")
        body["event"].pop("sender", None)
        response = client.post(FEISHU_WEBHOOK, json=body)
        assert response.status_code == 200
        assert "没有带可识别的发送者" in sender.messages[-1][1]
        assert asyncio.run(container.bot_conversation.list_turns(uuid.UUID(bot["id"]))) == []


# --- 2. 绑定后按该用户的人格对话 ---


def test_bound_sender_runs_turn_as_that_user(settings: Settings, db_schema: None) -> None:
    cfg = settings.model_copy(update={"feishu_verification_token": FEISHU_VERIFY_TOKEN})
    with _client(cfg) as client:
        container = client.app.state.container  # type: ignore[attr-defined]
        bot = _create_bot(client)
        bot_id = uuid.UUID(bot["id"])
        container.feishu_bot._bot_id = bot_id  # noqa: SLF001
        sender = _FakeFeishuSender()
        container.feishu_bot._sender = sender  # noqa: SLF001
        assistant = _RecordingAssistant()
        container.assistant = assistant  # type: ignore[assignment]

        # A 绑定了飞书；A / B / 主用户各有自己的 User 记忆。
        for user, content in (
            (USER_A, "A 的私有偏好"),
            (USER_B, "B 的私有偏好"),
            (OWNER, "主用户的私有偏好"),
        ):
            written = client.post(
                f"{PREFIX}/memory/entries",
                json={"content": content},
                headers=_auth(user),
            )
            assert written.status_code == 200, written.text
        # 只有 A 连接了 GitHub；B / 主用户没有。
        _insert_github(container, USER_A, "user-a")
        asyncio.run(
            container.channel_bindings.bind(
                channel="feishu", external_user_id="ou_test", cloud_user_id=USER_A
            )
        )

        response = client.post(
            FEISHU_WEBHOOK, json=_feishu_body(text="你好", message_id="om_run_a")
        )
        assert response.status_code == 200, response.text
        turns = asyncio.run(container.bot_conversation.list_turns(bot_id))
        assert len(turns) == 1 and turns[0].cloud_user_id == USER_A
        done = _wait_turn(client, bot["id"], str(turns[0].id), user=USER_A)
        assert done["status"] == "completed", done

        call = assistant.calls[-1]
        context = str(call.get("context") or "")
        assert "A 的私有偏好" in context
        assert "B 的私有偏好" not in context
        assert "主用户的私有偏好" not in context
        # GitHub 工具按 A 的连接状态装配，而不是主用户兜底。
        assert call.get("tools")
        assert "没有连接 GitHub" not in str(call.get("tools_note") or "")


# --- 3. 绑定关系的按人可见性 ---


def test_bindings_are_scoped_per_user(settings: Settings, db_schema: None) -> None:
    with _client(settings) as client:
        container = client.app.state.container  # type: ignore[attr-defined]
        a = asyncio.run(
            container.channel_bindings.bind(
                channel="feishu", external_user_id="ou_a", cloud_user_id=USER_A
            )
        )
        asyncio.run(
            container.channel_bindings.bind(
                channel="wecom", external_user_id="wecom_b", cloud_user_id=USER_B
            )
        )

        listed_a = client.get(f"{PREFIX}/channel-bindings", headers=_auth(USER_A)).json()["data"]
        assert {row["external_user_id"] for row in listed_a} == {"ou_a"}
        listed_b = client.get(f"{PREFIX}/channel-bindings", headers=_auth(USER_B)).json()["data"]
        assert {row["external_user_id"] for row in listed_b} == {"wecom_b"}

        # A 不能删 B 的绑定（404），也不能删完别人的。
        denied = client.delete(
            f"{PREFIX}/channel-bindings/{listed_b[0]['id']}", headers=_auth(USER_A)
        )
        assert denied.status_code == 404
        allowed = client.delete(f"{PREFIX}/channel-bindings/{a.id}", headers=_auth(USER_A))
        assert allowed.status_code == 200
        assert asyncio.run(container.channel_bindings.get_binding("feishu", "ou_a")) is None


# --- 4. 例程按人运行 ---


def test_routine_runs_with_stamped_user_memory(
    settings: Settings, db_schema: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(settings) as client:
        container = client.app.state.container  # type: ignore[attr-defined]
        bot = _create_bot(client)
        for user, content in ((USER_A, "A 的例程记忆"), (USER_B, "B 的例程记忆")):
            client.post(f"{PREFIX}/memory/entries", json={"content": content}, headers=_auth(user))

        created = client.post(
            f"{PREFIX}/bots/{bot['id']}/routines",
            json={"name": "A 的例程", "instruction": "每天检查", "run_at": "2099-01-01T00:00:00Z"},
            headers=_auth(USER_A),
        )
        assert created.status_code == 200, created.text
        routine = created.json()["data"]
        assert routine["cloud_user_id"] == USER_A

        started: list[Any] = []

        async def _start(task: Any) -> None:
            started.append(task)

        monkeypatch.setattr(container.task_run_service, "start", _start)
        row = asyncio.run(container.bots.get_routine(uuid.UUID(routine["id"])))
        task = asyncio.run(
            container.bots._tick_routine(  # noqa: SLF001 - 直接驱动一次到期例程
                row, datetime.now(timezone.utc)
            )
        )
        assert task is not None and started and started[0].id == task.id
        assert task.cloud_user_id == USER_A

        preview = asyncio.run(container.task_run_service.preview_instruction(task))
        assert "A 的例程记忆" in preview
        assert "B 的例程记忆" not in preview


# --- 5. 出站草稿按人隔离 ---


def _create_draft(container: Container, body: str, cloud_user_id: str | None) -> Any:
    return asyncio.run(
        container.outbound_drafts.create(channel="feishu", body=body, cloud_user_id=cloud_user_id)
    )


def test_outbound_drafts_are_scoped_per_user(settings: Settings, db_schema: None) -> None:
    with _client(settings) as client:
        container = client.app.state.container  # type: ignore[attr-defined]
        draft_a = _create_draft(container, "A 的回复", USER_A)
        draft_b = _create_draft(container, "B 的回复", USER_B)
        legacy = _create_draft(container, "历史回复", None)

        list_a = client.get(f"{PREFIX}/outbound-drafts", headers=_auth(USER_A)).json()["data"]
        assert {row["id"] for row in list_a} == {str(draft_a.id)}
        list_b = client.get(f"{PREFIX}/outbound-drafts", headers=_auth(USER_B)).json()["data"]
        assert {row["id"] for row in list_b} == {str(draft_b.id)}
        list_owner = client.get(f"{PREFIX}/outbound-drafts", headers=_auth(OWNER)).json()["data"]
        assert {row["id"] for row in list_owner} == {str(legacy.id)}

        # 看不到正文、也不能取消别人的草稿
        assert (
            client.get(f"{PREFIX}/outbound-drafts/{draft_b.id}", headers=_auth(USER_A)).status_code
            == 404
        )
        assert (
            client.post(
                f"{PREFIX}/outbound-drafts/{draft_b.id}/cancel", headers=_auth(USER_A)
            ).status_code
            == 404
        )


def test_outbound_decision_cannot_be_resolved_by_another_user(
    settings: Settings, db_schema: None
) -> None:
    with _client(settings) as client:
        container = client.app.state.container  # type: ignore[attr-defined]
        draft = _create_draft(container, "A 的回复", USER_A)

        async def _task_with_decision() -> Any:
            task = await container.task_repo.create(
                task_id=uuid.uuid4(),
                description="outbound",
                priority=100,
                project_id=None,
                agent_id=None,
                cloud_user_id=USER_A,
            )
            return await container.task_repo.raise_decision(
                task.id,
                {
                    "id": "decision-1",
                    "kind": "feishu_outbound",
                    "status": "pending",
                    "meta": {"draft_id": str(draft.id)},
                    "options": [{"label": "allow"}, {"label": "deny"}],
                },
            )

        task = asyncio.run(_task_with_decision())
        payload = {"decision_id": "decision-1", "action": "reject", "note": "不发送"}
        denied = client.post(
            f"{PREFIX}/tasks/{task.id}/decisions/choose",
            json=payload,
            headers=_auth(USER_B),
        )
        assert denied.status_code == 404
        allowed = client.post(
            f"{PREFIX}/tasks/{task.id}/decisions/choose",
            json=payload,
            headers=_auth(USER_A),
        )
        assert allowed.status_code == 200, allowed.text


# --- 6. 共享空间与 shared 记忆 ---


def _create_space(client: TestClient, user: str, name: str) -> dict[str, Any]:
    response = client.post(f"{PREFIX}/spaces", json={"name": name}, headers=_auth(user))
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_shared_space_membership_and_shared_memory(settings: Settings, db_schema: None) -> None:
    with _client(settings) as client:
        space = _create_space(client, USER_A, "我们俩")
        added = client.post(
            f"{PREFIX}/spaces/{space['id']}/members",
            json={"cloud_user_id": USER_B, "role": "member"},
            headers=_auth(USER_A),
        )
        assert added.status_code == 200, added.text

        # 成员可以写入并读取 shared 记忆
        written = client.post(
            f"{PREFIX}/spaces/{space['id']}/memories",
            json={"content": "周末一起整理磁盘"},
            headers=_auth(USER_B),
        )
        assert written.status_code == 200, written.text
        entries = client.get(
            f"{PREFIX}/spaces/{space['id']}/memories", headers=_auth(USER_A)
        ).json()["data"]
        assert [entry["content"] for entry in entries] == ["周末一起整理磁盘"]

        # recall 对成员可见；非成员看不到（也不泄露空间名）
        recall_a = client.get(f"{PREFIX}/memory", headers=_auth(USER_A)).json()["data"]["content"]
        assert "周末一起整理磁盘" in recall_a
        recall_c = client.get(f"{PREFIX}/memory", headers=_auth(USER_C)).json()["data"]["content"]
        assert "周末一起整理磁盘" not in recall_c
        assert "我们俩" not in recall_c

        # 非成员：读空间 / 写记忆 / 看成员 一律 404
        assert (
            client.get(f"{PREFIX}/spaces/{space['id']}", headers=_auth(USER_C)).status_code == 404
        )
        assert (
            client.post(
                f"{PREFIX}/spaces/{space['id']}/memories",
                json={"content": "不应写入"},
                headers=_auth(USER_C),
            ).status_code
            == 404
        )
        assert (
            client.get(f"{PREFIX}/spaces/{space['id']}/members", headers=_auth(USER_C)).status_code
            == 404
        )

        # 只有 owner 能管理成员
        assert (
            client.post(
                f"{PREFIX}/spaces/{space['id']}/members",
                json={"cloud_user_id": USER_C, "role": "member"},
                headers=_auth(USER_B),
            ).status_code
            == 403
        )
        # 不允许把空间留成没有 owner
        assert (
            client.delete(
                f"{PREFIX}/spaces/{space['id']}/members/{USER_A}",
                headers=_auth(USER_A),
            ).status_code
            == 409
        )


def test_shared_and_user_layers_stay_isolated(settings: Settings, db_schema: None) -> None:
    with _client(settings) as client:
        space = _create_space(client, USER_A, "共享区")
        client.post(
            f"{PREFIX}/spaces/{space['id']}/members",
            json={"cloud_user_id": USER_B, "role": "member"},
            headers=_auth(USER_A),
        )
        client.post(
            f"{PREFIX}/spaces/{space['id']}/memories",
            json={"content": "共享空间里的约定"},
            headers=_auth(USER_A),
        )
        client.post(
            f"{PREFIX}/memory/entries",
            json={"content": "A 的 User 层隐私"},
            headers=_auth(USER_A),
        )
        client.post(
            f"{PREFIX}/memory/entries",
            json={"content": "B 的 User 层隐私"},
            headers=_auth(USER_B),
        )

        user_a = client.get(
            f"{PREFIX}/memory/entries", params={"layer": "user"}, headers=_auth(USER_A)
        ).json()["data"]
        assert [entry["content"] for entry in user_a] == ["A 的 User 层隐私"]
        # shared 记忆不会混进 User 层
        assert all(entry["content"] != "共享空间里的约定" for entry in user_a)

        shared_b = client.get(
            f"{PREFIX}/spaces/{space['id']}/memories", headers=_auth(USER_B)
        ).json()["data"]
        assert [entry["content"] for entry in shared_b] == ["共享空间里的约定"]
        # User 层记忆也不会混进 shared 层
        assert all("隐私" not in entry["content"] for entry in shared_b)

        # A / B 的 recall 各自只带自己的 User 记忆 + 共同 shared。
        recall_a = client.get(f"{PREFIX}/memory", headers=_auth(USER_A)).json()["data"]["content"]
        recall_b = client.get(f"{PREFIX}/memory", headers=_auth(USER_B)).json()["data"]["content"]
        assert "A 的 User 层隐私" in recall_a and "B 的 User 层隐私" not in recall_a
        assert "B 的 User 层隐私" in recall_b and "A 的 User 层隐私" not in recall_b
        assert "共享空间里的约定" in recall_a and "共享空间里的约定" in recall_b


def test_shared_space_api_requires_cloud_login(settings: Settings, db_schema: None) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": None})
    with _client(cfg) as client:
        response = client.post(f"{PREFIX}/spaces", json={"name": "无主空间"})
        assert response.status_code == 403
        assert response.json()["code"] == "cloud_login_required"
