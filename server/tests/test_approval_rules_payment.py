"""支付审批钩子：默认放行，命中支付规则才要求审批。"""

from __future__ import annotations

import uuid

import pytest

from flux.core.approval_rules import evaluator as approval_evaluator
from flux.core.approval_rules.payment import (
    evaluate_payment_approval,
    require_payment_approval_if_needed,
)
from flux.errors import ConflictError


async def _task(container, *, cloud_user_id: str = "1"):
    return await container.task_repo.create(
        task_id=uuid.uuid4(),
        description="支付用例",
        priority=1,
        project_id=None,
        agent_id=None,
        cloud_user_id=cloud_user_id,
    )


async def test_no_payment_rule_proceeds(apply_container) -> None:
    assert (
        await evaluate_payment_approval(
            apply_container, cloud_user_id="1", amount=10_000, currency="CNY"
        )
        is None
    )
    task = await _task(apply_container)
    assert (
        await require_payment_approval_if_needed(
            apply_container, cloud_user_id="1", amount=10_000, task_id=task.id
        )
        is None
    )


async def test_matching_payment_rule_requires_approval(apply_container, monkeypatch) -> None:
    await apply_container.approval_rules.create(
        cloud_user_id="1",
        rule_type="payment_amount_over",
        params={"amount": 500, "currency": "CNY"},
    )
    task = await _task(apply_container)

    match = await evaluate_payment_approval(
        apply_container, cloud_user_id="1", amount=500.01, currency="CNY"
    )
    assert match is not None
    assert match.rule.type == "payment_amount_over"

    monkeypatch.setattr(approval_evaluator, "wait_for_approval", _allow)
    approved = await require_payment_approval_if_needed(
        apply_container,
        cloud_user_id="1",
        amount=500.01,
        currency="CNY",
        task_id=task.id,
    )
    assert approved is not None and approved.rule.id == match.rule.id

    fresh = await apply_container.task_repo.get(task.id)
    record = fresh.decisions[-1]
    assert record["kind"] == "payment_amount"
    assert record["meta"]["approval_rule_id"] == match.rule.id


async def _allow(*args, **kwargs) -> str:
    return "allowed"


async def test_payment_rule_without_a_task_fails_closed(apply_container) -> None:
    await apply_container.approval_rules.create(
        cloud_user_id="1",
        rule_type="payment_amount_over",
        params={"amount": 100, "currency": "CNY"},
    )
    with pytest.raises(ConflictError):
        await require_payment_approval_if_needed(
            apply_container, cloud_user_id="1", amount=101, task_id=None
        )


async def test_currency_mismatch_does_not_match(apply_container) -> None:
    await apply_container.approval_rules.create(
        cloud_user_id="1",
        rule_type="payment_amount_over",
        params={"amount": 100, "currency": "USD"},
    )
    assert (
        await evaluate_payment_approval(
            apply_container, cloud_user_id="1", amount=1000, currency="CNY"
        )
        is None
    )


async def test_payment_rules_are_user_scoped(apply_container) -> None:
    await apply_container.approval_rules.create(
        cloud_user_id="2",
        rule_type="payment_amount_over",
        params={"amount": 100, "currency": "CNY"},
    )
    assert (
        await evaluate_payment_approval(
            apply_container, cloud_user_id="3", amount=1000, currency="CNY"
        )
        is None
    )
