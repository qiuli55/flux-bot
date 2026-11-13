"""Supervisor（助理）P5：AUTO 续派、watchdog 回收、投机执行。

验收标准：

1. AUTO 模式下上游工单完成后，下游 planned 工单自动续派，不需要人工 POST dispatch；
2. observe 模式绝不自动派活（授权收回后立刻停止）；
3. watchdog 回收卡住的 dispatched 派活，并写 `timed_out` 报告释放并发位；
4. watchdog 不误伤健康的 running（任务未到终态、未超执行上限）；
5. 同一线程里无依赖的兄弟工单可在同一轮并行派出。
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from flux.config import Settings
from flux.container import Container
from flux.core.supervisor.planner import PlannedTicket, TicketPlan
from tests.conftest import create_agent
from tests.fakes import read_cli_argv
from tests.test_supervisor import (
    PREFIX,
    _assessor,
    _ingest,
    _planner,
    _StubProvider,
    _supervised,
    _wait_ticket,
)

#: 判断层明确 accept：AUTO 自动派活只看这个结论
_ACCEPT = (
    '{"decision":"accept","confidence":0.9,"reason":"明确的开发请求",'
    '"risk":{"level":"low"},"should_dispatch":true,"title":"拆分登录页"}'
)

#: 有依赖的两张工单：api 完成前 ui 不能派发
_CHAIN = (
    '{"tickets": ['
    '{"key":"api","title":"先改接口","description":"改登录接口","role":"developer"},'
    '{"key":"ui","title":"再改页面","description":"接新接口","role":"developer",'
    '"depends_on":["api"]}'
    "]}"
)

#: 两张互不依赖的兄弟工单：同一轮可并行
_PARALLEL = (
    '{"tickets": ['
    '{"key":"api","title":"改接口","description":"改登录接口","role":"developer"},'
    '{"key":"ui","title":"改页面","description":"接新接口","role":"developer"}'
    "]}"
)


def _enable_auto(client: TestClient, container: Container, plan: str) -> None:
    container.supervisor_worker._assessor = _assessor(_StubProvider(_ACCEPT))
    container.supervisor_orchestrator._planner = _planner(_StubProvider(plan))
    updated = client.put(f"{PREFIX}/settings", json={"accept_mode": "auto"})
    assert updated.status_code == 200, updated.text


def test_auto_mode_continues_dependency_chain(
    settings: Settings, tmp_path: Path, db_schema: None
) -> None:
    """上游 done 后自动派下游：全程不手动 POST /threads/{id}/dispatch。"""
    with _supervised(settings, tmp_path) as (client, _log_path, container):
        create_agent(client, name="dev-1", role="developer")
        _enable_auto(client, container, _CHAIN)
        _ingest(client, external_id="om_auto_chain", thread_key="feishu:chat_auto_chain")

        processed = client.post(f"{PREFIX}/process").json()["data"]
        assert processed["processed"] == 1

        tickets = {
            ticket["plan_key"].rsplit("-", 1)[-1]: ticket
            for ticket in client.get(f"{PREFIX}/tickets").json()["data"]
        }
        assert set(tickets) == {"api", "ui"}
        _wait_ticket(client, tickets["api"]["id"], "done")

        # 后续调度全部由后台对账里的 auto_dispatch 接手：不经过人工派发
        client.post(f"{PREFIX}/process")
        done = _wait_ticket(client, tickets["ui"]["id"], "done")
        assert done["report"]["status"] == "completed"


def test_observe_mode_never_auto_dispatches(
    settings: Settings, tmp_path: Path, db_schema: None
) -> None:
    """默认 observe：即使已经规划出工单树，也不会自动起 Run。"""
    with _supervised(settings, tmp_path) as (client, log_path, container):
        create_agent(client, name="dev-1", role="developer")
        container.supervisor_orchestrator._planner = _planner(_StubProvider(_CHAIN))
        trigger = _ingest(client, external_id="om_observe", thread_key="feishu:chat_observe")

        planned = client.post(f"{PREFIX}/observations/{trigger['id']}/plan").json()["data"]
        assert {ticket["stage"] for ticket in planned["tickets"]} == {"planned"}

        client.post(f"{PREFIX}/process")
        tickets = client.get(f"{PREFIX}/tickets").json()["data"]
        assert {ticket["stage"] for ticket in tickets} == {"planned"}
        assert read_cli_argv(log_path) == []


def test_independent_tickets_dispatch_in_same_round(
    settings: Settings, tmp_path: Path, db_schema: None
) -> None:
    """投机执行：无依赖兄弟工单受并发位约束，可在同一轮一起跑。"""
    with _supervised(settings, tmp_path, hold=True) as (client, log_path, container):
        create_agent(client, name="dev-1", role="developer")
        container.supervisor_orchestrator._planner = _planner(_StubProvider(_PARALLEL))
        trigger = _ingest(client, external_id="om_parallel", thread_key="feishu:chat_parallel")

        planned = client.post(
            f"{PREFIX}/observations/{trigger['id']}/plan", params={"dispatch": "true"}
        ).json()["data"]
        assert [outcome["dispatched"] for outcome in planned["dispatch"]] == [True, True]

        for ticket in planned["tickets"]:
            _wait_ticket(client, ticket["id"], "running")
        # 两个替身 Run 同时在跑，而不是排队等第一张结束
        deadline = time.monotonic() + 5
        while len(read_cli_argv(log_path)) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(read_cli_argv(log_path)) == 2


def test_watchdog_endpoint_reports_idle(client: TestClient) -> None:
    """手动回收端点：没有在途派活时如实返回 0，不报错。"""
    response = client.post(f"{PREFIX}/watchdog")
    assert response.status_code == 200, response.text
    assert response.json()["metadata"] == {"checked": 0, "expired": 0}


async def _stuck_ticket(repo, *, thread_key: str) -> uuid.UUID:
    thread = await repo.ensure_thread(thread_key=thread_key, source_key="feishu")
    plan = TicketPlan(
        tickets=(
            PlannedTicket(key="main", title="做", description="做", role="developer", sequence=1),
        )
    )
    tickets = await repo.create_tickets(
        thread_id=thread.id, project_id=None, plan=plan, key_prefix="wd"
    )
    return tickets[0].id


async def test_watchdog_expires_stuck_dispatch(container: Container, db_schema: None) -> None:
    """派活后一直没进入执行：回收、写 timed_out 报告、释放并发位。"""
    repo = container.supervisor_repo
    ticket_id = await _stuck_ticket(repo, thread_key="feishu:wd_stuck")
    await repo.create_assignment(ticket_id, status="dispatched")

    container.supervisor_orchestrator._dispatch_timeout_seconds = 0.0
    result = await container.supervisor_orchestrator.watchdog()

    assert result["checked"] == 1
    assert result["expired"] == 1
    assert (await repo.list_assignments(ticket_id))[0].status == "finished"
    reports = await repo.list_reports(ticket_id)
    assert reports[-1].status == "timed_out"


async def test_watchdog_expires_run_over_deadline(container: Container, db_schema: None) -> None:
    """执行超过上限：连 Run / 任务一起收尾，并留下可回放的超时报告。"""
    repo = container.supervisor_repo
    ticket_id = await _stuck_ticket(repo, thread_key="feishu:wd_run")
    task_id = uuid.uuid4()
    await container.task_repo.create(
        task_id=task_id, description="跑", priority=100, project_id=None, agent_id=None
    )
    assignment = await repo.create_assignment(ticket_id, task_id=task_id, status="running")
    await repo.update_assignment(
        assignment.id, started_at=datetime.now(timezone.utc) - timedelta(seconds=120)
    )

    container.supervisor_orchestrator._run_timeout_seconds = 60.0
    result = await container.supervisor_orchestrator.watchdog()

    assert result["expired"] == 1
    task = await container.task_repo.get(task_id)
    assert task.status == "cancelled"
    assert (await repo.list_reports(ticket_id))[-1].status == "timed_out"


async def test_watchdog_leaves_healthy_running_alone(container: Container, db_schema: None) -> None:
    """健康执行中的派活不能被误伤（任务未终态、未超执行上限）。"""
    repo = container.supervisor_repo
    ticket_id = await _stuck_ticket(repo, thread_key="feishu:wd_healthy")
    task_id = uuid.uuid4()
    await container.task_repo.create(
        task_id=task_id, description="跑", priority=100, project_id=None, agent_id=None
    )
    await repo.create_assignment(ticket_id, task_id=task_id, status="running")

    orchestrator = container.supervisor_orchestrator
    # dispatched 租约放开，只有 running 超时才会被回收
    orchestrator._dispatch_timeout_seconds = 0.0
    orchestrator._run_timeout_seconds = 0.0
    result = await orchestrator.watchdog()

    assert result["checked"] == 1
    assert result["expired"] == 0
    assert (await repo.list_assignments(ticket_id))[0].status == "running"
    assert await repo.list_reports(ticket_id) == []
