"""Task API 落库测试（M1 增量 B：任务由内存落 tasks 表，主规格 §12.4 / §11.2）。"""

from __future__ import annotations

import asyncio
import uuid

from fastapi.testclient import TestClient

from flux.config import Settings
from flux.core.task_engine.repository import MessageDraft
from flux.db.session import create_engine
from flux.enums import DshRunStatus, TaskStatus
from flux.main import create_app
from flux.models.project import Project

PREFIX = "/api/v1"

# M0 冻结的响应体字段（不含 cost）+ 后续增量追加的 created_at
# + Solo 生命周期字段（P0-05/P0-06 与 §5 决策模式：决策策略、需求确认、决策点、DSH Run）
_RESPONSE_KEYS = {
    "id",
    "description",
    "status",
    "priority",
    "agent_id",
    "project_id",
    "result",
    "created_at",
    "decision_mode",
    "confirmation",
    "decisions",
    "pending_decision",
    "run_id",
    "model",
    "api_key_id",
    # 对话权限三模式（plan / normal / full）
    "permission_mode",
    # 执行中切换 Agent 的排队目标（本轮 Run 收尾后生效）
    "pending_agent_id",
    # 对话来源（chat = 首页普通对话 / assistant = 助理对话）
    "origin",
    # Bot 档案（对话即 bot：名称 + 形状 × 颜色头像）
    "bot_name",
    "avatar_shape",
    "avatar_color",
    # Bot 模式：这条任务由哪个常驻 Bot 开工（为空 = 普通对话）
    "bot_id",
}


def _seed_project(database_url: str) -> str:
    """直接向 projects 表插入一行，供任务外键使用（M1 尚无项目 API）。"""
    project_id = uuid.uuid4()
    engine = create_engine(database_url)

    async def _insert() -> None:
        async with engine.begin() as conn:
            await conn.execute(Project.__table__.insert().values(id=project_id, name="测试项目"))

    try:
        asyncio.run(_insert())
    finally:
        asyncio.run(engine.dispose())
    return str(project_id)


def _new_client(settings: Settings) -> TestClient:
    """构造一个全新的 app 实例（模拟进程重启：容器、调度队列、数据库会话全部重建）。"""
    return TestClient(create_app(settings))


