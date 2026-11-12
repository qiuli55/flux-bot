"""Bot 模式 API 测试（2026-11-05）。

口径：
- Bot 与对话共用执行链路：每次开工落一条 `tasks` 记录（`tasks.bot_id` + `workspace` 快照）；
- Bot 任务默认不进首页对话列表，`/tasks?bot_id=` 才能单独取；
- 同一 Bot 同一时刻只允许一轮（上一轮未终态时手动触发 409）；
- 定时靠落库的 `next_run_at`：`tick()` 取到期 Bot 开工，并推进下一次触发时间。

真实 Run 一律替换成空实现：本文件只验证配置面与调度契约，不启动 runtime、不调模型。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flux.container import Container
from flux.core.agent_runtime.runtimes.service import TaskRunService
from flux.enums import TaskStatus
from flux.errors import ConflictError

PREFIX = "/api/v1"

_BOT_KEYS = {
    "id",
    "name",
    "description",
    "avatar_shape",
    "avatar_color",
    "agent_id",
    "model",
    "permission_mode",
    "decision_mode",
    "workspace",
    "workspace_path",
    "status",
    "schedule",
    "timezone",
    "missed_run_policy",
    "next_run_at",
    "last_run_at",
    "last_task_id",
    "chat_task_id",
    "last_settled_task_id",
    "failure_threshold",
    "consecutive_failures",
    "runs_total",
    "runs_failed",
    "last_error",
    "paused_reason",
    "token_budget",
    "tokens_used_today",
    "notify_on_failure",
    "event_triggers",
    "allowed_tools",
    "allowed_commands",
    "allowed_paths",
    "created_at",
    "updated_at",
}


@pytest.fixture(autouse=True)
def quiet_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """起 Run 换成空实现：任务留在 pending，正好用来验证「忙」与「不重复开工」。"""

    async def _noop(self: TaskRunService, task: object, *args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(TaskRunService, "start", _noop)


def _create(client: TestClient, **overrides: object) -> dict:
    payload: dict[str, object] = {"name": "巡检", "description": "检查服务健康"}
    payload.update(overrides)
    response = client.post(f"{PREFIX}/bots", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_create_bot_defaults(client: TestClient) -> None:
    """默认：active、需人工审核、无定时、工作目录按 <dsh_home>/bots/<id>/workspace 派生。"""
    bot = _create(client)
    assert set(bot) == _BOT_KEYS
    assert bot["status"] == "active"
    assert bot["permission_mode"] == "normal"
    assert bot["decision_mode"] == "auto"
    assert bot["schedule"] is None
    assert bot["next_run_at"] is None
    assert Path(bot["workspace_path"]).parts[-3:] == ("bots", bot["id"], "workspace")
    # 健康与熔断：默认连续失败 3 次自动暂停，统计从 0 开始
    assert bot["failure_threshold"] == 3
    assert bot["consecutive_failures"] == 0
    assert bot["runs_total"] == 0
    assert bot["last_error"] is None
    assert bot["paused_reason"] is None
    # 预算默认无上限，今日用量从账本实时聚合
    assert bot["token_budget"] is None
    assert bot["tokens_used_today"] == 0
    # 自动化默认：失败主动通知打开，不响应事件，不额外限制工具/命令/路径
    assert bot["notify_on_failure"] is True
    assert bot["event_triggers"] == []
    assert bot["allowed_tools"] is None
    assert bot["allowed_commands"] is None
    assert bot["allowed_paths"] is None


def test_create_bot_with_schedule_sets_next_run(client: TestClient) -> None:
    bot = _create(client, schedule="*/15 * * * *")
    assert bot["schedule"] == "*/15 * * * *"
    assert bot["next_run_at"] is not None


def test_create_bot_rejects_bad_schedule(client: TestClient) -> None:
    response = client.post(f"{PREFIX}/bots", json={"name": "坏定时", "schedule": "not a cron"})
    assert response.status_code == 400


def test_create_bot_rejects_relative_workspace(client: TestClient) -> None:
    response = client.post(
        f"{PREFIX}/bots", json={"name": "相对目录", "workspace": "relative/path"}
    )
    assert response.status_code == 400


def test_create_bot_rejects_custom_workspace_without_root(
    client: TestClient, tmp_path: Path
) -> None:
    response = client.post(
        f"{PREFIX}/bots", json={"name": "无根工作区", "workspace": str(tmp_path / "workspace")}
    )
    assert response.status_code == 400
    assert "FLUX_WORKSPACE_ROOT" in response.text


def test_create_bot_accepts_workspace_under_root(
    apply_client: TestClient, workspace_root: Path
) -> None:
    workspace = workspace_root / "bots" / "custom"
    workspace.mkdir(parents=True)
    bot = _create(apply_client, workspace=str(workspace))
    assert bot["workspace"] == str(workspace.resolve())


@pytest.mark.parametrize("outside", ["/etc", "sibling", "database_dir"])
def test_create_bot_rejects_workspace_outside_root(
    apply_client: TestClient, workspace_root: Path, outside: str
) -> None:
    if outside == "/etc":
        workspace = Path("/etc")
    elif outside == "sibling":
        workspace = workspace_root.parent / "sibling"
    else:
        # workspace_root.parent 就是测试数据库 flux-test.db 所在目录
        workspace = workspace_root.parent
    response = apply_client.post(
        f"{PREFIX}/bots", json={"name": "越界工作区", "workspace": str(workspace)}
    )
    assert response.status_code == 400, response.text


def test_create_bot_allows_empty_workspace(apply_client: TestClient, workspace_root: Path) -> None:
    for workspace in ("", None):
        payload = {"name": "默认工作区", "workspace": workspace}
        response = apply_client.post(f"{PREFIX}/bots", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["workspace"] is None


def test_patch_bot_workspace_is_confined_to_root(
    apply_client: TestClient, workspace_root: Path
) -> None:
    bot = _create(apply_client)
    outside = apply_client.patch(
        f"{PREFIX}/bots/{bot['id']}", json={"workspace": str(workspace_root.parent)}
    )
    assert outside.status_code == 400

    inside = workspace_root / "patched"
    accepted = apply_client.patch(f"{PREFIX}/bots/{bot['id']}", json={"workspace": str(inside)})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["workspace"] == str(inside.resolve())


def test_create_bot_unknown_agent_is_404(client: TestClient) -> None:
    response = client.post(f"{PREFIX}/bots", json={"name": "野 Agent", "agent_id": "no-such-agent"})
    assert response.status_code == 404


def test_patch_pause_clears_next_run(client: TestClient) -> None:
    bot = _create(client, schedule="0 * * * *")
    paused = client.patch(f"{PREFIX}/bots/{bot['id']}", json={"status": "paused"})
    assert paused.status_code == 200, paused.text
    data = paused.json()["data"]
    assert data["status"] == "paused"
    assert data["next_run_at"] is None
    # 恢复后按 cron 重新算出下一次触发
    resumed = client.patch(f"{PREFIX}/bots/{bot['id']}", json={"status": "active"})
    assert resumed.json()["data"]["next_run_at"] is not None


def test_run_now_creates_bot_task_hidden_from_chat_list(client: TestClient) -> None:
    bot = _create(client)
    started = client.post(f"{PREFIX}/bots/{bot['id']}/run")
    assert started.status_code == 200, started.text
    payload = started.json()["data"]
    task = payload["task"]
    assert task["bot_id"] == bot["id"]
    assert task["bot_name"] == bot["name"]
    assert payload["bot"]["last_task_id"] == task["id"]

    # 首页对话列表默认不含 Bot 任务
    chat_tasks = client.get(f"{PREFIX}/tasks").json()["data"]
    assert all(item["bot_id"] is None for item in chat_tasks)

    # 按 bot_id 过滤能取到，且带上 workspace 快照
    bot_tasks = client.get(f"{PREFIX}/tasks", params={"bot_id": bot["id"]}).json()["data"]
    assert [item["id"] for item in bot_tasks] == [task["id"]]
    listed = client.get(f"{PREFIX}/bots/{bot['id']}/tasks").json()["data"]
    assert [item["id"] for item in listed] == [task["id"]]


def test_run_now_conflicts_while_previous_round_running(client: TestClient) -> None:
    bot = _create(client)
    assert client.post(f"{PREFIX}/bots/{bot['id']}/run").status_code == 200
    conflict = client.post(f"{PREFIX}/bots/{bot['id']}/run")
    assert conflict.status_code == 409


def test_run_now_requires_instruction_or_description(client: TestClient) -> None:
    bot = _create(client, description="")
    response = client.post(f"{PREFIX}/bots/{bot['id']}/run")
    assert response.status_code == 400


def test_list_filter_and_delete(client: TestClient) -> None:
    kept = _create(client, name="保留")
    removed = _create(client, name="删除")
    client.patch(f"{PREFIX}/bots/{removed['id']}", json={"status": "paused"})

    paused = client.get(f"{PREFIX}/bots", params={"status": "paused"}).json()["data"]
    assert [bot["id"] for bot in paused] == [removed["id"]]
    assert client.get(f"{PREFIX}/bots", params={"status": "bogus"}).status_code == 400

    assert client.delete(f"{PREFIX}/bots/{removed['id']}").status_code == 200
    assert client.get(f"{PREFIX}/bots/{removed['id']}").status_code == 404
    assert client.get(f"{PREFIX}/bots/{kept['id']}").status_code == 200


def test_tick_launches_due_bot_and_advances_schedule(container: Container) -> None:
    """到期的 Bot 由 tick 开工，且 next_run_at 立刻推进到下一个触发点。"""

    async def _scenario() -> None:
        bot = await container.bots.create(
            name="定时巡检", description="检查磁盘", schedule="*/10 * * * *"
        )
        first_due = bot.next_run_at
        # 往前推到一天后：一定已经到期
        later = datetime.now(timezone.utc) + timedelta(days=1)
        started = await container.bots.tick(later)
        refreshed = await container.bots.get(bot.id)
        assert started == [str(refreshed.last_task_id)]
        assert refreshed.last_task_id is not None
        assert refreshed.next_run_at is not None
        assert refreshed.next_run_at > first_due

        # 上一轮还是 pending（替身不起 Run）：再扫一次必须跳过，不能重复开工
        again = await container.bots.tick(later + timedelta(minutes=5))
        assert again == []

    asyncio.run(_scenario())


def test_failure_threshold_can_be_configured(client: TestClient) -> None:
    bot = _create(client, failure_threshold=5)
    assert bot["failure_threshold"] == 5
    patched = client.patch(f"{PREFIX}/bots/{bot['id']}", json={"failure_threshold": 0})
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["failure_threshold"] == 0
    # 越界直接按请求非法拒绝（0 = 永不自动暂停）
    assert (
        client.patch(f"{PREFIX}/bots/{bot['id']}", json={"failure_threshold": 21}).status_code
        == 422
    )


def test_consecutive_failures_pause_bot_then_success_resumes(container: Container) -> None:
    """连续失败到阈值自动暂停；成功一轮结算后自愈回 active。"""

    async def _scenario() -> None:
        bot = await container.bots.create(
            name="熔断巡检",
            description="检查磁盘",
            schedule="*/10 * * * *",
            failure_threshold=2,
        )
        base = datetime.now(timezone.utc) + timedelta(days=1)

        first = await container.bots.tick(base)
        assert len(first) == 1
        await container.task_repo.set_status(uuid.UUID(first[0]), TaskStatus.FAILED)

        # 第一次失败：只累计，不暂停，照常开下一轮
        second = await container.bots.tick(base + timedelta(days=1))
        assert len(second) == 1
        midway = await container.bots.get(bot.id)
        assert midway.status == "active"
        assert midway.consecutive_failures == 1
        assert midway.runs_failed == 1
        assert midway.last_error is not None
        await container.task_repo.set_status(midway.last_task_id, TaskStatus.FAILED)

        # 第二次失败达到阈值：自动暂停，且不再开工
        third = await container.bots.tick(base + timedelta(days=2))
        assert third == []
        paused = await container.bots.get(bot.id)
        assert paused.status == "paused"
        assert paused.next_run_at is None
        assert paused.consecutive_failures == 2
        assert paused.runs_total == 2
        assert paused.paused_reason is not None

        # 暂停中仍可手动开工；跑通一轮后调度器结算即自愈
        task = await container.bots.run_now(bot.id)
        await container.task_repo.set_status(task.id, TaskStatus.COMPLETED)
        assert await container.bots.tick(base + timedelta(days=3)) == []
        healed = await container.bots.get(bot.id)
        assert healed.status == "active"
        assert healed.consecutive_failures == 0
        assert healed.last_error is None
        assert healed.paused_reason is None
        assert healed.next_run_at is not None

    asyncio.run(_scenario())


def test_manual_pause_survives_successful_run(container: Container) -> None:
    """用户手动暂停不是熔断：跑通一轮也不该自动恢复。"""

    async def _scenario() -> None:
        bot = await container.bots.create(name="手动暂停", description="检查磁盘")
        await container.bots.update(bot.id, {"status": "paused"})
        task = await container.bots.run_now(bot.id)
        await container.task_repo.set_status(task.id, TaskStatus.COMPLETED)
        await container.bots.tick(datetime.now(timezone.utc) + timedelta(days=1))
        assert (await container.bots.get(bot.id)).status == "paused"

    asyncio.run(_scenario())


def test_token_budget_is_configurable_with_unlimited_option(client: TestClient) -> None:
    """预算可自定义；null / 0 都表示无上限，负数直接拒绝。"""
    bot = _create(client, token_budget=50_000)
    assert bot["token_budget"] == 50_000
    assert bot["tokens_used_today"] == 0

    cleared = client.patch(f"{PREFIX}/bots/{bot['id']}", json={"token_budget": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["data"]["token_budget"] is None

    zeroed = client.patch(f"{PREFIX}/bots/{bot['id']}", json={"token_budget": 0})
    assert zeroed.status_code == 200, zeroed.text
    assert zeroed.json()["data"]["token_budget"] is None

    assert client.patch(f"{PREFIX}/bots/{bot['id']}", json={"token_budget": -1}).status_code == 422


def test_daily_budget_blocks_manual_run_until_raised(container: Container) -> None:
    """今日用量达到上限后手动开工 409；改成无上限立刻放行。"""

    async def _scenario() -> None:
        bot = await container.bots.create(name="预算巡检", description="检查磁盘", token_budget=100)
        task = await container.bots.run_now(bot.id)
        await container.task_repo.set_status(task.id, TaskStatus.COMPLETED)
        await container.billing.record_step(
            run_id="run-budget", model="gpt-5", input_tokens=120, task_id=str(task.id)
        )

        assert (await container.bots.tokens_used_today()).get(bot.id) == 120
        with pytest.raises(ConflictError):
            await container.bots.run_now(bot.id)

        # 调高预算（这里直接设为无上限）后下一轮立刻可以开工
        await container.bots.update(bot.id, {"token_budget": None})
        again = await container.bots.run_now(bot.id)
        assert again.id != task.id

    asyncio.run(_scenario())


def test_daily_budget_skips_scheduled_tick(container: Container) -> None:
    """定时触发遇到预算用尽：跳过本轮并推进 next_run_at，不在同一时刻反复重试。"""

    async def _scenario() -> None:
        bot = await container.bots.create(
            name="预算定时", description="检查磁盘", schedule="*/10 * * * *", token_budget=100
        )
        base = datetime.now(timezone.utc) + timedelta(days=1)
        first = await container.bots.tick(base)
        assert len(first) == 1
        task_id = uuid.UUID(first[0])
        await container.task_repo.set_status(task_id, TaskStatus.COMPLETED)

        moment = base + timedelta(minutes=10)
        await container.billing.record_step(
            run_id="run-budget-tick",
            model="gpt-5",
            input_tokens=150,
            task_id=str(task_id),
            occurred_at=moment,
        )

        assert await container.bots.tick(moment) == []
        refreshed = await container.bots.get(bot.id)
        assert refreshed.last_task_id == task_id
        assert refreshed.next_run_at is not None
        assert refreshed.next_run_at.replace(tzinfo=timezone.utc) > moment

    asyncio.run(_scenario())


def test_failure_writes_notification_until_marked_read(container: Container) -> None:
    """失败结算时写一条主动通知；已读后未读数归零。"""

    async def _scenario() -> None:
        bot = await container.bots.create(name="通知巡检", description="检查磁盘")
        task = await container.bots.run_now(bot.id)
        await container.task_repo.set_status(task.id, TaskStatus.FAILED)
        await container.bots.tick(datetime.now(timezone.utc) + timedelta(days=1))

        rows = await container.bots.list_notifications()
        assert len(rows) == 1
        assert rows[0].bot_id == bot.id
        assert rows[0].task_id == task.id
        assert "失败" in rows[0].title
        assert rows[0].body
        assert await container.bots.unread_notification_count() == 1

        assert await container.bots.mark_notification_read(rows[0].id) is not None
        assert await container.bots.unread_notification_count() == 0

    asyncio.run(_scenario())


def test_notification_can_be_disabled(container: Container) -> None:
    """关掉 notify_on_failure 后失败只累计统计，不写通知。"""

    async def _scenario() -> None:
        bot = await container.bots.create(
            name="静默巡检", description="检查磁盘", notify_on_failure=False
        )
        task = await container.bots.run_now(bot.id)
        await container.task_repo.set_status(task.id, TaskStatus.FAILED)
        await container.bots.tick(datetime.now(timezone.utc) + timedelta(days=1))

        assert await container.bots.list_notifications() == []
        assert (await container.bots.get(bot.id)).runs_failed == 1

    asyncio.run(_scenario())


def test_event_triggers_launch_bot_once_and_skip_self(container: Container) -> None:
    """事件触发：匹配到 event_triggers 才开工；自己产生的任务事件不再触发自己。"""

    async def _scenario() -> None:
        bot = await container.bots.create(
            name="事件巡检", description="检查磁盘", event_triggers=["task.completed"]
        )
        await container.bots.handle_event("task.completed", {"task_id": "other-task"})
        for _ in range(100):
            await asyncio.sleep(0.01)
            if (await container.bots.get(bot.id)).last_task_id is not None:
                break

        triggered = await container.bots.get(bot.id)
        assert triggered.last_task_id is not None
        task = await container.task_repo.get(triggered.last_task_id)
        assert task.description.startswith("[事件触发] task.completed")
        await container.task_repo.set_status(task.id, TaskStatus.COMPLETED)

        # 自己产生的 task.completed 不该再次触发自己（否则会无限自激）
        await container.bots.tick(datetime.now(timezone.utc) + timedelta(days=1))
        await container.bots.handle_event(
            "task.completed", {"task_id": str(task.id), "bot_id": str(bot.id)}
        )
        for _ in range(10):
            await asyncio.sleep(0.01)
        assert (await container.bots.get(bot.id)).last_task_id == task.id

    asyncio.run(_scenario())


def test_routine_tick_uses_own_instruction_and_keeps_bot_schedule(container: Container) -> None:
    """例程按自己的 cron 开工，用例程指令；不改写 Bot 主定时的 next_run_at。"""

    async def _scenario() -> None:
        bot = await container.bots.create(name="例程巡检", description="总描述")
        routine = await container.bots.create_routine(
            bot.id, name="夜间检查", instruction="检查备份", schedule="0 3 * * *"
        )
        assert routine.next_run_at is not None

        later = datetime.now(timezone.utc) + timedelta(days=1)
        started = await container.bots.tick(later)
        assert len(started) == 1
        task = await container.task_repo.get(uuid.UUID(started[0]))
        assert task.description == "检查备份"

        refreshed = await container.bots.get(bot.id)
        assert refreshed.next_run_at is None
        assert refreshed.last_task_id == task.id

        routines = await container.bots.list_routines(bot.id)
        assert routines[0].last_run_at is not None
        assert routines[0].next_run_at is not None
        assert routines[0].next_run_at.replace(tzinfo=timezone.utc) > later

    asyncio.run(_scenario())


def test_bot_automation_config_via_api(client: TestClient) -> None:
    """新配置面：失败通知开关、事件触发、工具/命令/路径白名单。"""
    bot = _create(
        client,
        notify_on_failure=False,
        event_triggers=["task.completed"],
        allowed_tools=["workspace.read"],
        allowed_commands=["git"],
        allowed_paths=["src"],
    )
    assert bot["notify_on_failure"] is False
    assert bot["event_triggers"] == ["task.completed"]
    assert bot["allowed_tools"] == ["workspace.read"]
    assert bot["allowed_commands"] == ["git"]
    assert bot["allowed_paths"] == ["src"]

    # 未知事件、硬禁令工具、绝对路径都在写入口拒绝
    assert (
        client.post(
            f"{PREFIX}/bots", json={"name": "坏事件", "event_triggers": ["bogus"]}
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"{PREFIX}/bots", json={"name": "坏工具", "allowed_tools": ["git.push"]}
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"{PREFIX}/bots", json={"name": "坏路径", "allowed_paths": ["/etc"]}
        ).status_code
        == 400
    )

    # 显式传 [] 表示取消白名单限制
    cleared = client.patch(f"{PREFIX}/bots/{bot['id']}", json={"allowed_tools": []})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["data"]["allowed_tools"] is None


def test_tools_catalog_excludes_forbidden(client: TestClient) -> None:
    data = client.get(f"{PREFIX}/bots/tools").json()["data"]
    names = {item["name"] for item in data}
    assert "workspace.read" in names
    assert "git.push" not in names
    assert "secret.read" not in names


def test_routine_crud_via_api(client: TestClient) -> None:
    bot = _create(client)
    created = client.post(
        f"{PREFIX}/bots/{bot['id']}/routines",
        json={"name": "日报", "instruction": "写日报", "schedule": "0 9 * * *"},
    )
    assert created.status_code == 200, created.text
    routine = created.json()["data"]
    assert routine["next_run_at"] is not None
    assert routine["enabled"] is True

    listed = client.get(f"{PREFIX}/bots/{bot['id']}/routines").json()["data"]
    assert [row["id"] for row in listed] == [routine["id"]]

    patched = client.patch(
        f"{PREFIX}/bots/{bot['id']}/routines/{routine['id']}",
        json={"schedule": None, "enabled": False},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["schedule"] is None
    assert patched.json()["data"]["next_run_at"] is None
    assert patched.json()["data"]["enabled"] is False

    assert (
        client.post(
            f"{PREFIX}/bots/{bot['id']}/routines", json={"name": "坏例程", "schedule": "bogus"}
        ).status_code
        == 400
    )

    assert client.delete(f"{PREFIX}/bots/{bot['id']}/routines/{routine['id']}").status_code == 200
    assert client.get(f"{PREFIX}/bots/{bot['id']}/routines").json()["data"] == []


def test_notifications_api_lists_and_marks_read(container: Container, client: TestClient) -> None:
    """通知接口：列表带未读数，单条 / 全部标记已读。"""

    async def _seed() -> None:
        bot = await container.bots.create(name="接口巡检", description="检查磁盘")
        task = await container.bots.run_now(bot.id)
        await container.task_repo.set_status(task.id, TaskStatus.FAILED)
        await container.bots.tick(datetime.now(timezone.utc) + timedelta(days=1))

    asyncio.run(_seed())

    listed = client.get(f"{PREFIX}/bots/notifications").json()
    assert listed["metadata"]["unread"] == 1
    notification = listed["data"][0]

    marked = client.post(f"{PREFIX}/bots/notifications/{notification['id']}/read")
    assert marked.status_code == 200, marked.text
    assert client.get(f"{PREFIX}/bots/notifications").json()["metadata"]["unread"] == 0

    assert client.post(f"{PREFIX}/bots/notifications/read-all").status_code == 200
    missing = client.post(f"{PREFIX}/bots/notifications/{uuid.uuid4()}/read")
    assert missing.status_code == 404
