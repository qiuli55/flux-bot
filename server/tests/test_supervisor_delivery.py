"""Supervisor（助理）P3：飞书回调、订阅直通与报告投递。

P3 的验收标准：

1. 飞书回调全部 fail-closed：未配置 token / token 不匹配 / 加密回调一律拒绝，
   同一条消息重复推送只落一行 trigger；
2. 外部线程的工单自动订阅回原会话，报告落库后经 outbox 只投递一次；
3. 投递策略按订阅的 mode 生效，未知策略不投；通道全部失败必须抛错给 outbox 重试，
   绝不静默丢弃；
4. `/reports`、`/timeline` 与 MCP 的四个只读工具只回放平台已记录的事实。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from flux.config import Settings
from flux.container import Container
from flux.core.supervisor.delivery import SupervisorNotifier, should_deliver
from flux.core.supervisor.outbox import normalize_dedupe_key
from flux.enums import ReportStatus
from flux.main import create_app

from .conftest import issue_token

PREFIX = "/api/v1/supervisor"
WEBHOOK = f"{PREFIX}/channels/feishu"
VERIFY_TOKEN = "verify-token-for-tests"


# --- 工具 ---


def _event(**overrides) -> dict:
    payload = {
        "source_key": "feishu",
        "external_id": "om_p3",
        "event_type": "message",
        "actor_id": "ou_owner",
        "actor_platform": "feishu",
        "thread_key": "feishu:chat_p3",
        "content": "把登录页拆成三个组件",
    }
    payload.update(overrides)
    return payload


def _ingest(client: TestClient, **overrides) -> dict:
    response = client.post(f"{PREFIX}/events", json=_event(**overrides))
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _plan(client: TestClient, trigger_id: str, **params) -> dict:
    response = client.post(f"{PREFIX}/observations/{trigger_id}/plan", params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _ticket(client: TestClient, ticket_id: str) -> dict:
    response = client.get(f"{PREFIX}/tickets/{ticket_id}")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _planned_ticket(client: TestClient) -> str:
    """走真实链路（事件 → 规划）拿一张工单，顺带完成自动订阅。"""
    return _plan(client, _ingest(client)["id"])["tickets"][0]["id"]


def _message_body(
    *,
    token: str | None = VERIFY_TOKEN,
    event_id: str = "evt_1",
    message_id: str = "om_hook_1",
    message_type: str = "text",
    text: str = "你好",
    event_type: str = "im.message.receive_v1",
    chat_id: str = "oc_chat_1",
) -> dict:
    header: dict = {"event_id": event_id, "event_type": event_type}
    if token is not None:
        header["token"] = token
    return {
        "schema": "2.0",
        "header": header,
        "event": {
            "sender": {"sender_id": {"open_id": "ou_hook"}},
            "message": {
                "message_id": message_id,
                "chat_id": chat_id,
                "message_type": message_type,
                "content": json.dumps({"text": text}),
            },
        },
    }


class _FakeChannel:
    """记录投递内容的通道替身；`fail=True` 模拟平台不可用。"""

    name = "feishu"

    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[tuple[str, str]] = []
        self.fail = fail
        self.closed = False

    async def send_text(self, external_id: str, text: str) -> None:
        if self.fail:
            raise RuntimeError("通道不可用")
        self.messages.append((external_id, text))

    async def aclose(self) -> None:
        self.closed = True


@contextmanager
def _app(settings: Settings, **updates: object) -> Iterator[tuple[TestClient, Container]]:
    """按需覆盖配置起一个 app，并返回容器（用于直接读写仓储）。"""
    configured = settings.model_copy(update=updates)
    app = create_app(configured)
    with TestClient(app) as client:
        yield client, app.state.container


# --- 飞书回调 ---


def test_feishu_url_verification_echoes_challenge(settings: Settings, db_schema: None) -> None:
    with _app(settings, supervisor_feishu_verification_token=VERIFY_TOKEN) as (client, _):
        response = client.post(
            WEBHOOK,
            json={"type": "url_verification", "challenge": "abc123", "token": VERIFY_TOKEN},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"challenge": "abc123"}


def test_feishu_webhook_is_fail_closed(settings: Settings, db_schema: None) -> None:
    # 没配 token：拒绝一切回调，不给"忘了配就是开放"的机会
    with _app(settings) as (client, _):
        assert client.post(WEBHOOK, json=_message_body()).status_code == 400

    with _app(settings, supervisor_feishu_verification_token=VERIFY_TOKEN) as (client, _):
        # token 不匹配
        assert client.post(WEBHOOK, json=_message_body(token="wrong")).status_code == 403
        # 完全不带 token
        assert client.post(WEBHOOK, json=_message_body(token=None)).status_code == 403
        # 飞书开启了 Encrypt Key：明确拒绝并要求关闭加密
        encrypted = client.post(WEBHOOK, json={"encrypt": "ciphertext"})
        assert encrypted.status_code == 400
        assert "Encrypt Key" in encrypted.json()["error"]
        # 非法 JSON
        assert client.post(WEBHOOK, content=b"not-json").status_code == 400


async def test_feishu_message_is_ingested_once(settings: Settings, db_schema: None) -> None:
    with _app(settings, supervisor_feishu_verification_token=VERIFY_TOKEN) as (client, container):
        first = client.post(WEBHOOK, json=_message_body())
        assert first.status_code == 200, first.text
        assert first.json()["created"] is True

        replay = client.post(WEBHOOK, json=_message_body())
        assert replay.status_code == 200, replay.text
        assert replay.json()["created"] is False
        assert replay.json()["trigger_id"] == first.json()["trigger_id"]

        observations = client.get(f"{PREFIX}/observations").json()
        assert observations["metadata"]["count"] == 1

        trigger_id = uuid.UUID(first.json()["trigger_id"])
        trigger = await container.supervisor_repo.get_trigger(trigger_id)
        assert trigger.content == "你好"
        assert trigger.thread_key == "oc_chat_1"
        assert trigger.actor_id == "ou_hook"
        # 审计用的原始请求体不能把 verification token 落库
        assert "token" not in (trigger.raw or {}).get("header", {})


def test_feishu_ignores_unsupported_events(settings: Settings, db_schema: None) -> None:
    with _app(settings, supervisor_feishu_verification_token=VERIFY_TOKEN) as (client, _):
        image = client.post(WEBHOOK, json=_message_body(message_type="image"))
        assert image.status_code == 200
        assert image.json()["ignored"] is True

        other = client.post(WEBHOOK, json=_message_body(event_type="im.chat.updated_v1"))
        assert other.status_code == 200
        assert other.json()["ignored"] is True

        assert client.get(f"{PREFIX}/observations").json()["metadata"]["count"] == 0


# --- 投递策略与 outbox ---


def test_should_deliver_is_fail_closed() -> None:
    assert should_deliver("all", ReportStatus.FAILED.value) is True
    assert should_deliver("terminal", ReportStatus.COMPLETED.value) is True
    assert should_deliver("terminal", "running") is False
    assert should_deliver("needs_input", ReportStatus.NEEDS_INPUT.value) is True
    assert should_deliver("needs_input", ReportStatus.COMPLETED.value) is False
    # 未知策略绝不投递
    assert should_deliver("sometimes", ReportStatus.COMPLETED.value) is False


async def test_outbox_enqueue_dedupes_by_key(container) -> None:
    outbox = container.supervisor_outbox
    assert await outbox.enqueue("demo.topic", {"n": 1}, dedupe_key="demo:1") is True
    assert await outbox.enqueue("demo.topic", {"n": 1}, dedupe_key="demo:1") is False
    assert await outbox.pending_count() == 1

    # 超长键压缩后仍然稳定且不冲突
    long_key = "x" * 200
    compact = normalize_dedupe_key(long_key)
    assert len(compact) <= 64
    assert compact == normalize_dedupe_key(long_key)
    assert compact != normalize_dedupe_key(f"{long_key}y")


# --- 自动订阅与订阅 API ---


def test_external_thread_is_auto_subscribed(client: TestClient) -> None:
    trigger = _ingest(client)
    ticket_id = _plan(client, trigger["id"])["tickets"][0]["id"]
    subscriptions = _ticket(client, ticket_id)["subscriptions"]
    assert [(s["source_key"], s["external_id"], s["mode"]) for s in subscriptions] == [
        ("feishu", "feishu:chat_p3", "terminal")
    ]

    # 重复规划不会重复订阅
    _plan(client, trigger["id"])
    assert len(_ticket(client, ticket_id)["subscriptions"]) == 1


def test_internal_source_is_not_subscribed(client: TestClient) -> None:
    trigger = _ingest(
        client,
        source_key="flux_internal",
        external_id="internal-1",
        thread_key="internal:1",
        actor_platform="flux",
    )
    ticket_id = _plan(client, trigger["id"])["tickets"][0]["id"]
    assert _ticket(client, ticket_id)["subscriptions"] == []


def test_subscription_api_lifecycle(client: TestClient) -> None:
    ticket_id = _planned_ticket(client)

    created = client.post(
        f"{PREFIX}/tickets/{ticket_id}/subscriptions",
        json={"source_key": "wechat", "external_id": "wx-1", "mode": "needs_input"},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["metadata"]["created"] is True
    subscription_id = body["data"]["id"]

    again = client.post(
        f"{PREFIX}/tickets/{ticket_id}/subscriptions",
        json={"source_key": "wechat", "external_id": "wx-1", "mode": "all"},
    )
    assert again.status_code == 200, again.text
    assert again.json()["metadata"]["created"] is False
    assert again.json()["data"]["id"] == subscription_id

    invalid = client.post(
        f"{PREFIX}/tickets/{ticket_id}/subscriptions",
        json={"source_key": "wechat", "external_id": "wx-2", "mode": "sometimes"},
    )
    assert invalid.status_code == 422, invalid.text

    missing = client.post(
        f"{PREFIX}/tickets/{uuid.uuid4()}/subscriptions",
        json={"source_key": "wechat", "external_id": "wx-3"},
    )
    assert missing.status_code == 404, missing.text

    listed = client.get(f"{PREFIX}/tickets/{ticket_id}/subscriptions").json()
    assert listed["metadata"]["count"] == 2

    deleted = client.delete(f"{PREFIX}/subscriptions/{subscription_id}")
    assert deleted.status_code == 200, deleted.text
    remaining = client.get(f"{PREFIX}/tickets/{ticket_id}/subscriptions").json()
    assert remaining["metadata"]["count"] == 1
    assert client.delete(f"{PREFIX}/subscriptions/{subscription_id}").status_code == 404


# --- 报告投递 ---


async def test_report_ready_outbox_delivers_once(client: TestClient) -> None:
    container = client.app.state.container
    ticket_id = uuid.UUID(_planned_ticket(client))
    channel = _FakeChannel()
    container.supervisor_worker._notifier = SupervisorNotifier(
        container.supervisor_repo, {"feishu": channel}
    )

    report = await container.supervisor_repo.create_report(
        ticket_id, status=ReportStatus.COMPLETED, summary="登录页已拆成三个组件"
    )
    await container.supervisor_worker.run_once(limit=10)
    assert len(channel.messages) == 1
    external_id, text = channel.messages[0]
    assert external_id == "feishu:chat_p3"
    assert "登录页已拆成三个组件" in text
    assert "已完成" in text
    assert str(ticket_id) in text

    # 同一份报告不会因为 outbox 重放而被发两次
    await container.supervisor_worker.run_once(limit=10)
    assert len(channel.messages) == 1
    assert (await container.supervisor_repo.get_report(report.id)).ticket_id == ticket_id


async def test_delivery_without_channel_raises_for_retry(client: TestClient) -> None:
    container = client.app.state.container
    ticket_id = uuid.UUID(_planned_ticket(client))
    report = await container.supervisor_repo.create_report(
        ticket_id, status=ReportStatus.COMPLETED, summary="收尾"
    )
    notifier = SupervisorNotifier(container.supervisor_repo, {})
    with pytest.raises(RuntimeError):
        await notifier.deliver_report(report.id)


# --- 只读查询面 ---


def test_reports_and_timeline_endpoints(client: TestClient) -> None:
    ticket_id = _planned_ticket(client)
    stopped = client.post(f"{PREFIX}/tickets/{ticket_id}/stop", json={"note": "不用做了"})
    assert stopped.status_code == 200, stopped.text

    reports = client.get(f"{PREFIX}/reports").json()
    assert reports["metadata"]["count"] == 1
    assert reports["data"][0]["status"] == ReportStatus.CANCELLED.value
    assert reports["data"][0]["summary"] == "不用做了"

    timeline = client.get(f"{PREFIX}/timeline").json()
    kinds = {item["kind"] for item in timeline["data"]}
    assert {"trigger", "ticket", "report"} <= kinds


def _rpc(client: TestClient, token: str, method: str, params: dict | None = None):
    body: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}"})


def _mcp_call(client: TestClient, token: str, name: str, arguments: dict) -> dict:
    response = _rpc(client, token, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"] is False, result["content"][0]["text"]
    return json.loads(result["content"][0]["text"])


def test_mcp_assistant_tools_replay_facts(mcp_client: TestClient) -> None:
    ticket_id = _planned_ticket(mcp_client)
    stopped = mcp_client.post(f"{PREFIX}/tickets/{ticket_id}/stop", json={"note": "收尾"})
    assert stopped.status_code == 200, stopped.text

    token = issue_token(mcp_client)

    overview = _mcp_call(mcp_client, token, "assistant.overview", {"limit": 5})
    assert overview["counts"]["stopped"] == 1
    assert overview["tickets"][0]["id"] == ticket_id

    ticket = _mcp_call(mcp_client, token, "assistant.ticket", {"ticket_id": ticket_id})
    assert ticket["stage"] == "stopped"
    assert ticket["reports"][0]["summary"] == "收尾"
    assert ticket["subscriptions"][0]["external_id"] == "feishu:chat_p3"

    reports = _mcp_call(mcp_client, token, "assistant.reports", {"limit": 5})
    assert [item["ticket_id"] for item in reports["reports"]] == [ticket_id]

    timeline = _mcp_call(mcp_client, token, "assistant.timeline", {"limit": 10})
    assert {"trigger", "ticket", "report"} <= {item["kind"] for item in timeline["events"]}

    # 只读工具不接受未知入参：越权/写操作在契约层就被拒绝
    denied = _rpc(
        mcp_client,
        token,
        "tools/call",
        {"name": "assistant.ticket", "arguments": {"ticket_id": ticket_id, "status": "done"}},
    ).json()["result"]
    assert denied["isError"] is True


def _create_task(client: TestClient, description: str, *, origin: str = "chat", **extra) -> dict:
    response = client.post(
        "/api/v1/tasks", json={"description": description, "origin": origin, **extra}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_mcp_assistant_reads_all_conversations(mcp_client: TestClient) -> None:
    """助理能通读普通对话：列表默认排除助理自己的对话，展开单条能拿到消息与全文。"""
    chat = _create_task(
        mcp_client, "把登录页拆成三个组件", summary="前情：已完成设计稿", summary_from=None
    )
    other = _create_task(mcp_client, "给支付页加埋点")
    own = _create_task(mcp_client, "梳理一下项目进度", origin="assistant")

    token = issue_token(mcp_client)

    listing = _mcp_call(mcp_client, token, "assistant.conversations", {"limit": 20})
    ids = {item["id"] for item in listing["conversations"]}
    assert ids == {chat["id"], other["id"]}
    assert own["id"] not in ids
    assert all(item["origin"] == "chat" for item in listing["conversations"])

    with_own = _mcp_call(
        mcp_client, token, "assistant.conversations", {"include_assistant": True, "limit": 20}
    )
    assert own["id"] in {item["id"] for item in with_own["conversations"]}

    keyword = _mcp_call(mcp_client, token, "assistant.conversations", {"keyword": "登录页"})
    assert [item["id"] for item in keyword["conversations"]] == [chat["id"]]

    detail = _mcp_call(mcp_client, token, "assistant.conversation", {"task_id": chat["id"]})
    assert detail["task"]["origin"] == "chat"
    assert [message["kind"] for message in detail["messages"]] == ["summary"]
    assert detail["has_more"] is False

    # 未知状态值在契约层就被拒绝，不会把整表当成无过滤条件读出
    invalid = _rpc(
        mcp_client,
        token,
        "tools/call",
        {"name": "assistant.conversations", "arguments": {"status": "done"}},
    ).json()["result"]
    assert invalid["isError"] is True

    # 列表接口的 origin 过滤与工具保持同一语义
    chats = mcp_client.get("/api/v1/tasks", params={"origin": "chat"}).json()["data"]
    assert {item["id"] for item in chats} >= {chat["id"], other["id"]}
    assistant_only = mcp_client.get("/api/v1/tasks", params={"origin": "assistant"}).json()["data"]
    assert [item["id"] for item in assistant_only] == [own["id"]]
