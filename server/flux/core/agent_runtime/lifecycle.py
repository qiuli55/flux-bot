"""Agent 生命周期状态机（主规格 §5.1）。

状态：CREATED → INITIALIZING → READY → RUNNING → WAITING_TOOL → REVIEWING → COMPLETED / FAILED

与源文档的差异：源状态机把 COMPLETED / FAILED 画成终态，但 Agent 是可复用实体
（§5.1 恢复策略要求重试与降级），因此本实现允许 COMPLETED / FAILED 回到 READY（重新待命）
或 FAILED → INITIALIZING（恢复重试）。该偏差已记录在主规格附录 A（裁决 A14）。
"""

from __future__ import annotations

from flux.enums import AgentState
from flux.errors import InvalidTransitionError

ALLOWED_TRANSITIONS: dict[AgentState, frozenset[AgentState]] = {
    AgentState.CREATED: frozenset({AgentState.INITIALIZING, AgentState.FAILED}),
    AgentState.INITIALIZING: frozenset({AgentState.READY, AgentState.FAILED}),
    AgentState.READY: frozenset({AgentState.RUNNING, AgentState.STOPPED, AgentState.FAILED}),
    AgentState.RUNNING: frozenset(
        {
            AgentState.WAITING_TOOL,
            AgentState.REVIEWING,
            AgentState.COMPLETED,
            AgentState.STOPPED,
            AgentState.FAILED,
        }
    ),
    AgentState.WAITING_TOOL: frozenset({AgentState.RUNNING, AgentState.STOPPED, AgentState.FAILED}),
    AgentState.REVIEWING: frozenset(
        {AgentState.RUNNING, AgentState.COMPLETED, AgentState.STOPPED, AgentState.FAILED}
    ),
    AgentState.COMPLETED: frozenset({AgentState.READY}),
    AgentState.FAILED: frozenset({AgentState.READY, AgentState.INITIALIZING}),
    AgentState.STOPPED: frozenset({AgentState.INITIALIZING, AgentState.READY}),
}


def allowed_from(state: AgentState) -> frozenset[AgentState]:
    return ALLOWED_TRANSITIONS.get(state, frozenset())


def can_transition(source: AgentState, target: AgentState) -> bool:
    return target in allowed_from(source)


def assert_transition(source: AgentState, target: AgentState) -> None:
    if not can_transition(source, target):
        raise InvalidTransitionError(
            f"非法状态跃迁：{source} → {target}",
            details={"from": str(source), "to": str(target)},
        )
