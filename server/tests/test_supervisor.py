"""Supervisor（助理）：事件幂等入库 + 准入判断 + 规划派发（P0 / P1 / P2）。

P0 的验收标准只有两条，测试围绕它们展开：

1. 同一外部事件重复投递 N 次，库里只有一行 trigger、outbox 只有一条；
2. 进程重启后再投同一事件，仍然不产生第二条记录、也不再排动作。

P1 的验收标准：

3. Gate 是纯函数：off / 空内容 / 来源停用 / 类型不匹配 / 忽略名单 / 速率超限 → ignore；
4. 判断层无状态：模型输出不合协议或超时 → unavailable，绝不默认 accept；
5. observe 模式只追加判断、推进状态，绝不创建 ticket、不起 Run。

P2 的验收标准：

6. 规划层只提建议：角色/依赖/层级不合法或模型不可用 → 降级成单工单，绝不派不可执行的计划；
7. 派发以平台事实为准：没有对应角色的 Agent 就写 blocked，绝不跨角色顶替；
8. 工单阶段由 task / assignment / report 派生，任务终态物化成报告（重复对账不刷重复行）；
9. 依赖未完成的工单不派发；停止会连 Run 一起停；重试新建一轮执行而不覆盖历史。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from flux.config import Settings
from flux.container import Container
from flux.core.model_gateway.base import ChatMessage, ChatResult, ModelProviderBase, TokenUsage
from flux.core.model_gateway.router import ModelRouter
from flux.core.supervisor.assess import SupervisorAssessor
from flux.core.supervisor.dispatcher import derive_stage
from flux.core.supervisor.gate import GateContext, evaluate
from flux.core.supervisor.planner import SupervisorPlanner
from flux.enums import AcceptMode, ModelProvider, SupervisorDecision, TicketStage
from flux.main import create_app
from flux.models.supervisor import SupervisorPolicy, SupervisorTicket, SupervisorTrigger
from tests.conftest import create_agent
from tests.fakes import mark_runtime_ready, read_cli_argv, write_fake_claude


def _event(**overrides) -> dict:
    payload = {
        "source_key": "feishu",
        "external_id": "om_abc123",
        "event_type": "message",
        "actor_id": "ou_owner",
        "actor_platform": "feishu",
        "thread_key": "feishu:chat_1",
        "content": "把登录页拆成三个组件",
        "raw": {"message_id": "om_abc123"},
    }
    payload.update(overrides)
    return payload


def test_repeated_delivery_ingests_once(client: TestClient) -> None:
    first = client.post("/api/v1/supervisor/events", json=_event())
    assert first.status_code == 200, first.text
    assert first.json()["metadata"]["created"] is True

    for _ in range(4):
        again = client.post("/api/v1/supervisor/events", json=_event())
        assert again.status_code == 200, again.text
        assert again.json()["metadata"]["created"] is False
        assert again.json()["data"]["id"] == first.json()["data"]["id"]

    observations = client.get("/api/v1/supervisor/observations").json()
    assert observations["metadata"]["count"] == 1
    # 重复投递不能重复排队：否则 P1 会对着同一条消息反复判断/派活
    assert observations["metadata"]["pending_outbox"] == 1


def test_event_survives_restart(settings: Settings, client: TestClient) -> None:
    """重启（换一个 app/container）后再投同一事件，仍命中已有记录。"""
    created = client.post("/api/v1/supervisor/events", json=_event()).json()
    assert created["metadata"]["created"] is True

    with TestClient(create_app(settings)) as restarted:
        replay = restarted.post("/api/v1/supervisor/events", json=_event())
        assert replay.status_code == 200, replay.text
        assert replay.json()["metadata"]["created"] is False
        assert replay.json()["data"]["id"] == created["data"]["id"]

        observations = restarted.get("/api/v1/supervisor/observations").json()
        assert observations["metadata"]["count"] == 1
        assert observations["metadata"]["pending_outbox"] == 1


def test_event_with_unknown_project_is_rejected(client: TestClient) -> None:
    response = client.post("/api/v1/supervisor/events", json=_event(project_id=str(uuid.uuid4())))
    assert response.status_code == 404, response.text


def test_blank_source_key_is_rejected(client: TestClient) -> None:
    response = client.post("/api/v1/supervisor/events", json=_event(source_key=""))
    assert response.status_code == 422, response.text


def test_settings_default_to_observe_and_persist(client: TestClient) -> None:
    current = client.get("/api/v1/supervisor/settings").json()["data"]
    # 未经用户显式授权前绝不自动派活
    assert current["accept_mode"] == "observe"
    assert current["max_tickets_per_thread"] == 6
    assert current["max_ticket_depth"] == 2

    updated = client.put(
        "/api/v1/supervisor/settings",
        json={"accept_mode": "auto", "max_ticket_depth": 1},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["accept_mode"] == "auto"
    assert updated.json()["data"]["max_ticket_depth"] == 1
    # 没给的字段保持原值，不能被一次 PUT 清空
    assert updated.json()["data"]["max_tickets_per_thread"] == 6

    reread = client.get("/api/v1/supervisor/settings").json()["data"]
    assert reread["accept_mode"] == "auto"


def test_invalid_accept_mode_is_rejected(client: TestClient) -> None:
    response = client.put("/api/v1/supervisor/settings", json={"accept_mode": "yolo"})
    assert response.status_code == 422, response.text


async def test_outbox_claim_then_fail_requeues(container: Container) -> None:
    await container.supervisor_repo.ingest(
        source_key="flux_internal",
        external_id="evt-1",
        thread_key="flux:thread-1",
        content="继续",
    )
    claimed = await container.supervisor_outbox.claim(limit=10)
    assert len(claimed) == 1
    assert claimed[0].status == "processing"

    await container.supervisor_outbox.fail(claimed[0].id, "模型超时")
    assert await container.supervisor_outbox.pending_count() == 1

    # 退避期内不再被领取，避免对上游持续施压
    assert await container.supervisor_outbox.claim(limit=10) == []


async def test_outbox_stale_rows_are_requeued(container: Container) -> None:
    await container.supervisor_repo.ingest(
        source_key="flux_internal",
        external_id="evt-2",
        thread_key="flux:thread-2",
    )
    claimed = await container.supervisor_outbox.claim(limit=10)
    assert len(claimed) == 1

    # 进程在 processing 状态崩溃：超时后必须退回 pending，不能永久卡住
    assert await container.supervisor_outbox.requeue_stale(older_than_seconds=0) == 1
    assert await container.supervisor_outbox.pending_count() == 1


@pytest.mark.parametrize("event_type", ["message", "mention", "command"])
def test_dedupe_scope_is_per_event_type(client: TestClient, event_type: str) -> None:
    """不同事件类型是不同事实：同一条消息的 message 与 command 各算一条。"""
    first = client.post("/api/v1/supervisor/events", json=_event(event_type=event_type))
    second = client.post("/api/v1/supervisor/events", json=_event(event_type=event_type))
    assert first.json()["metadata"]["created"] is True
    assert second.json()["metadata"]["created"] is False


# --- P1：Gate（纯函数）---


def test_gate_ignores_off_mode_blank_content_and_disabled_source() -> None:
    assert (
        evaluate(
            GateContext(accept_mode=AcceptMode.OFF),
            event_type="message",
            actor_id="",
            content="做事",
        ).decision
        is SupervisorDecision.IGNORE
    )
    assert (
        evaluate(
            GateContext(accept_mode=AcceptMode.OBSERVE, source_enabled=False),
            event_type="message",
            actor_id="ou_owner",
            content="做事",
        ).decision
        is SupervisorDecision.IGNORE
    )
    assert (
        evaluate(
            GateContext(accept_mode=AcceptMode.OBSERVE),
            event_type="message",
            actor_id="",
            content="   ",
        ).decision
        is SupervisorDecision.IGNORE
    )
    assert (
        evaluate(
            GateContext(accept_mode=AcceptMode.OBSERVE),
            event_type="message",
            actor_id="",
            content="把登录页拆成三个组件",
        ).decision
        is SupervisorDecision.ACCEPT
    )


def test_gate_applies_trigger_types_ignore_actors_and_rate_limit() -> None:
    context = GateContext(
        accept_mode=AcceptMode.OBSERVE,
        trigger_types=("command",),
        rules={"ignore_actors": ["ou_bot"], "max_events_per_window": 2, "window_seconds": 60},
        recent_trigger_count=1,
    )
    # 类型不在策略范围
    assert (
        evaluate(context, event_type="message", actor_id="ou_owner", content="做事").decision
        is SupervisorDecision.IGNORE
    )
    # 忽略名单优先于类型检查
    assert (
        evaluate(context, event_type="command", actor_id="ou_bot", content="做事").decision
        is SupervisorDecision.IGNORE
    )
    # 窗口内事件数超过上限
    over = GateContext(
        accept_mode=AcceptMode.OBSERVE,
        rules={"max_events_per_window": 2, "window_seconds": 60},
        recent_trigger_count=3,
    )
    verdict = evaluate(over, event_type="command", actor_id="ou_owner", content="做事")
    assert verdict.decision is SupervisorDecision.IGNORE
    assert verdict.detail["max_events_per_window"] == 2
    # 规则都在范围内 → 放行
    assert (
        evaluate(context, event_type="command", actor_id="ou_owner", content="做事").decision
        is SupervisorDecision.ACCEPT
    )


# --- P1：Assessment（有模型、无状态）---


class _StubProvider(ModelProviderBase):
    provider = ModelProvider.LOCAL

    def __init__(self, content: str, *, delay: float = 0.0) -> None:
        super().__init__(model_name="stub")
        self.content = content
        self.delay = delay
        self.messages: list[ChatMessage] = []

    def is_configured(self) -> bool:
        return True

    async def chat(self, messages: list[ChatMessage], **kwargs: object) -> ChatResult:
        self.messages = list(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        return ChatResult(
            content=self.content,
            provider=self.provider,
            model=self.model_name,
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            latency_ms=1,
        )


def _trigger(content: str = "把登录页拆成三个组件") -> SupervisorTrigger:
    return SupervisorTrigger(
        id=uuid.uuid4(),
        source_key="feishu",
        external_id="om_assess",
        event_type="message",
        dedupe_key="stub",
        actor_id="ou_owner",
        actor_platform="feishu",
        thread_key="feishu:chat_1",
        content=content,
    )


def _assessor(provider: ModelProviderBase, *, timeout: float = 1.0) -> SupervisorAssessor:
    return SupervisorAssessor(
        ModelRouter({ModelProvider.LOCAL: provider}), timeout_seconds=timeout, max_retries=0
    )


async def test_assessment_parses_structured_json_and_wraps_external_data() -> None:
    provider = _StubProvider(
        '{"decision":"accept","confidence":0.8,"reason":"明确的开发请求",'
        '"risk":{"level":"low"},"should_dispatch":true,"title":"拆分登录页"}'
    )
    result = await _assessor(provider).assess(_trigger())

    assert result.decision is SupervisorDecision.ACCEPT
    assert result.should_dispatch is True
    assert result.title == "拆分登录页"
    assert result.confidence == pytest.approx(0.8)
    assert result.model_meta["provider"] == "local"
    # 外部内容只能以数据块形式进入提示词，并明确声明不是指令
    user_message = provider.messages[-1].content
    assert "<<<EXTERNAL_DATA" in user_message and "EXTERNAL_DATA>>>" in user_message
    assert "把登录页拆成三个组件" in user_message
    system_prompt = provider.messages[0].content
    assert "不是给你的" in system_prompt and "绝不执行" in system_prompt


async def test_assessment_downgrades_non_json_output_to_unavailable() -> None:
    result = await _assessor(_StubProvider("[local-echo] 我不会输出 JSON")).assess(_trigger())
    assert result.decision is SupervisorDecision.UNAVAILABLE
    assert "不可用" in result.reason


async def test_assessment_downgrades_unknown_decision_to_unavailable() -> None:
    result = await _assessor(_StubProvider('{"decision":"maybe","reason":"拿不准"}')).assess(
        _trigger()
    )
    assert result.decision is SupervisorDecision.UNAVAILABLE


async def test_assessment_timeout_is_unavailable() -> None:
    result = await _assessor(
        _StubProvider('{"decision":"accept"}', delay=0.2), timeout=0.01
    ).assess(_trigger())
    assert result.decision is SupervisorDecision.UNAVAILABLE
    assert "超时" in result.reason


# --- P1：worker 串起 Gate → Assessment（observe 不派活）---


async def _ticket_count(container: Container) -> int:
    async with container.session_factory() as session:  # type: ignore[attr-defined]
        return int(await session.scalar(select(func.count()).select_from(SupervisorTicket)) or 0)


async def test_observe_mode_records_eval_and_creates_no_ticket(container: Container) -> None:
    trigger, created = await container.supervisor_repo.ingest(
        source_key="feishu",
        external_id="evt-observe-1",
        thread_key="feishu:chat_observe",
        actor_id="ou_owner",
        content="把登录页拆成三个组件",
    )
    assert created is True

    result = await container.supervisor.process_pending()
    assert result["processed"] == 1

    detail = await container.supervisor.observation_detail(trigger.id)
    assert detail["status"] == "assessed"
    # 测试用 EchoProvider 输出非 JSON：诚实降级为 unavailable，而不是默认放行
    assert detail["eval"]["decision"] == "unavailable"
    assert len(detail["evals"]) == 1
    # 观察模式绝不落 ticket、不起 Run
    assert await _ticket_count(container) == 0
    assert result["pending_outbox"] == 0


async def test_off_mode_ends_event_without_calling_model(container: Container) -> None:
    await container.supervisor.update_settings({"accept_mode": "off"})
    trigger, _ = await container.supervisor_repo.ingest(
        source_key="feishu",
        external_id="evt-off-1",
        thread_key="feishu:chat_off",
        content="把登录页拆成三个组件",
    )

    assert (await container.supervisor.process_pending())["processed"] == 1

    detail = await container.supervisor.observation_detail(trigger.id)
    assert detail["status"] == "ignored"
    # off = 不做判断：不留下 eval 行
    assert detail["evals"] == []


async def test_policy_trigger_types_stop_event_before_model(container: Container) -> None:
    async with container.session_factory() as session:  # type: ignore[attr-defined]
        session.add(
            SupervisorPolicy(
                id=uuid.uuid4(),
                source_key="feishu",
                name="只处理命令",
                trigger_types=["command"],
                rules={},
            )
        )
        await session.commit()

    trigger, _ = await container.supervisor_repo.ingest(
        source_key="feishu",
        external_id="evt-policy-1",
        event_type="message",
        thread_key="feishu:chat_policy",
        content="把登录页拆成三个组件",
    )
    await container.supervisor.process_pending()

    detail = await container.supervisor.observation_detail(trigger.id)
    assert detail["status"] == "ignored"
    assert detail["eval"]["decision"] == "ignore"
    assert "事件类型" in detail["eval"]["reason"]


def test_process_and_manual_reassess_endpoints(client: TestClient) -> None:
    created = client.post("/api/v1/supervisor/events", json=_event(external_id="om_p1")).json()[
        "data"
    ]

    processed = client.post("/api/v1/supervisor/process").json()
    assert processed["data"]["processed"] == 1
    assert processed["data"]["pending_outbox"] == 0

    reassessed = client.post(f"/api/v1/supervisor/observations/{created['id']}/assess")
    assert reassessed.status_code == 200, reassessed.text
    assert reassessed.json()["metadata"]["decision"] == "unavailable"

    detail = client.get(f"/api/v1/supervisor/observations/{created['id']}").json()["data"]
    # 重判只追加，不覆盖旧结论
    assert len(detail["evals"]) == 2
    assert detail["eval"]["decision"] == "unavailable"
    assert detail["status"] == "assessed"


# --- P2：规划层（模型只提建议，平台做硬校验）---

PREFIX = "/api/v1/supervisor"


def _planner(
    provider: ModelProviderBase, *, retries: int = 0, timeout: float = 1.0
) -> SupervisorPlanner:
    return SupervisorPlanner(
        ModelRouter({ModelProvider.LOCAL: provider}),
        timeout_seconds=timeout,
        max_retries=retries,
    )


async def test_planner_accepts_valid_dag_and_keeps_dependency() -> None:
    provider = _StubProvider(
        '{"tickets": ['
        '{"key":"api","title":"先改接口","description":"改登录接口","role":"developer"},'
        '{"key":"ui","title":"再改页面","description":"接新接口","role":"developer",'
        '"depends_on":["api"]}'
        "]}"
    )
    plan = await _planner(provider).plan(
        _trigger(), roles=("developer",), max_tickets=6, max_depth=2
    )

    assert plan.degraded is False
    assert [ticket.key for ticket in plan.tickets] == ["api", "ui"]
    assert plan.tickets[1].depends_on == ("api",)
    assert plan.tickets[1].sequence == 2


@pytest.mark.parametrize(
    ("content", "reason_part"),
    [
        # 角色不在 agents 表里：不能凭模型一句话就造出一个执行者
        (
            '{"tickets": [{"key":"a","title":"做","description":"做","role":"designer"}]}',
            "角色不可用",
        ),
        # 循环依赖：派发层永远等不到终态
        (
            '{"tickets": ['
            '{"key":"a","title":"做","description":"做","role":"developer","depends_on":["b"]},'
            '{"key":"b","title":"做","description":"做","role":"developer","depends_on":["a"]}'
            "]}",
            "循环依赖",
        ),
        # 非 JSON：诚实降级，而不是猜一个计划
        ("[local-echo] 我不会输出 JSON", "JSON"),
        ('{"tickets": []}', "没有 tickets"),
        # 依赖链超过设置上限
        (
            '{"tickets": ['
            '{"key":"a","title":"做","description":"做","role":"developer"},'
            '{"key":"b","title":"做","description":"做","role":"developer","depends_on":["a"]},'
            '{"key":"c","title":"做","description":"做","role":"developer","depends_on":["b"]}'
            "]}",
            "依赖链超过 2 层",
        ),
    ],
)
async def test_planner_degrades_invalid_plan_to_single_ticket(
    content: str, reason_part: str
) -> None:
    plan = await _planner(_StubProvider(content)).plan(
        _trigger(), roles=("developer",), max_depth=2
    )

    assert plan.degraded is True
    assert [ticket.key for ticket in plan.tickets] == ["main"]
    assert "规划层不可用" in plan.reason
    assert reason_part in plan.reason


@pytest.mark.parametrize(
    ("task_status", "assignment_status", "report_status", "expected"),
    [
        # 在途 assignment 优先：重试后必须立刻显示 running，而不是被旧报告盖住
        ("completed", "running", "completed", TicketStage.RUNNING),
        ("running", "running", None, TicketStage.RUNNING),
        ("running", None, None, TicketStage.RUNNING),
        ("completed", "finished", "completed", TicketStage.DONE),
        ("failed", "finished", "failed", TicketStage.STOPPED),
        (None, None, "cancelled", TicketStage.STOPPED),
        ("waiting_for_user_decision", "running", "needs_input", TicketStage.RUNNING),
        (None, "finished", "blocked", TicketStage.WAITING),
        (None, None, "needs_input", TicketStage.WAITING),
        # 没有报告但有任务终态：等待对账，不能算完成
        ("completed", "finished", None, TicketStage.REVIEWING),
        (None, "failed", None, TicketStage.STOPPED),
        (None, None, None, TicketStage.PLANNED),
    ],
)
def test_derive_stage_prioritises_platform_facts(
    task_status: str | None,
    assignment_status: str | None,
    report_status: str | None,
    expected: TicketStage,
) -> None:
    assert (
        derive_stage(
            task_status=task_status,
            assignment_status=assignment_status,
            report_status=report_status,
        )
        is expected
    )


# --- P2：全链路（规划 → 派发 → 真实 Run → 对账）---


@contextmanager
def _supervised(
    settings: Settings,
    tmp_path: Path,
    *,
    hold: bool = False,
) -> Iterator[tuple[TestClient, Path, Container]]:
    """带 fake Claude CLI 的 app：派发时起的是替身进程，不碰真实 Agent。"""
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    log_path = tmp_path / "cli-argv.log"
    sentinel = tmp_path / "release"
    binary = write_fake_claude(tmp_path / "claude", log_path=log_path, sentinel=sentinel, hold=hold)
    enabled = settings.model_copy(
        update={
            "runtime_home": str(tmp_path / "runtime-home"),
            "runtime_workspace": str(tmp_path / "runtime-ws"),
            "workspace_root": str(root),
            "mcp_enabled": True,
            "mcp_url": "http://127.0.0.1:8000/mcp",
        }
    )
    app = create_app(enabled)
    previous = os.environ.get("FLUX_CLAUDE_CLI_BINARY")
    os.environ["FLUX_CLAUDE_CLI_BINARY"] = binary
    try:
        with TestClient(app) as client:
            container = app.state.container
            mark_runtime_ready(container, "claude-code")
            try:
                yield client, log_path, container
            finally:
                sentinel.touch()
    finally:
        if previous is None:
            os.environ.pop("FLUX_CLAUDE_CLI_BINARY", None)
        else:
            os.environ["FLUX_CLAUDE_CLI_BINARY"] = previous


def _ingest(
    client: TestClient,
    *,
    external_id: str = "om_p2",
    thread_key: str = "feishu:chat_p2",
    content: str = "把登录页拆成三个组件",
) -> dict:
    response = client.post(
        f"{PREFIX}/events",
        json={
            "source_key": "feishu",
            "external_id": external_id,
            "event_type": "message",
            "actor_id": "ou_owner",
            "actor_platform": "feishu",
            "thread_key": thread_key,
            "content": content,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _ticket(client: TestClient, ticket_id: str) -> dict:
    response = client.get(f"{PREFIX}/tickets/{ticket_id}")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _wait_ticket(client: TestClient, ticket_id: str, stage: str, *, timeout: float = 10.0) -> dict:
    """轮询工单阶段直到收敛（Run 在后台线程里跑，HTTP 返回时它还没结束）。"""
    deadline = time.monotonic() + timeout
    detail: dict = {}
    while time.monotonic() < deadline:
        detail = _ticket(client, ticket_id)
        if detail["stage"] == stage:
            return detail
        time.sleep(0.02)
    raise AssertionError(f"工单未在 {timeout}s 内进入 {stage}，当前 {detail.get('stage')}")


def test_plan_dispatch_runs_and_reconciles(
    settings: Settings, tmp_path: Path, db_schema: None
) -> None:
    # EchoProvider 输出非 JSON：规划层诚实降级成单工单，但工单仍然可执行
    with _supervised(settings, tmp_path) as (client, log_path, _container):
        create_agent(client, name="dev-1", role="developer")
        trigger = _ingest(client)

        planned = client.post(
            f"{PREFIX}/observations/{trigger['id']}/plan", params={"dispatch": "true"}
        )
        assert planned.status_code == 200, planned.text
        body = planned.json()
        assert body["metadata"]["degraded"] is True
        assert body["metadata"]["tickets"] == 1
        ticket = body["data"]["tickets"][0]
        assert ticket["role"] == "developer"
        assert ticket["degraded"] is True
        assert body["data"]["dispatch"][0]["dispatched"] is True

        done = _wait_ticket(client, ticket["id"], "done")
        assert done["report"]["status"] == "completed"
        assert done["assignment"]["status"] == "finished"
        assert done["task_id"] is not None

        # 任务终态以 Task Engine 为准，工单阶段只是派生
        task = client.get(f"/api/v1/tasks/{done['task_id']}").json()["data"]
        assert task["status"] == "completed"

        # 派发确实起了 Run：替身收到的是工单原文
        runs = read_cli_argv(log_path)
        assert len(runs) == 1
        assert "把登录页拆成三个组件" in runs[0][-1]

        # 重复对账不刷重复报告
        again = _ticket(client, ticket["id"])
        assert len(done["reports"]) == 1
        assert len(again["reports"]) == 1

        overview = client.get(f"{PREFIX}/overview").json()["data"]
        assert overview["counts"]["done"] == 1


def test_planning_same_trigger_twice_keeps_one_tree(
    settings: Settings, tmp_path: Path, db_schema: None
) -> None:
    with _supervised(settings, tmp_path) as (client, _log_path, _container):
        trigger = _ingest(client)

        first = client.post(f"{PREFIX}/observations/{trigger['id']}/plan").json()["data"]
        second = client.post(f"{PREFIX}/observations/{trigger['id']}/plan").json()["data"]

        assert [t["id"] for t in first["tickets"]] == [t["id"] for t in second["tickets"]]
        assert client.get(f"{PREFIX}/tickets").json()["metadata"]["count"] == 1


def test_dispatch_without_matching_agent_records_blocked(
    settings: Settings, tmp_path: Path, db_schema: None
) -> None:
    # 没有任何 Agent 档案：绝不跨角色顶替，也不偷偷挑一个执行者
    with _supervised(settings, tmp_path) as (client, log_path, _container):
        trigger = _ingest(client)
        planned = client.post(
            f"{PREFIX}/observations/{trigger['id']}/plan", params={"dispatch": "true"}
        ).json()

        outcome = planned["data"]["dispatch"][0]
        assert outcome["dispatched"] is False
        assert "没有可执行角色" in outcome["reason"]
        assert read_cli_argv(log_path) == []

        detail = _ticket(client, planned["data"]["tickets"][0]["id"])
        assert detail["stage"] == "waiting"
        assert detail["report"]["status"] == "blocked"


def test_dependency_blocks_dispatch_until_done(
    settings: Settings, tmp_path: Path, db_schema: None
) -> None:
    with _supervised(settings, tmp_path) as (client, _log_path, container):
        create_agent(client, name="dev-1", role="developer")
        container.supervisor_orchestrator._planner = _planner(
            _StubProvider(
                '{"tickets": ['
                '{"key":"api","title":"先改接口","description":"改登录接口","role":"developer"},'
                '{"key":"ui","title":"再改页面","description":"接新接口","role":"developer",'
                '"depends_on":["api"]}'
                "]}"
            )
        )
        trigger = _ingest(client)

        planned = client.post(
            f"{PREFIX}/observations/{trigger['id']}/plan", params={"dispatch": "true"}
        ).json()["data"]
        assert planned["plan"]["degraded"] is False
        tickets = {ticket["plan_key"].rsplit("-", 1)[-1]: ticket for ticket in planned["tickets"]}
        assert set(tickets) == {"api", "ui"}

        # 第一轮只派发无依赖的 api
        assert [outcome["ticket_id"] for outcome in planned["dispatch"]] == [tickets["api"]["id"]]
        assert tickets["ui"]["stage"] == "planned"

        _wait_ticket(client, tickets["api"]["id"], "done")

        # 依赖完成后，第二轮才派发 ui
        again = client.post(f"{PREFIX}/threads/{planned['thread']['id']}/dispatch").json()
        assert again["metadata"]["dispatched"] == 1
        assert _wait_ticket(client, tickets["ui"]["id"], "done")["report"]["status"] == "completed"


def test_stop_cancels_run_and_records_reason(
    settings: Settings, tmp_path: Path, db_schema: None
) -> None:
    with _supervised(settings, tmp_path, hold=True) as (client, _log_path, _c):
        create_agent(client, name="dev-1", role="developer")
        trigger = _ingest(client)
        planned = client.post(
            f"{PREFIX}/observations/{trigger['id']}/plan", params={"dispatch": "true"}
        ).json()["data"]
        ticket = planned["tickets"][0]
        assert planned["dispatch"][0]["dispatched"] is True
        _wait_ticket(client, ticket["id"], "running")

        stopped = client.post(f"{PREFIX}/tickets/{ticket['id']}/stop", json={"note": "需求已取消"})
        assert stopped.status_code == 200, stopped.text

        detail = _ticket(client, ticket["id"])
        assert detail["stage"] == "stopped"
        assert detail["report"]["status"] == "cancelled"
        assert detail["report"]["summary"] == "需求已取消"


def test_retry_after_stop_starts_a_new_round(
    settings: Settings, tmp_path: Path, db_schema: None
) -> None:
    with _supervised(settings, tmp_path, hold=True) as (client, _log_path, _c):
        create_agent(client, name="dev-1", role="developer")
        trigger = _ingest(client)
        ticket = client.post(
            f"{PREFIX}/observations/{trigger['id']}/plan", params={"dispatch": "true"}
        ).json()["data"]["tickets"][0]
        _wait_ticket(client, ticket["id"], "running")
        client.post(f"{PREFIX}/tickets/{ticket['id']}/stop")

        retried = client.post(f"{PREFIX}/tickets/{ticket['id']}/retry")
        assert retried.status_code == 200, retried.text
        assert retried.json()["data"]["dispatched"] is True

        detail = _wait_ticket(client, ticket["id"], "running")
        # 新一轮立即显示 running，旧记录（含 cancelled 报告）保留供回放
        assert len(detail["assignments"]) == 2
        assert detail["report"]["status"] == "cancelled"


def test_auto_mode_dispatches_only_after_accept(
    settings: Settings, tmp_path: Path, db_schema: None
) -> None:
    with _supervised(settings, tmp_path) as (client, _log_path, container):
        create_agent(client, name="dev-1", role="developer")
        # 判断层换成明确 accept：AUTO 自动派活只看这个结论
        container.supervisor_worker._assessor = _assessor(
            _StubProvider(
                '{"decision":"accept","confidence":0.9,"reason":"明确的开发请求",'
                '"risk":{"level":"low"},"should_dispatch":true,"title":"拆分登录页"}'
            )
        )
        updated = client.put(f"{PREFIX}/settings", json={"accept_mode": "auto"})
        assert updated.json()["data"]["accept_mode"] == "auto"

        trigger = _ingest(client)
        processed = client.post(f"{PREFIX}/process").json()["data"]
        assert processed["processed"] == 1

        observation = client.get(f"{PREFIX}/observations/{trigger['id']}").json()["data"]
        assert observation["status"] == "queued"
        assert observation["eval"]["decision"] == "accept"

        tickets = client.get(f"{PREFIX}/tickets").json()["data"]
        assert len(tickets) == 1
        done = _wait_ticket(client, tickets[0]["id"], "done")
        assert done["report"]["status"] == "completed"
