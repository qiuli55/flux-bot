"""外部例程触发（通用 Webhook + GitHub Webhook）测试。

覆盖口径：
- 验签 fail-closed、错误签名 / 错误请求体 / 非法事件名；
- GitHub 事件名映射、ping 与 delivery 去重；
- Bot 级 event_triggers 与例程 event_triggers 同时派发；
- 定向 routine_id / bot_id、停用例程与暂停 Bot 被跳过；
- 冷却与忙锁；投递统一经过 EventBus。

真实 Run 一律替换成空实现：本文件只验证触发与派发契约，不启动 runtime、不调模型。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from flux.config import Settings
from flux.container import Container
from flux.core.agent_runtime.runtimes.service import TaskRunService
from flux.core.bot.webhook_triggers import sign_body
from flux.enums import BotStatus, TaskStatus
from flux.main import create_app

PREFIX = "/api/v1"
ROUTINES_HOOK = f"{PREFIX}/hooks/routines"
GITHUB_HOOK = f"{PREFIX}/hooks/github"
ROUTINE_SECRET = "routine-test-secret"
GITHUB_SECRET = "github-test-secret"


@pytest.fixture(autouse=True)
def quiet_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """起 Run 换成空实现：任务停在 pending，用来验证「忙」与冷却。"""

    async def _noop(self: TaskRunService, task: object, *args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(TaskRunService, "start", _noop)


def _json(body: object) -> bytes:
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sign(secret: str, body: bytes) -> str:
    return sign_body(secret, body)


def _sig_headers(secret: str, body: bytes) -> dict[str, str]:
    return {"X-Flux-Signature": _sign(secret, body), "Content-Type": "application/json"}


def _github_headers(secret: str, body: bytes, event: str, delivery: str = "") -> dict[str, str]:
    headers = {
        "X-Hub-Signature-256": _sign(secret, body),
        "X-GitHub-Event": event,
        "Content-Type": "application/json",
    }
    if delivery:
        headers["X-GitHub-Delivery"] = delivery
    return headers


@contextmanager
def _client(settings: Settings, **updates: object) -> Iterator[TestClient]:
    app = create_app(settings.model_copy(update=updates))
    with TestClient(app) as client:
        yield client


def _create_bot(client: TestClient, **overrides: object) -> dict:
    payload: dict[str, object] = {"name": "webhook bot", "description": "处理外部事件"}
    payload.update(overrides)
    response = client.post(f"{PREFIX}/bots", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _create_routine(client: TestClient, bot_id: str, **overrides: object) -> dict:
    payload: dict[str, object] = {"name": "hook routine", "instruction": "检查事件"}
    payload.update(overrides)
    response = client.post(f"{PREFIX}/bots/{bot_id}/routines", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def _wait_for_new_task(
    container: Container, bot_id: uuid.UUID, previous: uuid.UUID | None = None
) -> uuid.UUID | None:
    for _ in range(200):
        bot = await container.bots.get(bot_id)
        if bot.last_task_id is not None and bot.last_task_id != previous:
            return bot.last_task_id
        await asyncio.sleep(0.01)
    return None


async def _settle(container: Container, *, seconds: float = 0.05) -> None:
    """给后台派发协程一点时间，用于断言「没有发生」。"""
    await asyncio.sleep(seconds)


# --- 通用 Webhook：验签与请求校验 ---


def test_generic_hook_fails_closed_without_secret(settings: Settings, db_schema: None) -> None:
    body = _json({"event": "webhook.received", "payload": {}})
    with _client(settings) as client:
        response = client.post(
            ROUTINES_HOOK, content=body, headers=_sig_headers(ROUTINE_SECRET, body)
        )
        assert response.status_code == 503
        assert response.json()["code"] == "webhook_not_configured"
        assert ROUTINE_SECRET not in response.text

    with _client(settings, routine_webhook_secret="   ") as client:
        response = client.post(
            ROUTINES_HOOK, content=body, headers=_sig_headers(ROUTINE_SECRET, body)
        )
        assert response.status_code == 503


def test_generic_hook_rejects_bad_signature_and_bad_body(
    settings: Settings, db_schema: None
) -> None:
    with _client(settings, routine_webhook_secret=ROUTINE_SECRET) as client:
        body = _json({"event": "webhook.received", "payload": {}})
        # 不带签名 / 格式错误 / 用错密钥
        assert client.post(ROUTINES_HOOK, content=body).status_code == 401
        assert (
            client.post(
                ROUTINES_HOOK, content=body, headers={"X-Flux-Signature": "deadbeef"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                ROUTINES_HOOK,
                content=body,
                headers={"X-Flux-Signature": _sign("wrong-secret", body)},
            ).status_code
            == 401
        )

        # 签名正确但请求体不合法
        for raw in (b"not-json", b"[]"):
            response = client.post(
                ROUTINES_HOOK, content=raw, headers=_sig_headers(ROUTINE_SECRET, raw)
            )
            assert response.status_code == 400
            assert response.json()["code"] == "invalid_json"

        # 通用入口只接受 webhook.* 前缀族，github.* / 垃圾事件名一律 400
        for event in (
            "bogus",
            "github.push",
            "webhook.",
            "webhook.-bad",
            f"webhook.{'x' * 80}",
        ):
            raw = _json({"event": event, "payload": {}})
            response = client.post(
                ROUTINES_HOOK, content=raw, headers=_sig_headers(ROUTINE_SECRET, raw)
            )
            assert response.status_code == 400, (event, response.text)
            assert response.json()["code"] == "invalid_event"

        raw = _json({"event": "webhook.received", "payload": []})
        response = client.post(
            ROUTINES_HOOK, content=raw, headers=_sig_headers(ROUTINE_SECRET, raw)
        )
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_payload"


def test_generic_hook_reports_matches_and_exposes_routine_triggers(
    settings: Settings, db_schema: None
) -> None:
    with _client(settings, routine_webhook_secret=ROUTINE_SECRET) as client:
        bot = _create_bot(client, event_triggers=["webhook.deploy"])
        routine = _create_routine(client, bot["id"], event_triggers=["webhook.deploy"])
        assert routine["event_triggers"] == ["webhook.deploy"]

        raw = _json({"event": "webhook.deploy", "payload": {"summary": "v1.2.3"}})
        response = client.post(
            ROUTINES_HOOK, content=raw, headers=_sig_headers(ROUTINE_SECRET, raw)
        )
        assert response.status_code == 200, response.text
        assert response.json()["bots_matched"] == 1
        assert response.json()["routines_matched"] == 1
        assert ROUTINE_SECRET not in response.text

        raw = _json({"event": "webhook.other", "payload": {}})
        response = client.post(
            ROUTINES_HOOK, content=raw, headers=_sig_headers(ROUTINE_SECRET, raw)
        )
        assert response.status_code == 200
        assert response.json()["bots_matched"] == 0
        assert response.json()["routines_matched"] == 0

        # 例程触发列表可单独更新（列举与详情都走 to_dict）
        patched = client.patch(
            f"{PREFIX}/bots/{bot['id']}/routines/{routine['id']}",
            json={"event_triggers": ["webhook.other", "github.push"]},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["data"]["event_triggers"] == ["webhook.other", "github.push"]

        listed = client.get(f"{PREFIX}/bots/{bot['id']}/routines").json()["data"]
        assert listed[0]["event_triggers"] == ["webhook.other", "github.push"]

        raw = _json({"event": "webhook.other", "payload": {}})
        response = client.post(
            ROUTINES_HOOK, content=raw, headers=_sig_headers(ROUTINE_SECRET, raw)
        )
        assert response.json()["routines_matched"] == 1

        # 未声明的事件名仍然拒绝（前缀族之外不放行）
        rejected = client.patch(
            f"{PREFIX}/bots/{bot['id']}/routines/{routine['id']}",
            json={"event_triggers": ["bogus", "github.not-an-event"]},
        )
        assert rejected.status_code == 400
        assert rejected.json()["code"] == "bad_request"


def test_generic_hook_explicit_targets(settings: Settings, db_schema: None) -> None:
    with _client(settings, routine_webhook_secret=ROUTINE_SECRET) as client:
        bot = _create_bot(client)
        routine = _create_routine(client, bot["id"])
        disabled = _create_routine(client, bot["id"], name="disabled", enabled=False)

        # 定向例程：不要求它订阅该事件
        raw = _json(
            {
                "event": "webhook.manual",
                "payload": {"summary": "manual"},
                "routine_id": routine["id"],
            }
        )
        response = client.post(
            ROUTINES_HOOK, content=raw, headers=_sig_headers(ROUTINE_SECRET, raw)
        )
        assert response.status_code == 200, response.text
        assert response.json()["bots_matched"] == 1
        assert response.json()["routines_matched"] == 1

        # 定向 Bot：只算 Bot 级触发
        raw = _json({"event": "webhook.manual", "payload": {}, "bot_id": bot["id"]})
        response = client.post(
            ROUTINES_HOOK, content=raw, headers=_sig_headers(ROUTINE_SECRET, raw)
        )
        assert response.status_code == 200
        assert response.json()["bots_matched"] == 1
        assert response.json()["routines_matched"] == 0

        # 停用例程 / 暂停 Bot 定向也不触发
        raw = _json({"event": "webhook.manual", "payload": {}, "routine_id": disabled["id"]})
        response = client.post(
            ROUTINES_HOOK, content=raw, headers=_sig_headers(ROUTINE_SECRET, raw)
        )
        assert response.status_code == 200
        assert response.json()["routines_matched"] == 0
        client.patch(f"{PREFIX}/bots/{bot['id']}", json={"status": BotStatus.PAUSED.value})
        raw = _json({"event": "webhook.manual", "payload": {}, "bot_id": bot["id"]})
        response = client.post(
            ROUTINES_HOOK, content=raw, headers=_sig_headers(ROUTINE_SECRET, raw)
        )
        assert response.status_code == 200
        assert response.json()["bots_matched"] == 0

        # 找不到目标 / 归属不一致 / UUID 非法
        raw = _json({"event": "webhook.manual", "payload": {}, "routine_id": str(uuid.uuid4())})
        response = client.post(
            ROUTINES_HOOK, content=raw, headers=_sig_headers(ROUTINE_SECRET, raw)
        )
        assert response.status_code == 404
        assert response.json()["code"] == "target_not_found"

        other_bot = _create_bot(client, name="other bot")
        raw = _json(
            {
                "event": "webhook.manual",
                "payload": {},
                "bot_id": other_bot["id"],
                "routine_id": routine["id"],
            }
        )
        response = client.post(
            ROUTINES_HOOK, content=raw, headers=_sig_headers(ROUTINE_SECRET, raw)
        )
        assert response.status_code == 404

        raw = _json({"event": "webhook.manual", "payload": {}, "routine_id": "not-a-uuid"})
        response = client.post(
            ROUTINES_HOOK, content=raw, headers=_sig_headers(ROUTINE_SECRET, raw)
        )
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_target"


def test_event_trigger_allowlist_rejects_garbage(settings: Settings, db_schema: None) -> None:
    with _client(settings) as client:
        for value in ("bogus", "github.not-an-event", "webhook.-bad"):
            response = client.post(
                f"{PREFIX}/bots",
                json={"name": "bad triggers", "event_triggers": [value]},
            )
            assert response.status_code == 400, (value, response.text)

        bot = _create_bot(client)
        response = client.post(
            f"{PREFIX}/bots/{bot['id']}/routines",
            json={"name": "bad routine triggers", "event_triggers": ["bogus"]},
        )
        assert response.status_code == 400

        # 受控前缀族正常放行
        response = client.post(
            f"{PREFIX}/bots/{bot['id']}/routines",
            json={
                "name": "good routine triggers",
                "event_triggers": ["webhook.received", "webhook.deploy.prod", "github.push"],
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["event_triggers"] == [
            "webhook.received",
            "webhook.deploy.prod",
            "github.push",
        ]


# --- GitHub Webhook ---


def test_github_hook_fails_closed_and_rejects_bad_signature(
    settings: Settings, db_schema: None
) -> None:
    body = _json({"ref": "refs/heads/main"})
    assert client_post_github(settings, body, GITHUB_SECRET, "push").status_code == 503
    # 密钥配置了，但签名错误
    response = client_post_github(
        settings, body, "wrong-secret", "push", github_secret=GITHUB_SECRET
    )
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_signature"


def client_post_github(
    settings: Settings,
    body: bytes,
    signing_secret: str,
    event: str,
    *,
    github_secret: str | None = None,
    delivery: str = "",
):
    updates: dict[str, object] = {}
    if github_secret is not None:
        updates["github_webhook_secret"] = github_secret
    with _client(settings, **updates) as client:
        return client.post(
            GITHUB_HOOK,
            content=body,
            headers=_github_headers(signing_secret, body, event, delivery),
        )


def test_github_hook_maps_official_events(settings: Settings, db_schema: None) -> None:
    body = _json(
        {
            "action": "opened",
            "number": 12,
            "repository": {"full_name": "acme/app"},
            "sender": {"login": "alice"},
            "pull_request": {"title": "Fix bug"},
        }
    )
    mapping = {
        "push": "github.push",
        "pull_request": "github.pull_request",
        "issues": "github.issues",
        "issue_comment": "github.issue_comment",
        "release": "github.release",
        "workflow_run": "github.workflow_run",
        # 其余官方事件按 github.<event> 动态放行
        "deployment_status": "github.deployment_status",
    }
    with _client(settings, github_webhook_secret=GITHUB_SECRET) as client:
        for header, expected in mapping.items():
            response = client.post(
                GITHUB_HOOK, content=body, headers=_github_headers(GITHUB_SECRET, body, header)
            )
            assert response.status_code == 200, (header, response.text)
            assert response.json()["event"] == expected
            assert GITHUB_SECRET not in response.text


def test_github_hook_ping_and_delivery_dedup(settings: Settings, db_schema: None) -> None:
    with _client(settings, github_webhook_secret=GITHUB_SECRET) as client:
        ping_body = _json({"zen": "Keep it logically awesome."})
        ping = client.post(
            GITHUB_HOOK,
            content=ping_body,
            headers=_github_headers(GITHUB_SECRET, ping_body, "ping", "delivery-ping"),
        )
        assert ping.status_code == 200
        assert ping.json()["event"] == "github.ping"
        assert ping.json()["ignored"] is True

        body = _json({"ref": "refs/heads/main"})
        headers = _github_headers(GITHUB_SECRET, body, "push", "delivery-42")
        first = client.post(GITHUB_HOOK, content=body, headers=headers)
        assert first.status_code == 200
        assert first.json().get("duplicate") is None

        replay = client.post(GITHUB_HOOK, content=body, headers=headers)
        assert replay.status_code == 200
        assert replay.json()["duplicate"] is True
        assert replay.json()["bots_matched"] == 0


def test_github_hook_acks_application_errors_after_signature(
    settings: Settings, db_schema: None
) -> None:
    with _client(settings, github_webhook_secret=GITHUB_SECRET) as client:
        # 签名通过但 JSON 不合法：2xx，避免 GitHub 无限重试
        bad = b"{not-json"
        response = client.post(
            GITHUB_HOOK, content=bad, headers=_github_headers(GITHUB_SECRET, bad, "push")
        )
        assert response.status_code == 200
        assert response.json()["accepted"] is False
        assert response.json()["ignored"] is True

        # 签名通过但事件头缺失 / 非法：同样 2xx
        body = _json({"ref": "refs/heads/main"})
        missing = client.post(
            GITHUB_HOOK, content=body, headers={"X-Hub-Signature-256": _sign(GITHUB_SECRET, body)}
        )
        assert missing.status_code == 200
        assert missing.json()["ignored"] is True

        invalid = client.post(
            GITHUB_HOOK,
            content=body,
            headers=_github_headers(GITHUB_SECRET, body, "Pull-Request"),
        )
        assert invalid.status_code == 200
        assert invalid.json()["ignored"] is True


# --- 派发：Bot 级 + 例程级 + 定向 + 闸门 ---


def test_routine_event_trigger_launches_through_event_bus(
    settings: Settings, db_schema: None
) -> None:
    async def _scenario() -> None:
        container = Container(settings)
        bot = await container.bots.create(name="routine bot", description="总描述")
        routine = await container.bots.create_routine(
            bot.id,
            name="deploy check",
            instruction="检查部署结果",
            event_triggers=["webhook.deploy"],
        )
        await container.bots.start()
        try:
            await container.bus.publish("webhook.deploy", {"summary": "v1.2.3"})
            task_id = await _wait_for_new_task(container, bot.id)
            assert task_id is not None
            task = await container.task_repo.get(task_id)
            assert task.description.startswith("[事件触发] webhook.deploy")
            assert "v1.2.3" in task.description
            assert "检查部署结果" in task.description

            refreshed = await container.bots.get(bot.id)
            assert refreshed.last_task_id == task_id
            routines = await container.bots.list_routines(bot.id)
            assert routines[0].last_run_at is not None
            assert routine.event_triggers == ["webhook.deploy"]
        finally:
            await container.bots.stop()
            await container.engine.dispose()

    asyncio.run(_scenario())


def test_bot_level_event_trigger_still_works_alongside_routines(
    settings: Settings, db_schema: None
) -> None:
    async def _scenario() -> None:
        container = Container(settings)
        bot = await container.bots.create(
            name="bot level", description="Bot 级作业", event_triggers=["webhook.received"]
        )
        await container.bots.create_routine(
            bot.id,
            name="other routine",
            instruction="别的例程",
            event_triggers=["webhook.other"],
        )
        await container.bots.start()
        try:
            await container.bus.publish("webhook.received", {"summary": "hello"})
            task_id = await _wait_for_new_task(container, bot.id)
            assert task_id is not None
            task = await container.task_repo.get(task_id)
            assert task.description.startswith("[事件触发] webhook.received")
            assert "Bot 级作业" in task.description
        finally:
            await container.bots.stop()
            await container.engine.dispose()

    asyncio.run(_scenario())


def test_disabled_routine_and_paused_bot_are_skipped(settings: Settings, db_schema: None) -> None:
    async def _scenario() -> None:
        container = Container(settings)
        disabled_bot = await container.bots.create(name="disabled routine bot")
        await container.bots.create_routine(
            disabled_bot.id,
            name="disabled",
            instruction="不该跑",
            enabled=False,
            event_triggers=["webhook.deploy"],
        )
        paused_bot = await container.bots.create(name="paused bot")
        await container.bots.create_routine(
            paused_bot.id,
            name="paused",
            instruction="也不该跑",
            event_triggers=["webhook.deploy"],
        )
        await container.bots.update(paused_bot.id, {"status": BotStatus.PAUSED.value})

        await container.bots.start()
        try:
            await container.bus.publish("webhook.deploy", {"summary": "v1"})
            await _settle(container)
            assert (await container.bots.get(disabled_bot.id)).last_task_id is None
            assert (await container.bots.get(paused_bot.id)).last_task_id is None
        finally:
            await container.bots.stop()
            await container.engine.dispose()

    asyncio.run(_scenario())


def test_event_cooldown_and_busy_lock_skip(settings: Settings, db_schema: None) -> None:
    async def _scenario() -> None:
        container = Container(settings)
        bot = await container.bots.create(name="cooldown bot", description="冷却与忙锁")
        await container.bots.create_routine(
            bot.id,
            name="deploy",
            instruction="检查部署",
            event_triggers=["webhook.deploy"],
        )
        await container.bots.start()
        try:
            await container.bus.publish("webhook.deploy", {"summary": "v1"})
            first_id = await _wait_for_new_task(container, bot.id)
            assert first_id is not None

            # 冷却：完成上一轮后立刻再来同一事件，仍然跳过
            await container.task_repo.set_status(first_id, TaskStatus.COMPLETED)
            await container.bus.publish("webhook.deploy", {"summary": "v2"})
            await _settle(container)
            assert (await container.bots.get(bot.id)).last_task_id == first_id

            # 忙锁：清掉冷却，但上一轮（手动开工）仍是 pending
            manual = await container.bots.run_now(bot.id)
            container.bots._event_last.clear()
            await container.bus.publish("webhook.deploy", {"summary": "v3"})
            await _settle(container)
            assert (await container.bots.get(bot.id)).last_task_id == manual.id
        finally:
            await container.bots.stop()
            await container.engine.dispose()

    asyncio.run(_scenario())


def test_targeted_routine_launches_without_subscription(
    settings: Settings, db_schema: None
) -> None:
    async def _scenario() -> None:
        container = Container(
            settings.model_copy(update={"routine_webhook_secret": ROUTINE_SECRET})
        )
        bot = await container.bots.create(name="targeted bot")
        routine = await container.bots.create_routine(
            bot.id, name="targeted", instruction="定向执行"
        )
        assert routine.event_triggers is None

        await container.bots.start()
        try:
            body = _json(
                {
                    "event": "webhook.manual",
                    "payload": {"summary": "manual run"},
                    "routine_id": str(routine.id),
                }
            )
            result = await container.webhook_triggers.handle_routine_hook(
                body, _sign(ROUTINE_SECRET, body)
            )
            assert result.status_code == 200
            assert result.payload["bots_matched"] == 1
            assert result.payload["routines_matched"] == 1

            task_id = await _wait_for_new_task(container, bot.id)
            assert task_id is not None
            task = await container.task_repo.get(task_id)
            assert task.description.startswith("[事件触发] webhook.manual")
            assert "manual run" in task.description
            assert "定向执行" in task.description
        finally:
            await container.bots.stop()
            await container.engine.dispose()

    asyncio.run(_scenario())


def test_generic_webhook_service_publishes_to_subscribers(
    settings: Settings, db_schema: None
) -> None:
    async def _scenario() -> None:
        container = Container(
            settings.model_copy(update={"routine_webhook_secret": ROUTINE_SECRET})
        )
        bot = await container.bots.create(
            name="subscribed bot", event_triggers=["webhook.received"]
        )
        await container.bots.create_routine(
            bot.id,
            name="subscribed routine",
            instruction="例行检查",
            event_triggers=["webhook.received"],
        )
        await container.bots.start()
        try:
            body = _json({"event": "webhook.received", "payload": {"summary": "from service"}})
            result = await container.webhook_triggers.handle_routine_hook(
                body, _sign(ROUTINE_SECRET, body)
            )
            assert result.status_code == 200
            assert result.payload["bots_matched"] == 1
            assert result.payload["routines_matched"] == 1

            task_id = await _wait_for_new_task(container, bot.id)
            assert task_id is not None
            task = await container.task_repo.get(task_id)
            # Bot 级触发优先，同一事件不会在同一 Bot 上再起一轮
            assert task.description.startswith("[事件触发] webhook.received")
            assert "from service" in task.description
        finally:
            await container.bots.stop()
            await container.engine.dispose()

    asyncio.run(_scenario())


def test_github_webhook_service_publishes_and_summarizes(
    settings: Settings, db_schema: None
) -> None:
    async def _scenario() -> None:
        container = Container(settings.model_copy(update={"github_webhook_secret": GITHUB_SECRET}))
        bot = await container.bots.create(name="github bot", event_triggers=["github.push"])
        await container.bots.start()
        try:
            body = _json(
                {
                    "ref": "refs/heads/main",
                    "repository": {"full_name": "acme/app"},
                    "sender": {"login": "alice"},
                    "commits": [{"id": "abc"}],
                }
            )
            result = await container.webhook_triggers.handle_github_hook(
                body,
                _sign(GITHUB_SECRET, body),
                "push",
                "delivery-1",
            )
            assert result.status_code == 200
            assert result.payload["event"] == "github.push"
            assert result.payload["bots_matched"] == 1

            task_id = await _wait_for_new_task(container, bot.id)
            assert task_id is not None
            task = await container.task_repo.get(task_id)
            assert task.description.startswith("[事件触发] github.push")
            assert "acme/app" in task.description
            assert "refs/heads/main" in task.description
        finally:
            await container.bots.stop()
            await container.engine.dispose()

    asyncio.run(_scenario())
