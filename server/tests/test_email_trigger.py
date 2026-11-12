"""Email 入站触发测试：轮询、持久去重、错误隔离、Bot 派发与优雅停机。

所有 IMAP 交互都用 `test_connector_email` 里的假连接对象顶替，绝不访问真实网络。
事件只发布到进程内 EventBus，用例直接看 `container.bus.history`。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from flux.config import Settings
from flux.container import Container
from flux.core.agent_runtime.runtimes.service import TaskRunService
from flux.core.event.bus import Events
from flux.main import create_app
from flux.models.email_trigger_cursor import EmailTriggerCursor
from tests.test_connector_email import (
    INSTALL_SETTINGS,
    PASSWORD,
    USERNAME,
    FakeIMAP,
    _headers,
    _install,
    _message_bytes,
    _patch_imap,
)

PREFIX = "/api/v1"
ACCOUNT_A = "account-a"
MAILBOX = "INBOX"
BODY_MARKER = "unique-body-marker-email-trigger"
SUBJECT = "Quarterly report"
SENDER = "bob@example.test"


def _mailbox(uid: int = 7, **kwargs: Any) -> dict[bytes, tuple[bytes, bool]]:
    return {
        str(uid).encode(): (
            _message_bytes(
                subject=kwargs.pop("subject", SUBJECT),
                sender=kwargs.pop("sender", SENDER),
                body=kwargs.pop("body", BODY_MARKER),
                **kwargs,
            ),
            False,
        )
    }


async def _install_email(container: Container, account_key: str = ACCOUNT_A) -> None:
    settings = dict(INSTALL_SETTINGS)
    await container.connector_framework.install(
        account_key,
        "email",
        auth_type="api_key",
        api_key=PASSWORD,
        settings=settings,
    )


async def _cursor(container: Container, account_key: str = ACCOUNT_A) -> EmailTriggerCursor | None:
    async with container.session_factory() as session:  # type: ignore[attr-defined]
        return (
            await session.scalars(
                select(EmailTriggerCursor).where(
                    EmailTriggerCursor.account_key == account_key,
                    EmailTriggerCursor.mailbox == MAILBOX,
                )
            )
        ).first()


def _email_events(container: Container) -> list[dict[str, Any]]:
    return [payload for event, payload in container.bus.history if event == Events.EMAIL_RECEIVED]


# --- 轮询与去重 -------------------------------------------------------------


def test_new_message_publishes_once_and_second_tick_deduplicates(
    settings: Settings, db_schema: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _scenario() -> None:
        container = Container(settings)
        try:
            await _install_email(container)
            _patch_imap(monkeypatch, FakeIMAP(_mailbox()))

            first = await container.email_trigger.poll_account(ACCOUNT_A)
            assert first["published"] == 1
            events = _email_events(container)
            assert len(events) == 1

            second = await container.email_trigger.poll_account(ACCOUNT_A)
            assert second["published"] == 0
            assert len(_email_events(container)) == 1

            cursor = await _cursor(container)
            assert cursor is not None
            assert cursor.last_uid == 7
            assert cursor.last_status == "ok"
        finally:
            await container.dispose()

    asyncio.run(_scenario())


def test_payload_has_headers_but_no_body_or_password(
    settings: Settings, db_schema: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _scenario() -> None:
        container = Container(settings)
        try:
            await _install_email(container)
            _patch_imap(monkeypatch, FakeIMAP(_mailbox()))

            await container.email_trigger.poll_account(ACCOUNT_A)
            payload = _email_events(container)[0]

            assert payload["account_key"] == ACCOUNT_A
            assert payload["connector_id"] == "email"
            assert payload["mailbox"] == MAILBOX
            assert payload["id"] == "7"
            assert payload["uid"] == 7
            assert payload["from"] == SENDER
            assert payload["subject"] == SUBJECT
            assert payload["date"]

            serialized = json.dumps(payload, ensure_ascii=False)
            assert BODY_MARKER not in serialized
            assert PASSWORD not in serialized
            assert "body" not in payload
        finally:
            await container.dispose()

    asyncio.run(_scenario())


def test_persisted_cursor_prevents_republish_after_restart(
    settings: Settings, db_schema: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _scenario() -> None:
        first_container = Container(settings)
        try:
            await _install_email(first_container)
            _patch_imap(monkeypatch, FakeIMAP(_mailbox()))
            await first_container.email_trigger.poll_account(ACCOUNT_A)
            assert len(_email_events(first_container)) == 1
        finally:
            await first_container.dispose()

        # 新容器 = 新进程：进程内集合为空，只靠 email_trigger_cursors 去重。
        restarted = Container(settings)
        try:
            _patch_imap(monkeypatch, FakeIMAP(_mailbox()))
            result = await restarted.email_trigger.poll_account(ACCOUNT_A)
            assert result["published"] == 0
            assert _email_events(restarted) == []
            cursor = await _cursor(restarted)
            assert cursor is not None and cursor.last_uid == 7
        finally:
            await restarted.dispose()

    asyncio.run(_scenario())


# --- 生命周期 ---------------------------------------------------------------


def test_disabled_or_zero_interval_does_not_start_poller(
    settings: Settings, db_schema: None
) -> None:
    async def _scenario() -> None:
        disabled = Container(settings.model_copy(update={"email_trigger_enabled": False}))
        try:
            await disabled.email_trigger.start()
            assert disabled.email_trigger.running is False
        finally:
            await disabled.dispose()

        zero_interval = Container(
            settings.model_copy(
                update={"email_trigger_enabled": True, "email_trigger_interval_seconds": 0}
            )
        )
        try:
            await zero_interval.email_trigger.start()
            assert zero_interval.email_trigger.running is False
        finally:
            await zero_interval.dispose()

    asyncio.run(_scenario())


def test_interval_has_a_minimum_floor(settings: Settings, db_schema: None) -> None:
    async def _scenario() -> None:
        container = Container(settings.model_copy(update={"email_trigger_interval_seconds": 0.01}))
        try:
            assert container.email_trigger.interval_seconds >= 15.0
        finally:
            await container.dispose()

    asyncio.run(_scenario())


def test_shutdown_cancels_and_awaits_poller_task(
    settings: Settings, db_schema: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _scenario() -> None:
        container = Container(settings.model_copy(update={"email_trigger_enabled": True}))
        cancelled = asyncio.Event()

        async def _slow_tick() -> list[dict[str, Any]]:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return []

        monkeypatch.setattr(container.email_trigger, "tick", _slow_tick)
        try:
            await container.email_trigger.start()
            assert container.email_trigger.running is True
            await asyncio.sleep(0.01)
            await asyncio.wait_for(container.email_trigger.stop(), timeout=2)
            # stop() 必须 await 被取消的任务，而不是 cancel 完就返回。
            assert cancelled.is_set()
            assert container.email_trigger.running is False
        finally:
            await container.dispose()

    asyncio.run(_scenario())


# --- 错误隔离 ---------------------------------------------------------------


def test_imap_error_does_not_kill_the_loop(
    settings: Settings, db_schema: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _scenario() -> None:
        container = Container(settings)
        try:
            await _install_email(container)
            _patch_imap(monkeypatch, FakeIMAP(login_error=RuntimeError("imap down")))

            # tick() 自己吞掉账号级异常；后台循环因此不会退出。
            results = await container.email_trigger.tick()
            assert results == []
            cursor = await _cursor(container)
            assert cursor is not None
            assert cursor.last_status == "error"
            assert cursor.last_error
            assert "imap down" not in (cursor.last_error or "").lower()

            # 下一轮 IMAP 恢复：同一循环继续工作并正常发布。
            _patch_imap(monkeypatch, FakeIMAP(_mailbox()))
            await container.email_trigger.tick()
            assert len(_email_events(container)) == 1
            cursor = await _cursor(container)
            assert cursor is not None and cursor.last_status == "ok"
        finally:
            await container.dispose()

    asyncio.run(_scenario())


def test_tick_skips_accounts_without_installation(
    settings: Settings, db_schema: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _scenario() -> None:
        container = Container(settings)
        try:
            # 没有任何安装行：不会调用 IMAP，也不会抛异常。
            assert await container.email_trigger.tick() == []
        finally:
            await container.dispose()

    asyncio.run(_scenario())


def test_tick_skips_smtp_only_installation(settings: Settings, db_schema: None) -> None:
    async def _scenario() -> None:
        container = Container(settings)
        try:
            await container.connector_framework.install(
                ACCOUNT_A,
                "email",
                auth_type="api_key",
                api_key=PASSWORD,
                settings={"smtp_host": "smtp.example.test", "username": USERNAME},
            )
            # 只发信、不收信：不发起 IMAP 调用，也不记一条每轮重复的错误。
            assert await container.email_trigger.tick() == []
            assert await _cursor(container) is None
        finally:
            await container.dispose()

    asyncio.run(_scenario())


# --- Bot / 例程派发 ---------------------------------------------------------


def test_bot_subscribed_to_email_received_is_launched(
    settings: Settings, db_schema: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _noop_start(
        self: TaskRunService, task: object, *args: object, **kwargs: object
    ) -> None:
        return None

    monkeypatch.setattr(TaskRunService, "start", _noop_start)

    async def _scenario() -> None:
        container = Container(settings)
        try:
            bot = await container.bots.create(
                name="email bot",
                description="处理新邮件",
                event_triggers=[Events.EMAIL_RECEIVED],
            )
            await container.bots.start()
            try:
                await _install_email(container)
                _patch_imap(monkeypatch, FakeIMAP(_mailbox()))
                await container.email_trigger.poll_account(ACCOUNT_A)

                task_id: uuid.UUID | None = None
                for _ in range(200):
                    fresh = await container.bots.get(bot.id)
                    if fresh.last_task_id is not None:
                        task_id = fresh.last_task_id
                        break
                    await asyncio.sleep(0.01)

                assert task_id is not None, "email.received 没有触发 Bot 开工"
                task = await container.task_repo.get(task_id)
                assert "[事件触发] email.received" in (task.description or "")
                assert SUBJECT in (task.description or "")
            finally:
                await container.bots.stop()
        finally:
            await container.dispose()

    asyncio.run(_scenario())


# --- 手动轮询 API -----------------------------------------------------------


def test_poll_api_runs_one_tick(
    settings: Settings, db_schema: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(create_app(settings)) as client:
        installed = _install(client)
        assert installed.status_code == 200, installed.text
        _patch_imap(monkeypatch, FakeIMAP(_mailbox()))

        response = client.post(f"{PREFIX}/connectors/email/poll", headers=_headers(ACCOUNT_A))
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["published"] == 1
        assert data["messages"][0]["subject"] == SUBJECT
        assert BODY_MARKER not in response.text
        assert PASSWORD not in response.text

        again = client.post(f"{PREFIX}/connectors/email/poll", headers=_headers(ACCOUNT_A))
        assert again.status_code == 200, again.text
        assert again.json()["data"]["published"] == 0
