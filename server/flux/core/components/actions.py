"""组件动作回调的定位与服务端路由。

客户端只回传 `action_id` 与用户填的值；服务端**从已落库的组件树里**找回动作，
因此调用方无法自造回调 URL、decision_id 或内部动作名。允许的服务端动作只有
schema 里列出的那三种：

- `decision.choose`     → 选中待决策项的一个候选方案
- `decision.reject`     → 拒绝全部候选方案
- `secure_input.submit` → 把安全输入值交给密钥库（明文不落消息表）

客户端动作（`open_desktop` / `open_url`）不经过这里：它们不需要服务端参与。
"""

from __future__ import annotations

import uuid
from typing import Any

from flux.core.components.mapping import component_payload
from flux.core.event.bus import Events
from flux.core.task_engine.decisions import reject_decision, resolve_decision
from flux.core.task_engine.repository import MessageDraft
from flux.core.task_engine.secure_input import store_secure_input
from flux.enums import DecisionStatus, TaskMessageRole, TaskStatus
from flux.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from flux.models.task import Task, TaskMessage

Action = dict[str, Any]


def component_node(message: TaskMessage) -> dict[str, Any] | None:
    """取一条消息的组件树（kind=component 或读取时附带的 component）。"""
    payload = component_payload(message.kind, message.content, message.payload, str(message.id))
    if not isinstance(payload, dict):
        return None
    node = payload.get("component")
    return node if isinstance(node, dict) else None


def find_action(message: TaskMessage, action_id: str) -> Action | None:
    """在该消息的组件树里按 action_id 找动作；不存在返回 None。"""
    return _find_action(component_node(message), action_id)


def _find_action(node: Any, action_id: str) -> Action | None:
    if isinstance(node, dict):
        action = node.get("action")
        if isinstance(action, dict) and action.get("action_id") == action_id:
            return action
        for value in node.values():
            found = _find_action(value, action_id)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_action(item, action_id)
            if found is not None:
                return found
    return None


async def route_component_action(
    container: Any,
    *,
    task: Task,
    message: TaskMessage,
    action: Action,
    value: str | None,
    option: str | None,
    note: str | None,
    cloud_user_id: str | None,
) -> dict[str, Any]:
    """执行一个服务端动作；返回给调用方的结构化结果。"""
    if action.get("type") != "server":
        raise ValidationError("该动作由客户端处理，不需要提交到服务端")
    name = action.get("name")
    if name in {"decision.choose", "decision.reject"}:
        return await _route_decision(
            container,
            task=task,
            action=action,
            option=option,
            note=note,
            cloud_user_id=cloud_user_id,
        )
    if name == "secure_input.submit":
        return await _route_secure_input(container, task=task, card=message, value=value)
    raise NotFoundError("操作不存在或已失效", details={"action_id": action.get("action_id")})


async def _route_decision(
    container: Any,
    *,
    task: Task,
    action: Action,
    option: str | None,
    note: str | None,
    cloud_user_id: str | None,
) -> dict[str, Any]:
    name = str(action.get("name"))
    pending = task.pending_decision()
    if pending is None:
        raise ConflictError("当前没有等待你决策的问题", details={"task_id": str(task.id)})
    decision_id = str(action.get("decision_id") or "")
    if pending.get("id") != decision_id:
        raise NotFoundError(
            "这条操作已失效，请刷新对话后重试",
            details={"decision_id": decision_id, "pending": pending.get("id")},
        )
    await _require_outbound_decision_owned(container, pending, cloud_user_id)

    if name == "decision.choose":
        chosen = (option or str(action.get("option") or "")).strip()
        if not chosen:
            raise BadRequestError("请选择一个候选方案", details={"field": "option"})
        record = resolve_decision(pending, option=chosen, note=note)
        task, _ = await container.task_repo.resolve_decision(
            task.id,
            decision_id,
            status=DecisionStatus.RESOLVED,
            chosen=chosen,
            note=note,
            resolved_at=str(record["resolved_at"]),
        )
        content = f"你的选择：{chosen}" + (f"（{note}）" if note else "")
    else:
        record = reject_decision(pending, note=note)
        task, _ = await container.task_repo.resolve_decision(
            task.id,
            decision_id,
            status=DecisionStatus.REJECTED,
            chosen=None,
            note=note,
            resolved_at=str(record["resolved_at"]),
        )
        content = f"你拒绝了全部候选方案：{note or '请 Agent 重新给出方案'}"

    task = await container.task_repo.set_status(task.id, TaskStatus.RUNNING)
    message = (
        await container.task_repo.add_messages(
            task.id,
            [
                MessageDraft(
                    role=TaskMessageRole.USER.value,
                    kind="decision_result",
                    content=content,
                    payload={"decision": record},
                )
            ],
        )
    )[0]
    await container.bus.publish(
        Events.APPROVAL_RESOLVED,
        {
            "task_id": str(task.id),
            "decision_id": decision_id,
            "result": "allowed" if record.get("chosen") else "denied",
            "chosen": record.get("chosen"),
            "kind": pending.get("kind"),
            "meta": pending.get("meta") or {},
        },
    )
    return {
        "action": {"name": name, "action_id": action.get("action_id")},
        "decision": record,
        "message": message.to_dict(),
        "task": task.to_dict(),
    }


async def _route_secure_input(
    container: Any, *, task: Task, card: TaskMessage, value: str | None
) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("密钥不能为空", details={"field": "value"})
    result = await store_secure_input(container, task=task, card=card, secret=value)
    return {
        "action": {"name": "secure_input.submit", "action_id": f"secure_input:{card.id}"},
        "key": result["key"].to_dict(),
        "message": result["message"].to_dict(),
    }


async def _require_outbound_decision_owned(
    container: Any, pending: dict[str, Any], cloud_user_id: str | None
) -> None:
    """出站审批卡里带 draft_id 时，先确认草稿属于当前登录人（与 REST 口径一致）。"""
    kind = str(pending.get("kind") or "")
    if not kind.endswith("_outbound"):
        return
    meta = pending.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    raw_draft_id = str(meta.get("draft_id") or "").strip()
    if not raw_draft_id:
        return
    try:
        draft_id = uuid.UUID(raw_draft_id)
    except ValueError as exc:
        raise NotFoundError(
            f"出站草稿 {raw_draft_id} 不存在", details={"draft_id": raw_draft_id}
        ) from exc
    if cloud_user_id is None:
        raise NotFoundError(f"出站草稿 {raw_draft_id} 不存在", details={"draft_id": raw_draft_id})
    await container.outbound_drafts.get(draft_id, cloud_user_id=cloud_user_id)


__all__ = ["component_node", "find_action", "route_component_action"]
