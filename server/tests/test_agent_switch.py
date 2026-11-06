"""无感切换 Agent：执行中排队、本轮收尾后生效、失败保留原 Agent。"""

from __future__ import annotations

import asyncio
import uuid

from fastapi.testclient import TestClient

from flux.container import Container
from flux.enums import AgentInstallStatus, TaskStatus
from flux.models.agent_installation import AgentInstallation

PREFIX = "/api/v1"


def _create_agent(client: TestClient, name: str, runtime: str = "dsh") -> str:
    response = client.post(
        f"{PREFIX}/agents", json={"name": name, "role": "developer", "runtime": runtime}
    )
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["id"])


def _create_task(client: TestClient, agent_id: str) -> dict:
    response = client.post(
        f"{PREFIX}/tasks", json={"description": "把批量转换脚本补完并跑通", "agent_id": agent_id}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _set_status(client: TestClient, task_id: str, status: TaskStatus) -> None:
    container: Container = client.app.state.container
    asyncio.run(container.task_repo.set_status(uuid.UUID(task_id), status))


def _apply_pending(client: TestClient, task_id: str) -> None:
    container: Container = client.app.state.container

    async def _run() -> None:
        task = await container.task_repo.get(uuid.UUID(task_id))
        await container.agent_switch.apply_pending(task, "run-test", "completed")

    asyncio.run(_run())


def _messages(client: TestClient, task_id: str) -> list[dict]:
    response = client.get(f"{PREFIX}/tasks/{task_id}/messages", params={"limit": 100})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _ready_installation(client: TestClient, name: str) -> None:
    container: Container = client.app.state.container

    async def _run() -> None:
        async with container.session_factory() as session:
            session.add(
                AgentInstallation(name=name, adapter=name, status=AgentInstallStatus.READY.value)
            )
            await session.commit()

    asyncio.run(_run())


def test_switch_while_running_is_queued(client: TestClient) -> None:
    """执行中切换只排队：当前 Agent、本轮进度都不动，响应标 effective_from=next_run。"""
    agent_a = _create_agent(client, "builder")
    agent_b = _create_agent(client, "reviewer")
    task = _create_task(client, agent_a)
    _set_status(client, task["id"], TaskStatus.RUNNING)

    response = client.post(f"{PREFIX}/tasks/{task['id']}/agent", json={"agent_id": agent_b})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["queued"] is True
    assert data["effective_from"] == "next_run"
    assert data["agent_id"] == agent_a
    assert data["pending_agent_id"] == agent_b
    assert data["target_agent_id"] == agent_b
    assert data["target_agent"] == "reviewer"

    # 排队期间真正的执行者仍是原 Agent
    current = client.get(f"{PREFIX}/tasks/{task['id']}").json()["data"]
    assert current["agent_id"] == agent_a
    assert current["pending_agent_id"] == agent_b


def test_queued_switch_applies_after_run_finishes(client: TestClient) -> None:
    """Run 收尾后应用排队：换人、清模型覆盖、落交接卡、发 agent_switched 事件。"""
    agent_a = _create_agent(client, "builder")
    agent_b = _create_agent(client, "reviewer")
    task = _create_task(client, agent_a)
    _set_status(client, task["id"], TaskStatus.RUNNING)
    client.post(f"{PREFIX}/tasks/{task['id']}/agent", json={"agent_id": agent_b})

    _set_status(client, task["id"], TaskStatus.COMPLETED)
    _apply_pending(client, task["id"])

    current = client.get(f"{PREFIX}/tasks/{task['id']}").json()["data"]
    assert current["agent_id"] == agent_b
    assert current["pending_agent_id"] is None
    assert current["model"] is None
    assert current["api_key_id"] is None

    handoffs = [m for m in _messages(client, task["id"]) if m["kind"] == "handoff"]
    assert len(handoffs) == 1
    card = handoffs[0]
    assert "Agent 交接卡" in card["content"]
    assert "builder" in card["content"] and "reviewer" in card["content"]
    assert card["payload"]["from_agent_id"] == agent_a
    assert card["payload"]["to_agent_id"] == agent_b

    events = client.get(f"{PREFIX}/tasks/{task['id']}/events", params={"after_seq": 0})
    assert events.status_code == 200, events.text
    types = [item["type"] for item in events.json()["data"]]
    assert "agent_switched" in types


def test_queued_switch_waits_for_user_decision(client: TestClient) -> None:
    """任务在等人决策时还没真正结束，排队保留、不提前换人。"""
    agent_a = _create_agent(client, "builder")
    agent_b = _create_agent(client, "reviewer")
    task = _create_task(client, agent_a)
    _set_status(client, task["id"], TaskStatus.RUNNING)
    client.post(f"{PREFIX}/tasks/{task['id']}/agent", json={"agent_id": agent_b})
    _set_status(client, task["id"], TaskStatus.WAITING_FOR_USER_DECISION)

    _apply_pending(client, task["id"])

    current = client.get(f"{PREFIX}/tasks/{task['id']}").json()["data"]
    assert current["agent_id"] == agent_a
    assert current["pending_agent_id"] == agent_b
    assert not [m for m in _messages(client, task["id"]) if m["kind"] == "handoff"]


def test_switch_to_unready_runtime_keeps_agent(client: TestClient) -> None:
    """目标 runtime 未接入：按真实原因报错，原 Agent 与排队状态都不变。"""
    agent_a = _create_agent(client, "builder")
    agent_c = _create_agent(client, "cli-worker", runtime="opencode")
    task = _create_task(client, agent_a)

    response = client.post(f"{PREFIX}/tasks/{task['id']}/agent", json={"agent_id": agent_c})
    assert response.status_code == 409, response.text
    assert "opencode" in response.json()["message"]

    current = client.get(f"{PREFIX}/tasks/{task['id']}").json()["data"]
    assert current["agent_id"] == agent_a
    assert current["pending_agent_id"] is None


def test_switch_to_ready_cli_agent_takes_effect(client: TestClient) -> None:
    """runtime 已 READY 时切换立即生效：不排队；还没跑过一轮就不落空交接卡。"""
    agent_a = _create_agent(client, "builder")
    agent_d = _create_agent(client, "cli-ready", runtime="opencode")
    _ready_installation(client, "opencode")
    task = _create_task(client, agent_a)

    response = client.post(f"{PREFIX}/tasks/{task['id']}/agent", json={"agent_id": agent_d})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["queued"] is False
    assert data["effective_from"] == "now"
    assert data["agent_id"] == agent_d
    assert data["pending_agent_id"] is None
    assert not [m for m in _messages(client, task["id"]) if m["kind"] == "handoff"]

    events = client.get(f"{PREFIX}/tasks/{task['id']}/events", params={"after_seq": 0})
    types = [item["type"] for item in events.json()["data"]]
    assert "agent_switched" in types


def test_pending_target_disappears_at_finish(client: TestClient) -> None:
    """排队目标在生效前失效：清掉排队、保留原 Agent，并如实落一条说明。"""
    agent_a = _create_agent(client, "builder")
    task = _create_task(client, agent_a)
    container: Container = client.app.state.container

    async def _prepare_and_apply() -> None:
        # 构造「目标 Agent 在本轮期间被删除」：排队里留一个注册表已不存在的 id
        await container.task_repo.set_status(uuid.UUID(task["id"]), TaskStatus.COMPLETED)
        await container.task_repo.set_pending_agent(uuid.UUID(task["id"]), uuid.uuid4())
        current = await container.task_repo.get(uuid.UUID(task["id"]))
        await container.agent_switch.apply_pending(current, "run-test", "completed")

    asyncio.run(_prepare_and_apply())

    current = client.get(f"{PREFIX}/tasks/{task['id']}").json()["data"]
    assert current["agent_id"] == agent_a
    assert current["pending_agent_id"] is None
    failed = [
        m
        for m in _messages(client, task["id"])
        if m["kind"] == "text" and "切换未生效" in m["content"]
    ]
    assert failed, "排队切换失效时必须如实说明原因，不能静默"
    assert not [m for m in _messages(client, task["id"]) if m["kind"] == "handoff"]


def test_sessions_are_isolated_per_agent(client: TestClient) -> None:
    """同一任务、同一 runtime、不同 Agent 不共用外部会话。"""
    container: Container = client.app.state.container
    task_id = uuid.uuid4()
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()

    async def _run() -> tuple[str, str, str]:
        repo = container.runtime_sessions
        await repo.set(task_id, "opencode", "ses-a", agent_a)
        return (
            await repo.get(task_id, "opencode", agent_a),
            await repo.get(task_id, "opencode", agent_b),
            await repo.get(task_id, "opencode", None),
        )

    got_a, got_b, got_legacy = asyncio.run(_run())
    assert got_a == "ses-a"
    assert got_b == ""
    assert got_legacy == ""
