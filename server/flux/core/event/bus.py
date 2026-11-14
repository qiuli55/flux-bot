"""事件总线（主规格 §5.4 事件命名格式 resource.action）。

默认实现是进程内发布/订阅；需要跨进程广播时由 `build_event_bus` 按设置选
`RedisEventBus`（见 flux.core.event.redis_bus）。两者对外契约一致：
subscribe / unsubscribe / publish / history / clear，外加容器生命周期用的
start / aclose（进程内实现是空操作）。测试仍可直接构造 `EventBus()`。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from flux.logging import get_logger

if TYPE_CHECKING:
    from flux.config import Settings

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
    #: 全局审批规则（按云用户）：新增 / 修改 / 删除各发一次。载荷只含 id、归属、
    #: 类型、启用状态与创建者，不含参数原文或任何密钥。
    APPROVAL_RULE_CREATED = "approval_rule.created"
    APPROVAL_RULE_UPDATED = "approval_rule.updated"
    APPROVAL_RULE_DELETED = "approval_rule.deleted"
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
    #: 通用外部 Webhook 的统一事件名；`webhook.<slug>` 由受控前缀族动态放行，
    #: 与 Bot / 例程的 event_triggers 直接对上。
    WEBHOOK_RECEIVED = "webhook.received"
    #: GitHub Webhook 映射到的事件族；其余官方 X-GitHub-Event 按 `github.<event>`
    #: 动态放行（仅限小写字母、数字与下划线）。
    GITHUB_PUSH = "github.push"
    GITHUB_PULL_REQUEST = "github.pull_request"
    GITHUB_ISSUES = "github.issues"
    GITHUB_ISSUE_COMMENT = "github.issue_comment"
    GITHUB_RELEASE = "github.release"
    GITHUB_WORKFLOW_RUN = "github.workflow_run"
    #: IMAP 轮询发现的新邮件；载荷只含账号、目录、UID 与邮件头字段，
    #: 不含正文与凭据。Bot / 例程的 event_triggers 直接订阅这个事件名。
    EMAIL_RECEIVED = "email.received"
    #: Bot 后台子任务（2026-11-13）：起止均发一次，载荷含 bot_id / subtask_id /
    #: parent_task_id / status；父对话里另有 kind=subtask 的消息与 subtask_update 流事件。
    SUBTASK_STARTED = "subtask.started"
    SUBTASK_COMPLETED = "subtask.completed"
    SUBTASK_FAILED = "subtask.failed"
    SUBTASK_CANCELLED = "subtask.cancelled"


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
        return await self._invoke(event, body)

    async def _invoke(self, event: str, body: dict[str, Any]) -> int:
        """调用本进程订阅者；跨进程实现复用这段本地分发逻辑。"""
        handlers = [*self._handlers.get(event, []), *self._handlers.get(WILDCARD, [])]
        for handler in handlers:
            try:
                outcome = handler(event, body)
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception:  # noqa: BLE001 - 单个订阅者失败不应影响主流程
                logger.exception("事件处理器执行失败 event=%s", event)
        return len(handlers)

    async def start(self) -> None:
        """启动总线后台任务。进程内实现无需后台循环，空操作。"""
        return None

    async def aclose(self) -> None:
        """停止总线后台任务并释放连接。进程内实现无资源可释放。"""
        return None

    @property
    def history(self) -> list[tuple[str, dict[str, Any]]]:
        return list(self._history)

    def clear(self) -> None:
        self._history.clear()


def build_event_bus(settings: Settings) -> EventBus:
    """按设置构建事件总线。

    `event_bus_backend` 默认 "memory"；只有显式设为 "redis" 才换成 Redis pub/sub。
    这样 redis_url 有默认 localhost 值也不会让本地测试或没装 Redis 的机器失效。
    未知取值一律回退到进程内实现并告警（fail-safe，不让配置笔误拖垮启动）。
    """
    backend = (getattr(settings, "event_bus_backend", None) or "memory").strip().lower()
    if backend == "redis":
        # 延迟导入：进程内后端（默认）完全不加载 redis 客户端。
        from flux.core.event.redis_bus import DEFAULT_CHANNEL, RedisEventBus

        channel = (
            getattr(settings, "event_bus_redis_channel", None) or ""
        ).strip() or DEFAULT_CHANNEL
        return RedisEventBus(settings.redis_url, channel=channel)
    if backend != "memory":
        logger.warning("未知的事件总线后端 %r，回退到进程内实现", backend)
    return EventBus()
