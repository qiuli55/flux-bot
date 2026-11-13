"""统一出站草稿：持久化、状态机与只读 API 契约。

这些用例不触网、不发真实消息：发送本身仍由频道在审批结果里完成，
本文件只验证草稿的落库、原子认领、终态与查询/取消接口。
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flux.config import Settings
from flux.container import Container
from flux.core.channels.outbound_drafts import OutboundDraftService
from flux.db.session import create_engine, create_session_factory
from flux.errors import ConflictError, NotFoundError

PREFIX = "/api/v1"


def _service(settings: Settings) -> tuple[Any, OutboundDraftService]:
    """独立引擎 + 服务：避免与 TestClient 的事件循环共用连接。"""
    engine = create_engine(settings.database_url)
    return engine, OutboundDraftService(create_session_factory(engine))


async def _create(
    service: OutboundDraftService,
    *,
    channel: str = "feishu",
    body: str = "待发送正文",
    **kwargs: Any,
) -> Any:
    return await service.create(
        channel=channel,
        body=body,
        dest={"chat_id": "oc_test"},
        **kwargs,
    )


def _seed(
    settings: Settings,
    *,
    transitions: list[tuple[str, tuple[Any, ...]]] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """给 API 用例造数据；transitions 用来把草稿推到 sent / failed 等状态。"""

    async def _run() -> dict[str, Any]:
        engine, service = _service(settings)
        try:
            draft = await _create(service, **kwargs)
            for name, args in transitions or []:
                await getattr(service, name)(draft.id, *args)
            return (await service.get(draft.id)).to_dict()
        finally:
            await engine.dispose()

    return asyncio.run(_run())


# --- 持久化与状态机 ---


def test_create_pending_draft_never_sends(settings: Settings, db_schema: None) -> None:
    async def scenario() -> None:
        engine, service = _service(settings)
        try:
            bot_id = uuid.uuid4()
            task_id = uuid.uuid4()
            turn_id = uuid.uuid4()
            draft = await _create(
                service,
                body="你好，世界",
                bot_id=bot_id,
                task_id=task_id,
                turn_id=turn_id,
            )
            assert draft.status == "pending"
            assert draft.body == "你好，世界"
            assert draft.body_hash == hashlib.sha256("你好，世界".encode()).hexdigest()
            assert draft.dest == {"chat_id": "oc_test"}
            assert draft.bot_id == bot_id
            assert draft.task_id == task_id
            assert draft.turn_id == turn_id
            assert draft.decision_id is None
            assert draft.sent_at is None
            assert draft.resolved_at is None

            # 只是落库，没有任何发送痕迹
            pending = await service.list(channel="feishu", status="pending")
            assert [row.id for row in pending] == [draft.id]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_allow_claims_once_and_marks_sent(settings: Settings, db_schema: None) -> None:
    async def scenario() -> None:
        engine, service = _service(settings)
        try:
            draft = await _create(service, body="只发一次")
            claimed = await service.claim(draft.id)
            assert claimed is not None
            assert claimed.status == "sending"
            assert claimed.body == "只发一次"
            assert claimed.dest == {"chat_id": "oc_test"}

            # 第二次「允许」拿不到认领权，也就不会发送第二次
            assert await service.claim(draft.id) is None

            assert await service.mark_sent(draft.id) is True
            sent = await service.get(draft.id)
            assert sent.status == "sent"
            assert sent.sent_at is not None
            assert sent.resolved_at is not None
            assert sent.body == "只发一次"
            # 终态不能再迁移
            assert await service.mark_sent(draft.id) is False
            assert await service.claim(draft.id) is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_deny_marks_denied_and_keeps_body(settings: Settings, db_schema: None) -> None:
    async def scenario() -> None:
        engine, service = _service(settings)
        try:
            draft = await _create(service, body="拒绝也别丢")
            assert await service.mark_denied(draft.id) is True
            denied = await service.get(draft.id)
            assert denied.status == "denied"
            assert denied.body == "拒绝也别丢"
            assert denied.sent_at is None
            assert denied.resolved_at is not None
            # 幂等且不可再被认领
            assert await service.mark_denied(draft.id) is False
            assert await service.claim(draft.id) is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_cancel_only_pending(settings: Settings, db_schema: None) -> None:
    async def scenario() -> None:
        engine, service = _service(settings)
        try:
            draft = await _create(service, body="取消我")
            cancelled = await service.cancel(draft.id)
            assert cancelled.status == "cancelled"
            assert cancelled.body == "取消我"
            assert cancelled.resolved_at is not None

            with pytest.raises(ConflictError):
                await service.cancel(draft.id)
            with pytest.raises(NotFoundError):
                await service.cancel(uuid.uuid4())

            sent_draft = await _create(service, body="已经发了")
            await service.claim(sent_draft.id)
            await service.mark_sent(sent_draft.id)
            with pytest.raises(ConflictError):
                await service.cancel(sent_draft.id)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_send_failure_marks_failed_and_retains_body(settings: Settings, db_schema: None) -> None:
    async def scenario() -> None:
        engine, service = _service(settings)
        try:
            draft = await _create(service, body="正文不能丢")
            await service.claim(draft.id)
            assert await service.mark_failed(draft.id, RuntimeError("上游 500")) is True
            failed = await service.get(draft.id)
            assert failed.status == "failed"
            assert failed.body == "正文不能丢"
            assert failed.body_hash
            assert failed.sent_at is None
            assert failed.resolved_at is not None
            assert "RuntimeError" in (failed.error or "")
            # 错误串只放异常信息，不放正文
            assert "正文不能丢" not in (failed.error or "")
            # failed 不自动重试，也不能被再次认领
            assert await service.mark_failed(draft.id, "again") is False
            assert await service.claim(draft.id) is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_draft_survives_container_restart(settings: Settings, db_schema: None) -> None:
    """Container A 落库并 dispose，Container B 指向同一个 SQLite 文件仍能看到。"""

    async def scenario() -> None:
        first = Container(settings)
        try:
            draft = await first.outbound_drafts.create(
                channel="feishu",
                body="重启后还在",
                dest={"chat_id": "oc_restart", "message_id": "om_restart"},
            )
            draft_id = draft.id
        finally:
            await first.dispose()

        second = Container(settings)
        try:
            row = await second.outbound_drafts.get(draft_id)
            assert row.status == "pending"
            assert row.body == "重启后还在"
            assert row.dest["chat_id"] == "oc_restart"
            rows = await second.outbound_drafts.list(channel="feishu", status="pending")
            assert [item.id for item in rows] == [draft_id]
        finally:
            await second.dispose()

    asyncio.run(scenario())


# --- API ---


def test_list_and_get_return_body_and_support_filters(
    client: TestClient, settings: Settings
) -> None:
    bot_id = uuid.uuid4()
    pending = _seed(settings, body="待发送正文", bot_id=bot_id)
    sent = _seed(
        settings,
        body="已经发送",
        bot_id=bot_id,
        transitions=[("claim", ()), ("mark_sent", ())],
    )

    all_rows = client.get(f"{PREFIX}/outbound-drafts").json()
    assert all_rows["success"] is True
    assert all_rows["metadata"]["count"] == 2

    pending_only = client.get(f"{PREFIX}/outbound-drafts", params={"status": "pending"}).json()[
        "data"
    ]
    assert [row["id"] for row in pending_only] == [pending["id"]]

    by_bot = client.get(f"{PREFIX}/outbound-drafts", params={"bot_id": str(bot_id)}).json()["data"]
    assert {row["id"] for row in by_bot} == {pending["id"], sent["id"]}

    other_channel = client.get(f"{PREFIX}/outbound-drafts", params={"channel": "wecom"}).json()[
        "data"
    ]
    assert other_channel == []

    detail = client.get(f"{PREFIX}/outbound-drafts/{pending['id']}").json()["data"]
    assert detail["body"] == "待发送正文"
    assert detail["status"] == "pending"
    assert detail["dest"] == {"chat_id": "oc_test"}

    sent_detail = client.get(f"{PREFIX}/outbound-drafts/{sent['id']}").json()["data"]
    assert sent_detail["status"] == "sent"
    assert sent_detail["sent_at"] is not None


def test_api_rejects_unknown_id_bad_status_and_bad_limit(
    client: TestClient, settings: Settings
) -> None:
    missing = uuid.uuid4()
    assert client.get(f"{PREFIX}/outbound-drafts/{missing}").status_code == 404
    assert client.get(f"{PREFIX}/outbound-drafts/not-a-uuid").status_code == 404

    bad_status = client.get(f"{PREFIX}/outbound-drafts", params={"status": "bogus"})
    assert bad_status.status_code == 400
    assert bad_status.json()["code"] == "bad_request"

    bad_bot = client.get(f"{PREFIX}/outbound-drafts", params={"bot_id": "not-a-uuid"})
    assert bad_bot.status_code == 400

    assert client.get(f"{PREFIX}/outbound-drafts", params={"limit": 0}).status_code == 400
    assert client.get(f"{PREFIX}/outbound-drafts", params={"limit": 201}).status_code == 400


def test_cancel_endpoint_only_cancels_pending(client: TestClient, settings: Settings) -> None:
    pending = _seed(settings, body="取消我")
    response = client.post(f"{PREFIX}/outbound-drafts/{pending['id']}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "cancelled"
    assert response.json()["data"]["body"] == "取消我"

    # 终态再取消返回 409
    again = client.post(f"{PREFIX}/outbound-drafts/{pending['id']}/cancel")
    assert again.status_code == 409
    assert again.json()["code"] == "conflict"

    # 已发送的草稿同样不能取消
    sent = _seed(settings, body="已发送", transitions=[("claim", ()), ("mark_sent", ())])
    assert client.post(f"{PREFIX}/outbound-drafts/{sent['id']}/cancel").status_code == 409

    # 不存在的草稿 404
    assert client.post(f"{PREFIX}/outbound-drafts/{uuid.uuid4()}/cancel").status_code == 404


def test_denied_draft_body_is_readable_via_api(client: TestClient, settings: Settings) -> None:
    denied = _seed(settings, body="被拒绝的正文", transitions=[("mark_denied", ())])
    detail = client.get(f"{PREFIX}/outbound-drafts/{denied['id']}").json()["data"]
    assert detail["status"] == "denied"
    assert detail["body"] == "被拒绝的正文"
