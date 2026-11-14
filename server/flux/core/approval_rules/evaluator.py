"""审批规则的纯匹配逻辑 + 命中后的审批卡闸门。

默认行为是**不审批**：`evaluate_approval` 找不到规则就返回 None，调用方直接放行。
只有命中该 `cloud_user_id` 的启用规则时才 `raise_approval` / `wait_for_approval`，
复用既有决策点与卡片机制，不另造一套审批系统。

匹配顺序固定为 `created_at` 升序、同时间按 id 升序；返回**第一条**命中的规则。
规则按云用户隔离：调用方必须传入动作真正归属的 `cloud_user_id`，绝不跨用户匹配。

无用户身份时（例如历史任务既没有 task.cloud_user_id 也没有配置主用户）本模块
返回「无规则命中」，由调用方决定是否放行。对会改动他人数据的动作，调用方应传入
可解析的用户；当前 terminal / 工具审批都从 run → task 解析身份，解析不到就没有
规则可匹配——这与「空规则表 = 零审批」一致。
"""

from __future__ import annotations

import fnmatch
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from flux.core.approval_rules.rules import (
    DEFAULT_CURRENCY,
    RULE_TYPE_COMMAND_PATTERN,
    RULE_TYPE_CUSTOM,
    RULE_TYPE_OUTBOUND_MESSAGE,
    RULE_TYPE_PAYMENT_AMOUNT_OVER,
    RULE_TYPE_TASK_MATCH,
    RULE_TYPE_TOOL_MATCH,
)
from flux.core.task_engine.approvals import (
    ApprovalCancelledError,
    ApprovalDeniedError,
    ApprovalTimeoutError,
    raise_approval,
    wait_for_approval,
)
from flux.errors import ConflictError

#: 终端命令动作（命令字符串就是 classify_command 看到的那一条）。
ACTION_TERMINAL_COMMAND = "terminal_command"
#: 通用工具调用（Claude Code 权限桥等）。
ACTION_TOOL = "tool"
#: 任务 / 例程类型动作。
ACTION_TASK = "task"
#: 出站消息（飞书 / 企业微信 / 钉钉）。
ACTION_OUTBOUND_MESSAGE = "outbound_message"
#: 支付动作。
ACTION_PAYMENT = "payment"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_GLOB_META = frozenset("*?[")


@dataclass(frozen=True)
class ApprovalAction:
    """一次待判定的动作；字段按动作类型取用，缺省即「该维度不参与匹配」。"""

    kind: str
    command: str | None = None
    tool_name: str | None = None
    task_kind: str | None = None
    amount: float | None = None
    currency: str | None = None
    channel: str | None = None
    custom: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleSpec:
    """规则的可匹配视图；与 ORM 解耦，便于纯函数测试。"""

    id: str
    type: str
    params: Mapping[str, Any]
    description: str = ""
    enabled: bool = True
    created_at: datetime | None = None
    created_by_kind: str = "user"
    created_by_bot_id: str | None = None


@dataclass(frozen=True)
class ApprovalRuleMatch:
    """命中的规则与一句可读原因（进审批卡 detail / 日志）。"""

    rule: RuleSpec
    reason: str


def _matches_pattern(value: str, pattern: str) -> bool:
    """精确匹配；含 * ? [ 时按 shell 风格 glob 匹配（区分大小写）。"""
    if any(char in pattern for char in _GLOB_META):
        return fnmatch.fnmatchcase(value, pattern)
    return value == pattern


def _order_key(rule: RuleSpec) -> tuple[datetime, str]:
    created = rule.created_at or _EPOCH
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (created, rule.id)


def _match_payment(params: Mapping[str, Any], action: ApprovalAction) -> str | None:
    if action.amount is None:
        return None
    threshold = params.get("amount")
    if not isinstance(threshold, (int, float)):
        return None
    rule_currency = str(params.get("currency") or DEFAULT_CURRENCY).upper()
    action_currency = str(action.currency or DEFAULT_CURRENCY).upper()
    if rule_currency != action_currency:
        # 不做汇率换算：币种不同即不命中，避免把 USD 金额误当成 CNY 阈值。
        return None
    if float(action.amount) > float(threshold):
        return (
            f"支付金额 {action.amount:g} {action_currency} 超过规则阈值 "
            f"{float(threshold):g} {rule_currency}"
        )
    return None


