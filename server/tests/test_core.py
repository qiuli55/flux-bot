"""核心层单元测试：状态机、权限、事件、成本、调度、工作流。"""

from __future__ import annotations

import asyncio

import pytest

from flux.core.agent_runtime.lifecycle import assert_transition, can_transition
from flux.core.event.bus import EventBus, Events
from flux.core.model_gateway.base import ModelPricing, TokenUsage
from flux.core.permission_engine.policy import PermissionPolicy
from flux.core.task_engine.scheduler import TaskScheduler
from flux.core.workflow_engine.orchestrator import (
    BugFixWorkflow,
    FeatureDevelopmentWorkflow,
    plan_workflow,
)
from flux.enums import AgentRole, AgentState, Capability, Role
from flux.errors import (
    InvalidTransitionError,
    NotFoundError,
    PermissionDeniedError,
)
from flux.services.cost_service.calculator import compute_cost

# --- 状态机（主规格 §5.1）---


def test_documented_lifecycle_path_is_allowed() -> None:
    """源文档画的主链路必须全部可通过。"""
    path = [
        AgentState.CREATED,
        AgentState.INITIALIZING,
        AgentState.READY,
        AgentState.RUNNING,
        AgentState.WAITING_TOOL,
        AgentState.RUNNING,
        AgentState.REVIEWING,
        AgentState.COMPLETED,
    ]
    for source, target in zip(path, path[1:], strict=False):
        assert can_transition(source, target), f"{source} → {target} 应被允许"


def test_lifecycle_rejects_skipping_states() -> None:
    assert not can_transition(AgentState.CREATED, AgentState.RUNNING)
    with pytest.raises(InvalidTransitionError):
        assert_transition(AgentState.CREATED, AgentState.RUNNING)


def test_agent_can_be_rearmed_and_recovered() -> None:
    """Agent 可复用（裁决 A14）：完成/失败/停止后能回到 READY 或 INITIALIZING。"""
    assert can_transition(AgentState.COMPLETED, AgentState.READY)
    assert can_transition(AgentState.STOPPED, AgentState.READY)
    assert can_transition(AgentState.FAILED, AgentState.INITIALIZING)


# --- 权限（主规格 §5.5 / §14.5）---


def test_admin_holds_every_capability() -> None:
    policy = PermissionPolicy()
    for capability in Capability:
        assert policy.grants(Role.ADMIN, capability)


def test_developer_cannot_deploy_or_read_secrets() -> None:
    policy = PermissionPolicy()
    assert policy.grants(Role.DEVELOPER, Capability.TERMINAL_EXECUTE)
    assert not policy.grants(Role.DEVELOPER, Capability.DEPLOY)
    assert not policy.grants(Role.REVIEWER, Capability.FILE_WRITE)
    with pytest.raises(PermissionDeniedError):
        policy.require(Role.DEVELOPER, Capability.SECRET_ACCESS)


def test_agent_capability_check_is_fail_closed() -> None:
    policy = PermissionPolicy()
    with pytest.raises(PermissionDeniedError):
        policy.require_agent_capability(frozenset(), Capability.FILE_WRITE, agent="dev-1")


# --- 事件总线（主规格 §5.4）---


def test_event_bus_delivers_to_exact_and_wildcard_subscribers() -> None:
    bus = EventBus()
    seen: list[tuple[str, dict[str, object]]] = []

    def handler(event: str, payload: dict[str, object]) -> None:
        seen.append((event, payload))

    bus.subscribe(Events.TASK_CREATED, handler)
    bus.subscribe("*", handler)

    delivered = asyncio.run(bus.publish(Events.TASK_CREATED, {"task_id": "t-1"}))

    assert delivered == 2
    assert seen == [
        (Events.TASK_CREATED, {"task_id": "t-1"}),
        (Events.TASK_CREATED, {"task_id": "t-1"}),
    ]


def test_event_bus_isolates_failing_handler() -> None:
    bus = EventBus()
    received: list[str] = []

    def bad_handler(_: str, __: dict[str, object]) -> None:
        raise RuntimeError("订阅者内部错误")

    bus.subscribe(Events.TASK_FAILED, bad_handler)
    bus.subscribe(Events.TASK_FAILED, lambda _e, _p: received.append("ok"))

    asyncio.run(bus.publish(Events.TASK_FAILED, {}))

    assert received == ["ok"]


# --- 成本（主规格 §15.4）---


def test_cost_is_none_without_configured_pricing() -> None:
    """没有单价就不给数字，绝不用猜测值填充。"""
    assert compute_cost(1000, 1000, ModelPricing(0, 0)) == 0.0

    from flux.core.model_gateway.providers.echo import EchoProvider

    provider = EchoProvider()
    assert provider.calculate_cost(TokenUsage(input_tokens=100, output_tokens=50)) is None


def test_cost_computed_from_pricing() -> None:
    pricing = ModelPricing(input_per_1k=0.5, output_per_1k=1.5)
    assert compute_cost(2000, 1000, pricing) == 2.5


def test_token_usage_total() -> None:
    assert TokenUsage(input_tokens=7, output_tokens=3).total_tokens == 10


# --- 调度器（主规格 §5.1）---


def test_scheduler_pops_by_priority_then_fifo() -> None:
    scheduler = TaskScheduler()
    scheduler.submit("task-low", "低优先级", priority=200)
    scheduler.submit("task-high", "高优先级", priority=10)
    scheduler.submit("task-mid", "中优先级", priority=100)

    assert [t.task_id for t in (scheduler.next(), scheduler.next(), scheduler.next())] == [
        "task-high",
        "task-mid",
        "task-low",
    ]
    assert scheduler.next() is None


def test_scheduler_cancel_skips_task() -> None:
    scheduler = TaskScheduler()
    scheduler.submit("task-a", "甲")
    scheduler.submit("task-b", "乙")
    assert scheduler.cancel("task-a") is True
    assert scheduler.cancel("task-a") is False
    assert scheduler.pending_count == 1
    assert scheduler.next().task_id == "task-b"


def test_scheduler_cancel_unknown_task() -> None:
    with pytest.raises(NotFoundError):
        TaskScheduler().cancel("不存在")


# --- 工作流（主规格 §6.6）---


def test_feature_workflow_order_matches_spec() -> None:
    steps = FeatureDevelopmentWorkflow().plan("实现用户认证")
    assert [s.role for s in steps] == [
        AgentRole.TECH_LEAD,
        AgentRole.ARCHITECT,
        AgentRole.DEVELOPER,
        AgentRole.REVIEWER,
        AgentRole.TESTER,
    ]
    assert [s.order for s in steps] == [1, 2, 3, 4, 5]
    assert "实现用户认证" in steps[0].instruction


def test_bug_fix_escalation_adds_tech_lead() -> None:
    normal = BugFixWorkflow().plan("修复登录崩溃")
    escalated = BugFixWorkflow().plan("修复登录崩溃", escalated=True)
    assert normal[0].role is AgentRole.TESTER
    assert escalated[0].role is AgentRole.TECH_LEAD
    assert len(escalated) == 4


def test_plan_unknown_workflow_raises() -> None:
    with pytest.raises(NotFoundError):
        plan_workflow("不存在的工作流", "x")
