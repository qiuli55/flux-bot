"""Supervisor 的准入 Gate（纯函数，0 次模型调用）。

Gate 只回答"这条事件要不要进入判断层"，不读库、不写库、不调模型：
输入是几项已经结构化的事实，输出是一个可回放的结论。所有"为什么跳过"
都必须给出机器可读的理由，方便观察期核对判断层是否被垃圾流量淹没。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from flux.enums import AcceptMode, SupervisorDecision

__all__ = ["GateContext", "GateVerdict", "evaluate", "select_policy"]

#: policy 里可用的硬规则键（未知键忽略，避免配置错误直接拒绝所有事件）
_RULE_IGNORE_ACTORS = "ignore_actors"
_RULE_MAX_EVENTS = "max_events_per_window"
_RULE_WINDOW_SECONDS = "window_seconds"


@dataclass(frozen=True)
class GateContext:
    """Gate 需要的全部事实（全部来自结构化字段，不做自由文本推测）。"""

    accept_mode: AcceptMode
    source_enabled: bool = True
    #: 生效策略限定的事件类型；空 = 不限制
    trigger_types: tuple[str, ...] = ()
    rules: dict[str, Any] = field(default_factory=dict)
    #: 窗口内已收到的事件数（含当前这条）
    recent_trigger_count: int = 0


@dataclass(frozen=True)
class GateVerdict:
    """Gate 结论：`accept` 才继续走 Assessment，其余一律终结。"""

    decision: SupervisorDecision
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


def evaluate(
    context: GateContext,
    *,
    event_type: str,
    actor_id: str,
    content: str,
) -> GateVerdict:
    """按固定顺序逐条检查；第一条不通过的规则即为终局结论。"""
    if context.accept_mode is AcceptMode.OFF:
        return GateVerdict(SupervisorDecision.IGNORE, "准入模式为 off")
    if not context.source_enabled:
        return GateVerdict(SupervisorDecision.IGNORE, "来源已停用")
    if not content.strip():
        return GateVerdict(SupervisorDecision.IGNORE, "事件内容为空")

    if context.trigger_types and event_type not in context.trigger_types:
        return GateVerdict(
            SupervisorDecision.IGNORE,
            f"事件类型 {event_type} 不在策略范围内",
            {"event_type": event_type, "trigger_types": list(context.trigger_types)},
        )

    ignored_actors = context.rules.get(_RULE_IGNORE_ACTORS) or []
    if actor_id and actor_id in ignored_actors:
        return GateVerdict(
            SupervisorDecision.IGNORE,
            "说话人在忽略名单内",
            {"actor_id": actor_id},
        )

    max_events = _positive_int(context.rules.get(_RULE_MAX_EVENTS))
    window_seconds = _positive_int(context.rules.get(_RULE_WINDOW_SECONDS))
    if max_events is not None and context.recent_trigger_count > max_events:
        return GateVerdict(
            SupervisorDecision.IGNORE,
            f"{window_seconds or '?'} 秒内事件数超过 {max_events} 条",
            {
                "recent_trigger_count": context.recent_trigger_count,
                "max_events_per_window": max_events,
                "window_seconds": window_seconds,
            },
        )

    return GateVerdict(SupervisorDecision.ACCEPT, "通过准入")


def select_policy(
    policies: list[Any],
    *,
    source_key: str,
    project_id: object | None,
) -> Any | None:
    """从候选策略里挑最具体的一条。

    具体程度：项目 + 来源 > 项目 > 来源 > 全局。同分时按创建顺序取先到者，
    保证同样的输入永远得到同样的策略（判断可回放）。
    """
    best: Any | None = None
    best_score = -1
    for policy in policies:
        if getattr(policy, "enabled", True) is False:
            continue
        score = 0
        if getattr(policy, "project_id", None) is not None:
            if project_id is None or str(policy.project_id) != str(project_id):
                continue
            score += 2
        if getattr(policy, "source_key", None) is not None:
            if str(policy.source_key) != source_key:
                continue
            score += 1
        if score > best_score:
            best, best_score = policy, score
    return best


def _positive_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
