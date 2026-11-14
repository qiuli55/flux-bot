"""支付审批钩子（预留给未来的支付集成）。

当前仓库没有真实的支付 / 下单 / 扣款调用（`vault` 只存卡信息，不发支付请求），
因此这里提供**可直接接入的一行钩子**，而不是伪造一条支付链路。

未来接入支付时，在真正扣款之前调用：

    from flux.core.approval_rules.payment import require_payment_approval_if_needed

    await require_payment_approval_if_needed(
        container,
        cloud_user_id=order_owner_cloud_user_id,
        amount=order.amount,
        currency=order.currency,
        task_id=task.id,      # 用于把审批卡挂进对应对话；没有 Run/Task 时 fail-closed
        run_id=run_id,
    )

语义：

- 没有 `payment_amount_over` 规则 → 返回 None，直接扣款；
- 规则命中 → 挂审批卡并等待；允许才返回，拒绝 / 超时 / 取消抛异常，**绝不扣款**；
- 命中规则但没有 task_id → `ConflictError`（无法挂卡就 fail-closed）；
- 不做汇率换算：规则币种与支付币种不同即不命中（USD 阈值不会误伤 CNY 支付）。
"""

from __future__ import annotations

import uuid
from typing import Any

from flux.core.approval_rules.evaluator import (
    ACTION_PAYMENT,
    ApprovalAction,
    ApprovalRuleMatch,
    evaluate_approval,
    require_approval_if_rule_matches,
)
from flux.core.task_engine.approvals import APPROVAL_KIND_PAYMENT_AMOUNT

__all__ = [
    "evaluate_payment_approval",
    "require_payment_approval_if_needed",
]


async def evaluate_payment_approval(
    container: Any,
    *,
    cloud_user_id: str | None,
    amount: float,
    currency: str = "CNY",
) -> ApprovalRuleMatch | None:
    """只判定不挂卡：返回命中的支付规则（没有则 None）。"""
    return await evaluate_approval(
        container,
        cloud_user_id=cloud_user_id,
        action=ApprovalAction(
            kind=ACTION_PAYMENT,
            amount=float(amount),
            currency=currency,
        ),
    )


async def require_payment_approval_if_needed(
    container: Any,
    *,
    cloud_user_id: str | None,
    amount: float,
    currency: str = "CNY",
    task_id: uuid.UUID | None = None,
    run_id: str | None = None,
) -> ApprovalRuleMatch | None:
    """支付前的审批闸门；没命中规则返回 None（调用方继续扣款）。"""
    normalized_currency = (currency or "CNY").strip().upper() or "CNY"
    return await require_approval_if_rule_matches(
        container,
        cloud_user_id=cloud_user_id,
        action=ApprovalAction(
            kind=ACTION_PAYMENT,
            amount=float(amount),
            currency=normalized_currency,
        ),
        task_id=task_id,
        run_id=run_id,
        kind=APPROVAL_KIND_PAYMENT_AMOUNT,
        title="支付需要你确认",
        detail=f"准备支付 {amount:g} {normalized_currency}。",
    )