def _match_command(params: Mapping[str, Any], action: ApprovalAction) -> str | None:
    command = (action.command or "").strip()
    if not command:
        return None
    pattern = params.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return None
    style = str(params.get("style") or "glob").lower()
    if style == "regex":
        import re

        try:
            matched = re.search(pattern, command) is not None
        except re.error:  # pragma: no cover - 正常写入前已校验，防御性兜底
            return None
    else:
        matched = _matches_pattern(command, pattern)
    if not matched:
        return None
    style_label = "正则" if style == "regex" else "通配符"
    return f"命令命中{style_label}规则 {pattern!r}"


def _match_tool_or_task(params: Mapping[str, Any], action: ApprovalAction) -> str | None:
    specified = False
    reasons: list[str] = []
    for key, value in (("tool_name", action.tool_name), ("task_kind", action.task_kind)):
        pattern = params.get(key)
        if pattern is None:
            continue
        specified = True
        if value is None or not _matches_pattern(str(value), str(pattern)):
            return None
        reasons.append(f"{key} {value!r} 匹配 {pattern!r}")
    if not specified:
        return None
    return "命中规则：" + "；".join(reasons)


def _match_outbound(params: Mapping[str, Any], action: ApprovalAction) -> str | None:
    if action.kind != ACTION_OUTBOUND_MESSAGE:
        return None
    rule_channel = params.get("channel")
    if rule_channel is None:
        return "规则要求出站消息先经你确认"
    action_channel = (action.channel or "").strip().lower()
    if not action_channel or action_channel != str(rule_channel).lower():
        return None
    return f"规则要求发往 {action_channel} 的出站消息先经你确认"


def _match_custom(params: Mapping[str, Any], action: ApprovalAction) -> str | None:
    key = params.get("key")
    if not isinstance(key, str) or not key:
        return None
    if "value" not in params:
        return None
    custom = action.custom or {}
    if key not in custom:
        # 未知 key 一律不命中：custom 是预留钩子，不能靠猜 key 触发审批。
        return None
    if custom[key] != params.get("value"):
        return None
    return f"自定义字段 {key} 命中规则"


def match_rule(rule: RuleSpec, action: ApprovalAction) -> str | None:
    """单条规则是否命中；返回命中原因，未命中返回 None。"""
    if not rule.enabled:
        return None
    params = rule.params if isinstance(rule.params, Mapping) else {}
    try:
        if rule.type == RULE_TYPE_PAYMENT_AMOUNT_OVER:
            return _match_payment(params, action)
        if rule.type == RULE_TYPE_COMMAND_PATTERN:
            return _match_command(params, action)
        if rule.type in (RULE_TYPE_TOOL_MATCH, RULE_TYPE_TASK_MATCH):
            return _match_tool_or_task(params, action)
        if rule.type == RULE_TYPE_OUTBOUND_MESSAGE:
            return _match_outbound(params, action)
        if rule.type == RULE_TYPE_CUSTOM:
            return _match_custom(params, action)
    except Exception:  # noqa: BLE001 - 坏规则不能拖垮主流程，按不命中处理
        return None
    return None


def evaluate_rules(rules: Sequence[RuleSpec], action: ApprovalAction) -> ApprovalRuleMatch | None:
    """按 created_at / id 升序返回第一条命中的规则；没有则 None。"""
    for rule in sorted((item for item in rules if item.enabled), key=_order_key):
        reason = match_rule(rule, action)
        if reason is not None:
            return ApprovalRuleMatch(rule=rule, reason=reason)
    return None


def rule_spec_from_row(row: Any) -> RuleSpec:
    """把 ORM 行（或任意同名属性对象）转成纯匹配视图。"""
    bot_id = getattr(row, "created_by_bot_id", None)
    return RuleSpec(
        id=str(getattr(row, "id", "")),
        type=str(getattr(row, "type", "")),
        params=dict(getattr(row, "params", None) or {}),
        description=str(getattr(row, "description", "") or ""),
        enabled=bool(getattr(row, "enabled", True)),
        created_at=getattr(row, "created_at", None),
        created_by_kind=str(getattr(row, "created_by_kind", "user") or "user"),
        created_by_bot_id=str(bot_id) if bot_id is not None else None,
    )


