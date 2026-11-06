"""事件总线（主规格 §5.4 事件命名格式 resource.action）。

M0 实现为进程内发布/订阅，接口与生产形态一致；按 §15.7 换 Redis pub/sub 时只替换本模块实现。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from flux.logging import get_logger

logger = get_logger(__name__)

EventHandler = Callable[[str, dict[str, Any]], "Awaitable[None] | None"]

WILDCARD = "*"


class Events:
    """事件名常量（主规格 §5.4 / §17.9 事件命名规范）。"""

    AGENT_STARTED = "agent.started"
    AGENT_COMPLETED = "agent.completed"
    AGENT_STATE_CHANGED = "agent.state_changed"
    #: 本机 Agent 接入状态变化（最终方案 §3.2：DISCOVERED → … → READY）
    AGENT_INSTALLATION_CHANGED = "agent.installation_changed"
    TASK_CREATED = "task.created"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    #: 对话权限模式切换（Plan / Normal / Full）：载荷含 permission_mode 与 previous_mode，
    #: 供多端同步胶囊状态；只影响后续消息，不改写历史。
    PERMISSION_MODE_CHANGED = "task.permission_mode_changed"
    #: 审批闸门事件：发起（审批卡写入对话）与结果（允许/拒绝/超时）各发一次。
    APPROVAL_REQUESTED = "task.approval_requested"
    APPROVAL_RESOLVED = "task.approval_resolved"
    CONNECTOR_EXECUTED = "connector.executed"
    WORKSPACE_CHANGED = "workspace.changed"
    #: Apply 整批成功（上下文闭环）：载荷含 batch_id / files / task_id / project_id /
    #: added_lines / removed_lines / test（真实测试结果），供收尾沉淀使用。
    #: 逐条 applied 事件不带批次信息，沉淀若挂在它上面会把一批写成 N 条。
    WORKSPACE_APPLIED = "workspace.applied"
    #: Apply 崩溃恢复（P0-1）：进程重启/入口懒检查对账完一批中断的 Apply 后发出，
    #: 载荷含 batch_id、终态（recovered / needs_attention）与逐条处理结论。
    APPLY_RECOVERED = "apply.recovered"
    #: 正式回滚（P1-2）：整批/单条 Apply 被撤销、文件还原为改动前时发出，
    #: 载荷含 batch_id、change_ids 与终态。Rollback 不产生任何 git 提交或回执。
    APPLY_ROLLED_BACK = "apply.rolled_back"
    #: 终端事件（Agent Terminal Console §8）：终端窗口与主窗口消费同一套事件，
    #: 载荷即 terminal_events 行（session_id / seq / kind / source / chunk / exit_code）。
    TERMINAL_EVENT = "terminal.event"
    GIT_COMMITTED = "git.committed"
    PROJECT_SCANNED = "project.scanned"
    BRAIN_UPDATED = "project.brain_updated"
    USAGE_RECORDED = "usage.recorded"
    DSH_RUN_STARTED = "dsh.run_started"
    DSH_EVENT = "dsh.event"
    DSH_RUN_COMPLETED = "dsh.run_completed"
    DSH_RUN_FAILED = "dsh.run_failed"
    DSH_RUN_CANCELLED = "dsh.run_cancelled"
    # P2-15：超时（startup / idle / hard）与重启接管导致的终态，单列事件便于前端区分展示
    DSH_RUN_TIMEOUT = "dsh.run_timeout"
    DSH_RUN_INTERRUPTED = "dsh.run_interrupted"
    # MCP 能力面的工具调用（目标架构 §3.2：越权不静默降级，必须留痕）
    MCP_TOOL_CALLED = "mcp.tool_called"
    MCP_TOOL_DENIED = "mcp.tool_denied"
    #: 能力导入（批次③ §5）：首次导入与带 replace 决策的替换各发一次；
    #: 重复导入判定为 unchanged / keep 时不发事件（没有发生变化就不该有"已变更"信号）。
    CAPABILITY_IMPORTED = "capability.imported"
    CAPABILITY_REPLACED = "capability.replaced"


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}
        self._history: list[tuple[str, dict[str, Any]]] = []

    def subscribe(self, event: str, handler: EventHandler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def unsubscribe(self, event: str, handler: EventHandler) -> None:
        if handler in self._handlers.get(event, []):
            self._handlers[event].remove(handler)

    async def publish(self, event: str, payload: dict[str, Any] | None = None) -> int:
        """发布事件，返回被调用的处理器数量。处理器异常只记录，不中断其他订阅者。"""
        body = dict(payload or {})
        self._history.append((event, body))
        handlers = [*self._handlers.get(event, []), *self._handlers.get(WILDCARD, [])]
        for handler in handlers:
            try:
                outcome = handler(event, body)
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception:  # noqa: BLE001 - 单个订阅者失败不应影响主流程
                logger.exception("事件处理器执行失败 event=%s", event)
        return len(handlers)

    @property
    def history(self) -> list[tuple[str, dict[str, Any]]]:
        return list(self._history)

    def clear(self) -> None:
        self._history.clear()
