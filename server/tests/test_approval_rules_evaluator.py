"""审批规则：参数校验与纯匹配逻辑（不碰数据库 / 不挂真实审批卡）。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from flux.core.approval_rules import evaluator
from flux.core.approval_rules.evaluator import (
    ACTION_OUTBOUND_MESSAGE,
    ACTION_PAYMENT,
    ACTION_TASK,
    ACTION_TERMINAL_COMMAND,
    ACTION_TOOL,
    ApprovalAction,
    RuleSpec,
    evaluate_approval,
    evaluate_rules,
    match_rule,
)
from flux.core.approval_rules.rules import (
    RULE_TYPE_COMMAND_PATTERN,
    RULE_TYPE_CUSTOM,
    RULE_TYPE_OUTBOUND_MESSAGE,
    RULE_TYPE_PAYMENT_AMOUNT_OVER,
    RULE_TYPE_TASK_MATCH,
    RULE_TYPE_TOOL_MATCH,
    default_description,
    normalize_rule_params,
)
from flux.errors import BadRequestError, ConflictError

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _rule(
    rule_type: str,
    params: dict[str, Any],
    *,
    rule_id: str | None = None,
    enabled: bool = True,
    created_at: datetime | None = None,
    description: str = "",
) -> RuleSpec:
    return RuleSpec(
        id=rule_id or uuid.uuid4().hex,
        type=rule_type,
        params=params,
        description=description,
        enabled=enabled,
        created_at=created_at or BASE,
    )


def _action(**kwargs: Any) -> ApprovalAction:
    return ApprovalAction(**kwargs)


# ---------------------------------------------------------------- 参数校验


def test_payment_params_default_currency_and_numeric_string() -> None:
    assert normalize_rule_params(RULE_TYPE_PAYMENT_AMOUNT_OVER, {"amount": "500"}) == {
        "amount": 500.0,
        "currency": "CNY",
    }
    assert normalize_rule_params(
        RULE_TYPE_PAYMENT_AMOUNT_OVER, {"amount": 12.5, "currency": "usd"}
    ) == {"amount": 12.5, "currency": "USD"}


@pytest.mark.parametrize("amount", [-1, "abc", True, None, float("inf")])
def test_payment_rejects_bad_amount(amount: Any) -> None:
    with pytest.raises(BadRequestError):
        normalize_rule_params(RULE_TYPE_PAYMENT_AMOUNT_OVER, {"amount": amount})


def test_command_pattern_defaults_to_glob_and_normalizes() -> None:
    assert normalize_rule_params(RULE_TYPE_COMMAND_PATTERN, {"pattern": " rm * "}) == {
        "pattern": "rm *",
        "style": "glob",
    }


@pytest.mark.parametrize(
    "pattern",
    [
        "rm (",
        "(" + "a+" * 31 + ")",
        "(a+)+$",
        "(.*)*",
        r"(a)\1",
        "a" * 201,
    ],
)
def test_unsafe_regex_is_rejected(pattern: str) -> None:
    with pytest.raises(BadRequestError):
        normalize_rule_params(RULE_TYPE_COMMAND_PATTERN, {"pattern": pattern, "style": "regex"})


def test_safe_regex_is_accepted() -> None:
    params = normalize_rule_params(
        RULE_TYPE_COMMAND_PATTERN, {"pattern": r"^rm\s+-rf\s+/tmp/", "style": "regex"}
    )
    assert params["style"] == "regex"


def test_tool_match_requires_a_field() -> None:
    with pytest.raises(BadRequestError):
        normalize_rule_params(RULE_TYPE_TOOL_MATCH, {})
    assert normalize_rule_params(RULE_TYPE_TOOL_MATCH, {"tool_name": "terminal.execute"}) == {
        "tool_name": "terminal.execute"
    }
    assert normalize_rule_params(RULE_TYPE_TASK_MATCH, {"task_kind": "routine"}) == {
        "task_kind": "routine"
    }


def test_outbound_channel_filter() -> None:
    assert normalize_rule_params(RULE_TYPE_OUTBOUND_MESSAGE, {}) == {"channel": None}
    assert normalize_rule_params(RULE_TYPE_OUTBOUND_MESSAGE, {"channel": "all"}) == {
        "channel": None
    }
    assert normalize_rule_params(RULE_TYPE_OUTBOUND_MESSAGE, {"channel": "FeiShu"}) == {
        "channel": "feishu"
    }
    with pytest.raises(BadRequestError):
        normalize_rule_params(RULE_TYPE_OUTBOUND_MESSAGE, {"channel": "email"})


def test_custom_requires_key_and_value() -> None:
    with pytest.raises(BadRequestError):
        normalize_rule_params(RULE_TYPE_CUSTOM, {"key": "k"})
    assert normalize_rule_params(RULE_TYPE_CUSTOM, {"key": "k", "value": 1}) == {
        "key": "k",
        "value": 1,
    }


def test_unknown_rule_type_is_rejected() -> None:
    with pytest.raises(BadRequestError):
        normalize_rule_params("something_else", {})


def test_default_descriptions_are_chinese() -> None:
    assert "500" in default_description(
        RULE_TYPE_PAYMENT_AMOUNT_OVER, {"amount": 500.0, "currency": "CNY"}
    )
    assert "rm *" in default_description(
        RULE_TYPE_COMMAND_PATTERN, {"pattern": "rm *", "style": "glob"}
    )
    assert "飞书" in default_description(RULE_TYPE_OUTBOUND_MESSAGE, {"channel": "feishu"})
    assert "terminal.execute" in default_description(
        RULE_TYPE_TOOL_MATCH, {"tool_name": "terminal.execute"}
    )


# ---------------------------------------------------------------- 匹配


def test_payment_amount_over_matches_only_above_threshold_same_currency() -> None:
    rule = _rule(RULE_TYPE_PAYMENT_AMOUNT_OVER, {"amount": 500.0, "currency": "CNY"})
    assert match_rule(rule, _action(kind=ACTION_PAYMENT, amount=500.0, currency="CNY")) is None
    assert match_rule(rule, _action(kind=ACTION_PAYMENT, amount=500.01, currency="cny"))
    # 币种不同不换算、不命中
    assert match_rule(rule, _action(kind=ACTION_PAYMENT, amount=1000, currency="USD")) is None
    # 动作没有金额也不命中
    assert match_rule(rule, _action(kind=ACTION_PAYMENT)) is None


def test_payment_action_without_currency_defaults_to_cny() -> None:
    rule = _rule(RULE_TYPE_PAYMENT_AMOUNT_OVER, {"amount": 10, "currency": "CNY"})
    assert match_rule(rule, _action(kind=ACTION_PAYMENT, amount=11))


def test_command_pattern_glob_and_regex() -> None:
    glob_rule = _rule(RULE_TYPE_COMMAND_PATTERN, {"pattern": "rm -rf *", "style": "glob"})
    assert match_rule(glob_rule, _action(kind=ACTION_TERMINAL_COMMAND, command="rm -rf /tmp/x"))
    assert match_rule(glob_rule, _action(kind=ACTION_TERMINAL_COMMAND, command="ls -la")) is None

    regex_rule = _rule(RULE_TYPE_COMMAND_PATTERN, {"pattern": r"\bsystemctl\b", "style": "regex"})
    assert match_rule(
        regex_rule, _action(kind=ACTION_TERMINAL_COMMAND, command="sudo systemctl restart nginx")
    )
    assert match_rule(regex_rule, _action(kind=ACTION_TERMINAL_COMMAND, command="ls")) is None


def test_command_pattern_does_not_match_when_command_missing() -> None:
    rule = _rule(RULE_TYPE_COMMAND_PATTERN, {"pattern": "*", "style": "glob"})
    assert match_rule(rule, _action(kind=ACTION_TOOL, tool_name="Bash")) is None


def test_tool_and_task_match_are_exact_or_glob() -> None:
    exact = _rule(RULE_TYPE_TOOL_MATCH, {"tool_name": "terminal.execute"})
    assert match_rule(exact, _action(kind=ACTION_TOOL, tool_name="terminal.execute"))
    assert match_rule(exact, _action(kind=ACTION_TOOL, tool_name="Bash")) is None

    glob = _rule(RULE_TYPE_TOOL_MATCH, {"tool_name": "terminal.*"})
    assert match_rule(glob, _action(kind=ACTION_TOOL, tool_name="terminal.execute"))

    both = _rule(RULE_TYPE_TASK_MATCH, {"tool_name": "terminal.*", "task_kind": "routine"})
    assert match_rule(
        both, _action(kind=ACTION_TASK, tool_name="terminal.execute", task_kind="routine")
    )
    assert (
        match_rule(both, _action(kind=ACTION_TASK, tool_name="terminal.execute", task_kind="chat"))
        is None
    )


def test_outbound_rule_matches_channel_or_all() -> None:
    any_channel = _rule(RULE_TYPE_OUTBOUND_MESSAGE, {"channel": None})
    assert match_rule(any_channel, _action(kind=ACTION_OUTBOUND_MESSAGE, channel="feishu"))

    feishu_only = _rule(RULE_TYPE_OUTBOUND_MESSAGE, {"channel": "feishu"})
    assert match_rule(feishu_only, _action(kind=ACTION_OUTBOUND_MESSAGE, channel="feishu"))
    assert match_rule(feishu_only, _action(kind=ACTION_OUTBOUND_MESSAGE, channel="wecom")) is None
    # 不是出站动作就不命中
    assert match_rule(feishu_only, _action(kind=ACTION_TERMINAL_COMMAND, command="ls")) is None


def test_custom_rule_matches_known_key_only() -> None:
    rule = _rule(RULE_TYPE_CUSTOM, {"key": "risk", "value": "high"})
    assert match_rule(rule, _action(kind=ACTION_TASK, custom={"risk": "high"}))
    assert match_rule(rule, _action(kind=ACTION_TASK, custom={"risk": "low"})) is None
    assert match_rule(rule, _action(kind=ACTION_TASK, custom={"other": "high"})) is None


def test_disabled_rule_is_ignored() -> None:
    rule = _rule(RULE_TYPE_TOOL_MATCH, {"tool_name": "terminal.execute"}, enabled=False)
    action = _action(kind=ACTION_TOOL, tool_name="terminal.execute")
    assert match_rule(rule, action) is None
    assert evaluate_rules([rule], action) is None


def test_first_match_order_is_created_at_then_id() -> None:
    later = _rule(
        RULE_TYPE_TOOL_MATCH,
        {"tool_name": "terminal.execute"},
        rule_id="b" * 32,
        created_at=BASE + timedelta(seconds=1),
        description="later",
    )
    earlier_b = _rule(
        RULE_TYPE_TOOL_MATCH,
        {"tool_name": "terminal.*"},
        rule_id="b" * 32,
        created_at=BASE,
        description="earlier-b",
    )
    earlier_a = _rule(
        RULE_TYPE_TOOL_MATCH,
        {"tool_name": "terminal.*"},
        rule_id="a" * 32,
        created_at=BASE,
        description="earlier-a",
    )
    match = evaluate_rules(
        [later, earlier_b, earlier_a], _action(kind=ACTION_TOOL, tool_name="terminal.execute")
    )
    assert match is not None
    assert match.rule.description == "earlier-a"


def test_no_rules_means_no_match() -> None:
    assert evaluate_rules([], _action(kind=ACTION_TERMINAL_COMMAND, command="rm -rf /")) is None


# ---------------------------------------------------------------- 用户隔离与服务入口


class _FakeRuleService:
    def __init__(self, rules_by_user: dict[str, list[RuleSpec]]) -> None:
        self.rules_by_user = rules_by_user
        self.calls: list[tuple[str, bool]] = []

    async def list_specs(self, cloud_user_id: str, *, enabled_only: bool = True) -> list[RuleSpec]:
        self.calls.append((cloud_user_id, enabled_only))
        return list(self.rules_by_user.get(cloud_user_id, []))


async def test_evaluate_approval_only_reads_the_actions_owner() -> None:
    service = _FakeRuleService(
        {
            "1": [_rule(RULE_TYPE_TOOL_MATCH, {"tool_name": "terminal.execute"})],
            "2": [],
        }
    )
    container = SimpleNamespace(approval_rules=service)
    action = _action(kind=ACTION_TOOL, tool_name="terminal.execute")

    assert await evaluate_approval(container, cloud_user_id="1", action=action) is not None
    assert await evaluate_approval(container, cloud_user_id="2", action=action) is None
    assert await evaluate_approval(container, cloud_user_id=None, action=action) is None
    assert service.calls == [("1", True), ("2", True)]


async def test_rule_approval_without_task_fails_closed() -> None:
    match = evaluator.ApprovalRuleMatch(
        rule=_rule(RULE_TYPE_TOOL_MATCH, {"tool_name": "terminal.execute"}, description="命中"),
        reason="命中",
    )
    with pytest.raises(ConflictError):
        await evaluator.raise_rule_approval(
            SimpleNamespace(),
            match=match,
            task_id=None,
            run_id=None,
            kind="rule_match",
            title="需要确认",
            detail="执行命令",
        )


async def test_rule_approval_card_carries_rule_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_raise(container: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"id": "decision-1"}

    async def fake_wait(container: Any, **kwargs: Any) -> str:
        return "allowed"

    monkeypatch.setattr(evaluator, "raise_approval", fake_raise)
    monkeypatch.setattr(evaluator, "wait_for_approval", fake_wait)

    rule = _rule(
        RULE_TYPE_TOOL_MATCH,
        {"tool_name": "terminal.execute"},
        rule_id="rule-1",
        description="执行终端命令需要确认",
    )
    match = evaluator.ApprovalRuleMatch(rule=rule, reason="命中规则")
    outcome = await evaluator.raise_rule_approval(
        SimpleNamespace(),
        match=match,
        task_id=uuid.uuid4(),
        run_id="run-1",
        kind="rule_match",
        title="需要确认",
        detail="命令：rm -rf /tmp/x",
    )
    assert outcome == "allowed"
    assert captured["meta"]["approval_rule_id"] == "rule-1"
    assert captured["meta"]["approval_rule_type"] == RULE_TYPE_TOOL_MATCH
    assert "执行终端命令需要确认" in captured["detail"]
    assert captured["title"] == "需要确认"