def test_create_then_get_reads_back_same_task(client: TestClient) -> None:
    """创建后 GET 能读回同一条任务，且响应体是 M0 冻结字段与 Solo 生命周期字段的并集。"""
    created = client.post(f"{PREFIX}/tasks", json={"description": "修复登录缺陷", "priority": 5})
    assert created.status_code == 200
    task = created.json()["data"]
    assert set(task) == _RESPONSE_KEYS
    assert task["status"] == "pending"
    assert task["priority"] == 5
    # created_at 来自数据库（server_default）而不是请求体，必须是非空时间串
    assert isinstance(task["created_at"], str) and task["created_at"]

    fetched = client.get(f"{PREFIX}/tasks/{task['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["data"] == task


def test_task_survives_app_restart(settings: Settings, db_schema: None) -> None:
    """重启后任务仍在：换个全新的 app 实例仍能从库里读回同一条。"""
    project_id = _seed_project(settings.database_url)

    with _new_client(settings) as first:
        created = first.post(
            f"{PREFIX}/tasks",
            json={"description": "重启后仍在", "priority": 7, "project_id": project_id},
        )
        assert created.status_code == 200
        task_id = created.json()["data"]["id"]

    with _new_client(settings) as second:
        fetched = second.get(f"{PREFIX}/tasks/{task_id}")

    assert fetched.status_code == 200
    data = fetched.json()["data"]
    assert data["description"] == "重启后仍在"
    assert data["priority"] == 7
    assert data["project_id"] == project_id


def test_cancel_persists_even_when_task_not_in_queue(settings: Settings, db_schema: None) -> None:
    """取消落库持久化：任务不在内存队列里（新进程）也必须取消成功。"""
    with _new_client(settings) as first:
        task_id = first.post(f"{PREFIX}/tasks", json={"description": "待取消"}).json()["data"]["id"]

    # 新 app 实例的调度队列为空，但数据库才是状态权威，取消必须成功
    with _new_client(settings) as second:
        cancelled = second.post(f"{PREFIX}/tasks/{task_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["data"]["status"] == "cancelled"
        persisted = second.get(f"{PREFIX}/tasks/{task_id}").json()["data"]
        assert persisted["status"] == "cancelled"


def test_cancel_twice_conflicts(client: TestClient) -> None:
    """终态（cancelled）任务再次取消返回 409（沿用 M0 断言风格）。"""
    task_id = client.post(f"{PREFIX}/tasks", json={"description": "甲"}).json()["data"]["id"]
    assert client.post(f"{PREFIX}/tasks/{task_id}/cancel").status_code == 200

    response = client.post(f"{PREFIX}/tasks/{task_id}/cancel")
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"


def test_delete_task_removes_messages_runs_and_queue_entry(client: TestClient) -> None:
    """删除对话是彻底删除：任务、消息、运行记录一起清掉，排队中的也要摘出队列。"""
    task_id = client.post(f"{PREFIX}/tasks", json={"description": "待删除的对话"}).json()["data"][
        "id"
    ]
    task_uuid = uuid.UUID(task_id)
    container = client.app.state.container

    async def _seed() -> None:
        await container.task_repo.add_messages(
            task_uuid, [MessageDraft(role="user", content="删除前的消息")]
        )
        await container.run_repo.create(
            run_id="run-delete-1",
            session_id="session-delete-1",
            instruction="x",
            status=DshRunStatus.COMPLETED,
            task_id=task_uuid,
        )
        await container.task_memory.add(task_uuid, "run-delete-1", "记忆卡内容")
        await container.runtime_sessions.set(task_uuid, "opencode", "ses-delete-1")

    asyncio.run(_seed())
    assert task_id in [item.task_id for item in container.scheduler.pending()]

    deleted = client.delete(f"{PREFIX}/tasks/{task_id}")
    assert deleted.status_code == 200
    assert deleted.json()["data"] == {
        "id": task_id,
        "deleted": True,
        "messages": 1,
        "runs": 1,
    }

    assert client.get(f"{PREFIX}/tasks/{task_id}").status_code == 404
    assert client.get(f"{PREFIX}/tasks/{task_id}/messages").status_code == 404
    assert task_id not in [task["id"] for task in client.get(f"{PREFIX}/tasks").json()["data"]]
    assert task_id not in [item.task_id for item in container.scheduler.pending()]
    assert asyncio.run(container.run_repo.get("run-delete-1")) is None
    # 工作记忆卡与会话绑定也必须一起清掉，否则重建同名任务会继承旧会话。
    assert asyncio.run(container.task_memory.latest(task_uuid)) is None
    assert asyncio.run(container.runtime_sessions.get(task_uuid, "opencode")) == ""


def test_delete_running_task_conflicts_until_stopped(client: TestClient) -> None:
    """运行中的对话不允许直接删除：先停止，停完才可以删。"""
    task_id = client.post(f"{PREFIX}/tasks", json={"description": "执行中的对话"}).json()["data"][
        "id"
    ]
    container = client.app.state.container
    asyncio.run(container.task_repo.set_status(uuid.UUID(task_id), TaskStatus.RUNNING))

    blocked = client.delete(f"{PREFIX}/tasks/{task_id}")
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "conflict"
    assert "先停止" in blocked.json()["message"]

    assert client.post(f"{PREFIX}/tasks/{task_id}/cancel").status_code == 200
    assert client.delete(f"{PREFIX}/tasks/{task_id}").status_code == 200
    assert client.get(f"{PREFIX}/tasks/{task_id}").status_code == 404


def test_delete_unknown_or_malformed_task_not_found(client: TestClient) -> None:
    """未知任务与非法 UUID 的删除都按 404 处理。"""
    assert client.delete(f"{PREFIX}/tasks/{uuid.uuid4()}").status_code == 404
    malformed = client.delete(f"{PREFIX}/tasks/不是-uuid")
    assert malformed.status_code == 404
    assert malformed.json()["code"] == "not_found"


def test_unknown_task_not_found(client: TestClient) -> None:
    """未知任务 GET / 取消都返回 404。"""
    unknown = uuid.uuid4()
    assert client.get(f"{PREFIX}/tasks/{unknown}").status_code == 404
    assert client.post(f"{PREFIX}/tasks/{unknown}/cancel").status_code == 404


def test_malformed_task_id_not_found(client: TestClient) -> None:
    """非法 UUID 的任务标识按 404 处理（不因参数不是 UUID 而返回 422）。"""
    response = client.get(f"{PREFIX}/tasks/不是-uuid")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert client.post(f"{PREFIX}/tasks/不是-uuid/cancel").status_code == 404


def test_invalid_project_id_is_bad_request(client: TestClient) -> None:
    """project_id 非法字符串 → 400。"""
    response = client.post(f"{PREFIX}/tasks", json={"description": "x", "project_id": "不是-uuid"})
    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"


def test_invalid_agent_id_is_bad_request(client: TestClient) -> None:
    """agent_id 非法字符串 → 400。"""
    response = client.post(f"{PREFIX}/tasks", json={"description": "x", "agent_id": "不是-uuid"})
    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"


def test_unknown_agent_id_not_found(client: TestClient) -> None:
    """agent_id 是合法 UUID 但 Agent 不存在 → 404（边界 fail-closed）。"""
    response = client.post(
        f"{PREFIX}/tasks", json={"description": "x", "agent_id": str(uuid.uuid4())}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_unknown_project_id_not_found(client: TestClient) -> None:
    """project_id 是合法 UUID 但项目不存在 → 404，与 agent_id 的语义一致。

    不能退化成「靠数据库外键报约束错」的 400：同一类「关联实体不存在」在 API 上
    必须只有一个错误码，否则调用方无法用统一逻辑处理。
    """
    response = client.post(
        f"{PREFIX}/tasks", json={"description": "x", "project_id": str(uuid.uuid4())}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_task_with_seeded_project_is_created(settings: Settings, db_schema: None) -> None:
    """项目真实存在时建任务成功，且 project_id 原样落库。"""
    project_id = _seed_project(settings.database_url)
    with _new_client(settings) as client_:
        created = client_.post(
            f"{PREFIX}/tasks", json={"description": "归属某项目", "project_id": project_id}
        )
    assert created.status_code == 200
    assert created.json()["data"]["project_id"] == project_id


def test_task_with_existing_agent_persists_agent_id(client: TestClient) -> None:
    """agent_id 指向已注册 Agent 时正常建任务并落库。"""
    agent_id = client.post(
        f"{PREFIX}/agents", json={"name": "测试 Agent", "role": "developer"}
    ).json()["data"]["id"]

    created = client.post(f"{PREFIX}/tasks", json={"description": "带 Agent", "agent_id": agent_id})
    assert created.status_code == 200
    task = created.json()["data"]
    assert task["agent_id"] == agent_id
    assert client.get(f"{PREFIX}/tasks/{task['id']}").json()["data"]["agent_id"] == agent_id


def test_priority_orders_scheduler_queue_and_is_persisted(client: TestClient) -> None:
    """priority 生效于调度队列顺序，且落库值与请求一致。"""
    low = client.post(f"{PREFIX}/tasks", json={"description": "低优先", "priority": 7}).json()[
        "data"
    ]
    high = client.post(f"{PREFIX}/tasks", json={"description": "高优先", "priority": 3}).json()[
        "data"
    ]

    container = client.app.state.container
    order = [scheduled.task_id for scheduled in container.scheduler.pending()]
    assert order == [high["id"], low["id"]]

    assert client.get(f"{PREFIX}/tasks/{low['id']}").json()["data"]["priority"] == 7
    assert client.get(f"{PREFIX}/tasks/{high['id']}").json()["data"]["priority"] == 3
