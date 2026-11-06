"""逐次审批闸门（2026-10-25 三模式）。

`terminal.execute` 在执行前调用 `classify_command`；命中高风险（正常模式）或受保护对象
（任何模式）时，必须先把审批卡写进对话、把任务置为 `waiting_for_user_decision`，再阻塞
等待用户点「允许 / 拒绝」。审批复用既有决策点机制（`tasks.decisions` + `decision` 消息 +
手机端决策卡 + `POST /tasks/{id}/decisions/choose`），不另造一套消息与接口。

三条边界：

- 审批记录**强制 pending**，不受任务 `decision_mode=auto` 影响——自动模式也不能替用户
  批准高风险命令；
- 等待最多 10 分钟：超时把记录置 `expired`、任务回到 `running`，命令**不执行**，工具返回
  明确错误让 Agent 重新征询；
- Run 已取消/任务已终态时立即退出，命令同样不执行（用户停掉的任务不该继续改文件）。

等待期间不持有数据库事务，只做 500ms 轮询。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from flux.core.task_engine.decisions import build_decision
from flux.core.task_engine.repository import MessageDraft
from flux.enums import DecisionMode, DecisionStatus, DshRunStatus, TaskMessageRole, TaskStatus
from flux.errors import AIOSError, PermissionDeniedError
from flux.logging import get_logger

logger = get_logger(__name__)

APPROVAL_KIND_HIGH_RISK = "high_risk_command"
APPROVAL_KIND_PROTECTED = "protected_access"

#: 审批卡上的两个选项（label 就是回传给 choose 接口的值）
APPROVAL_ALLOW = "允许"
APPROVAL_DENY = "拒绝"

#: 等用户点击的上限（与计划一致：10 分钟）
APPROVAL_TIMEOUT_SECONDS = 600
_POLL_INTERVAL_SECONDS = 0.5

_FINAL_TASK_STATUSES = {
    TaskStatus.COMPLETED.value,
    TaskStatus.FAILED.value,
    TaskStatus.CANCELLED.value,
}
_FINAL_RUN_STATUSES = {
    DshRunStatus.COMPLETED.value,
    DshRunStatus.FAILED.value,
    DshRunStatus.TIMEOUT.value,
    DshRunStatus.CANCELLED.value,
    DshRunStatus.INTERRUPTED.value,
}


class ApprovalDeniedError(PermissionDeniedError):
    """用户在审批卡上点了「拒绝」：命令不执行。"""

    code = "approval_denied"


class ApprovalTimeoutError(AIOSError):
    """审批超时（10 分钟无人响应）：命令不执行，Agent 需重新征询。"""

    code = "approval_timeout"
    http_status = 408


class ApprovalCancelledError(AIOSError):
    """等待审批期间 Run 被取消/任务进入终态：命令不执行。"""

    code = "approval_cancelled"
    http_status = 409


async def raise_approval(
    container: Any,
    *,
    task_id: uuid.UUID,
    run_id: str | None,
    kind: str,
    title: str,
    detail: str,
    meta: dict[str, Any],
) -> dict[str, Any]:
    """把一条审批请求写成强制 pending 的决策记录 + 对话消息，返回记录本身。"""
    record = build_decision(
        question=title,
        options=[
            {
                "label": APPROVAL_ALLOW,
                "description": "仅本次有效，执行完即失效",
                "impact": "允许后该命令会立刻在终端会话中执行",
                "recommended": False,
            },
            {
                "label": APPROVAL_DENY,
                "description": "默认建议：先拒绝，让 Agent 说明为什么非做不可",
                "impact": "拒绝后命令不会执行，Agent 会收到明确错误",
                "recommended": True,
            },
        ],
        context=detail,
        recommendation=None,
        # MANUAL 才能保证 pending；auto 模式也不允许代替用户批准
        mode=DecisionMode.MANUAL,
    )
    record = {
        **record,
        "kind": kind,
        "meta": {**meta, "task_id": str(task_id), "run_id": run_id},
    }
    await container.task_repo.raise_decision(task_id, record)
    await container.task_repo.set_status(task_id, TaskStatus.WAITING_FOR_USER_DECISION)

    message = (
        await container.task_repo.add_messages(
            task_id,
            [
                MessageDraft(
                    role=TaskMessageRole.ASSISTANT.value,
                    kind="decision",
                    content=title,
                    payload={"decision": record},
                )
            ],
        )
    )[0]
    await container.task_stream.publish(
        task_id, "assistant_message", {"message": message.to_dict()}
    )
    await container.bus.publish(
        "task.approval_requested",
        {
            "task_id": str(task_id),
            "run_id": run_id,
            "decision_id": record["id"],
            "kind": kind,
            "meta": record["meta"],
        },
    )
    logger.info(
        "approval.raised task=%s run=%s kind=%s rule=%s decision=%s",
        task_id,
        run_id,
        kind,
        meta.get("rule"),
        record["id"],
    )
    return record


async def wait_for_approval(
    container: Any,
    *,
    task_id: uuid.UUID,
    decision_id: str,
    run_id: str | None,
    timeout_seconds: float = APPROVAL_TIMEOUT_SECONDS,
) -> str:
    """阻塞等待用户决定；返回 allowed / denied / timeout / cancelled。"""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        task = await container.task_repo.get(task_id)
        record = _find_record(task.decisions, decision_id)
        if record is not None:
            status = str(record.get("status") or "")
            if status == DecisionStatus.RESOLVED.value:
                allowed = record.get("chosen") == APPROVAL_ALLOW
                logger.info(
                    "approval.resolved task=%s decision=%s chosen=%s",
                    task_id,
                    decision_id,
                    record.get("chosen"),
                )
                return "allowed" if allowed else "denied"
            if status in (DecisionStatus.REJECTED.value, DecisionStatus.EXPIRED.value):
                logger.info(
                    "approval.resolved task=%s decision=%s status=%s", task_id, decision_id, status
                )
                return "denied" if status == DecisionStatus.REJECTED.value else "timeout"

        if task.status in _FINAL_TASK_STATUSES:
            logger.info(
                "approval.cancelled task=%s decision=%s reason=task_final", task_id, decision_id
            )
            return "cancelled"
        if run_id:
            run = await container.run_repo.get(run_id)
            if run is not None and run.status in _FINAL_RUN_STATUSES:
                logger.info(
                    "approval.cancelled task=%s run=%s status=%s", task_id, run_id, run.status
                )
                return "cancelled"

        if asyncio.get_running_loop().time() >= deadline:
            await _expire(container, task_id=task_id, decision_id=decision_id, run_id=run_id)
            return "timeout"
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def _expire(
    container: Any, *, task_id: uuid.UUID, decision_id: str, run_id: str | None
) -> None:
    """超时收尾：记录置 expired、任务恢复 running、写一条结果消息让卡片显示「已超时」。"""
    now = datetime.now(timezone.utc).isoformat()
    task, record = await container.task_repo.resolve_decision(
        task_id,
        decision_id,
        status=DecisionStatus.EXPIRED,
        chosen=None,
        note="审批超时（10 分钟），命令未执行",
        resolved_at=now,
    )
    if task.status == TaskStatus.WAITING_FOR_USER_DECISION.value:
        await container.task_repo.set_status(task_id, TaskStatus.RUNNING)
    message = (
        await container.task_repo.add_messages(
            task_id,
            [
                MessageDraft(
                    role=TaskMessageRole.ASSISTANT.value,
                    kind="decision_result",
                    content="审批超时（10 分钟）：命令未执行。需要时我会重新征询。",
                    payload={"decision": record, "expired": True},
                )
            ],
        )
    )[0]
    await container.task_stream.publish(
        task_id, "assistant_message", {"message": message.to_dict()}
    )
    await container.bus.publish(
        "task.approval_resolved",
        {
            "task_id": str(task_id),
            "run_id": run_id,
            "decision_id": decision_id,
            "result": "timeout",
        },
    )
    logger.warning("approval.timeout task=%s run=%s decision=%s", task_id, run_id, decision_id)


def _find_record(records: Any, decision_id: str) -> dict[str, Any] | None:
    for record in records or []:
        if isinstance(record, dict) and record.get("id") == decision_id:
            return record
    return None


__all__ = [
    "APPROVAL_ALLOW",
    "APPROVAL_DENY",
    "APPROVAL_KIND_HIGH_RISK",
    "APPROVAL_KIND_PROTECTED",
    "APPROVAL_TIMEOUT_SECONDS",
    "ApprovalCancelledError",
    "ApprovalDeniedError",
    "ApprovalTimeoutError",
    "raise_approval",
    "wait_for_approval",
]