async def evaluate_approval(
    container: Any,
    *,
    cloud_user_id: str | None,
    action: ApprovalAction,
) -> ApprovalRuleMatch | None:
    """加载该用户的启用规则并返回第一条命中；没有用户 / 没有规则即 None。"""
    user_id = str(cloud_user_id).strip() if cloud_user_id is not None else ""
    if not user_id:
        return None
    service = getattr(container, "approval_rules", None)
    if service is None:  # pragma: no cover - 装配缺失时保持「默认不审批」
        return None
    rows = await service.list_specs(user_id, enabled_only=True)
    return evaluate_rules(rows, action)


def rule_meta(match: ApprovalRuleMatch) -> dict[str, Any]:
    """审批卡 / 日志用的规则元数据；不含参数原文，只有 id、类型与说明。"""
    return {
        "approval_rule_id": match.rule.id,
        "approval_rule_type": match.rule.type,
        "approval_rule_description": match.rule.description,
        "approval_rule_created_by_kind": match.rule.created_by_kind,
        "approval_rule_created_by_bot_id": match.rule.created_by_bot_id,
    }


async def raise_rule_approval(
    container: Any,
    *,
    match: ApprovalRuleMatch,
    task_id: uuid.UUID | None,
    run_id: str | None,
    kind: str,
    title: str,
    detail: str,
    meta: Mapping[str, Any] | None = None,
    options: list[dict[str, Any]] | None = None,
) -> str:
    """命中规则后挂审批卡并等待；返回 allowed / denied / timeout / cancelled。

    没有可挂卡的 task 时 fail-closed：抛 ConflictError，动作不执行。这保留了
    原先「需要审批但没有 Run」的硬边界，只不过触发条件从分类结果变成规则命中。
    """
    if task_id is None:
        raise ConflictError(
            f"命中了审批规则「{match.rule.description or match.rule.type}」，"
            "但当前操作没有关联的任务 / Run，无法发起审批，已拒绝执行"
        )
    rule_line = match.rule.description or match.reason
    full_detail = f"{detail}\n\n触发规则：{rule_line}" if detail else f"触发规则：{rule_line}"
    record = await raise_approval(
        container,
        task_id=task_id,
        run_id=run_id,
        kind=kind,
        title=title,
        detail=full_detail,
        meta={**(meta or {}), **rule_meta(match), "rule_reason": match.reason},
        options=options,
    )
    return await wait_for_approval(
        container,
        task_id=task_id,
        decision_id=str(record["id"]),
        run_id=run_id,
    )


def ensure_outcome_allowed(match: ApprovalRuleMatch, outcome: str) -> None:
    """把审批结果映射成调用方异常；allowed 直接返回。"""
    if outcome == "allowed":
        return
    if outcome == "denied":
        raise ApprovalDeniedError(f"用户拒绝了这次操作（规则：{match.rule.description}）")
    if outcome == "timeout":
        raise ApprovalTimeoutError("审批超时（10 分钟）：操作未执行。需要时请重新申请。")
    raise ApprovalCancelledError("任务已取消或结束，操作未执行")


async def require_approval_if_rule_matches(
    container: Any,
    *,
    cloud_user_id: str | None,
    action: ApprovalAction,
    task_id: uuid.UUID | None,
    run_id: str | None,
    kind: str,
    title: str,
    detail: str,
    meta: Mapping[str, Any] | None = None,
    options: list[dict[str, Any]] | None = None,
) -> ApprovalRuleMatch | None:
    """命中规则才审批；未命中直接返回 None，调用方继续执行。

    命中但用户拒绝 / 超时 / 取消时抛对应异常，动作由调用方中止。
    """
    match = await evaluate_approval(container, cloud_user_id=cloud_user_id, action=action)
    if match is None:
        return None
    outcome = await raise_rule_approval(
        container,
        match=match,
        task_id=task_id,
        run_id=run_id,
        kind=kind,
        title=title,
        detail=detail,
        meta=meta,
        options=options,
    )
    ensure_outcome_allowed(match, outcome)
    return match


__all__ = [
    "ACTION_OUTBOUND_MESSAGE",
    "ACTION_PAYMENT",
    "ACTION_TASK",
    "ACTION_TERMINAL_COMMAND",
    "ACTION_TOOL",
    "ApprovalAction",
    "ApprovalRuleMatch",
    "RuleSpec",
    "ensure_outcome_allowed",
    "evaluate_approval",
    "evaluate_rules",
    "match_rule",
    "raise_rule_approval",
    "require_approval_if_rule_matches",
    "rule_meta",
    "rule_spec_from_row",
]
