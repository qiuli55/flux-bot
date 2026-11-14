"""全局审批规则：默认零审批，命中用户规则才挂审批卡。"""

from __future__ import annotations

from flux.core.approval_rules.evaluator import (
    ACTION_OUTBOUND_MESSAGE,
    ACTION_PAYMENT,
    ACTION_TASK,
    ACTION_TERMINAL_COMMAND,
    ACTION_TOOL,
    ApprovalAction,
    ApprovalRuleMatch,
    RuleSpec,
    ensure_outcome_allowed,
    evaluate_approval,
    evaluate_rules,
    match_rule,
    raise_rule_approval,
    require_approval_if_rule_matches,
)
from flux.core.approval_rules.payment import (
    evaluate_payment_approval,
    require_payment_approval_if_needed,
)

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
    "evaluate_payment_approval",
    "evaluate_rules",
    "match_rule",
    "raise_rule_approval",
    "require_approval_if_rule_matches",
    "require_payment_approval_if_needed",
]
