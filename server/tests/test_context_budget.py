"""动态投喂预算测试（文档 §6.1–6.3）。

§6.1 要求预算由公式推导而不是拍一个常数；§6.2 / §6.3 把内容分成"能不能压"（分区）
与"实际长什么样"（等级）。这里验证三件事：

1. 公式算得对：`ceiling = 窗口 × 安全系数 − 预留输出 − 系统占用`，必留项扣完不为负；
2. 对话历史的条数随预算走，不再是与预算无关的固定值；
3. P1 溢出时的编码是**选择**而非生成：产物比原文短，且逐字仍来自原文。
"""

from __future__ import annotations

import pytest

from flux.core.context.budget import (
    DEFAULT_BUDGET_CHARS,
    DEFAULT_CONTEXT_WINDOW,
    ESTIMATED_MESSAGE_CHARS,
    HISTORY_BUDGET_SHARE,
    RESERVED_OUTPUT_CHARS,
    SAFETY_FACTOR,
    SYSTEM_TOOL_OVERHEAD_CHARS,
    FeedBudget,
    Level,
    Zone,
    history_message_limit,
)
from flux.core.mcp.context_packager import _encode


def test_default_budget_is_derived_from_the_formula() -> None:
    """默认预算 = 窗口 × 安全系数 − 预留输出 − 系统占用，而不是一个拍出来的常数。"""
    expected = (
        int(DEFAULT_CONTEXT_WINDOW * SAFETY_FACTOR)
        - RESERVED_OUTPUT_CHARS
        - SYSTEM_TOOL_OVERHEAD_CHARS
    )
    assert expected == DEFAULT_BUDGET_CHARS
    assert FeedBudget().ceiling == expected


def test_feed_budget_splits_required_and_available() -> None:
    budget = FeedBudget(required=5_000)
    assert budget.total == budget.required + budget.available
    assert budget.available == budget.ceiling - 5_000


def test_required_over_ceiling_never_yields_negative_available() -> None:
    """必留区撑爆预算时 `available` 归零而不是变负——余量只有"没有"，没有"欠账"。"""
    budget = FeedBudget(required=DEFAULT_BUDGET_CHARS + 1)
    assert budget.available == 0
    assert budget.total == budget.required


def test_zone_and_level_values() -> None:
    assert [zone.value for zone in Zone] == ["P0", "P1", "P2"]
    assert [level.value for level in Level] == ["L1", "L2", "L3"]


def test_history_limit_tracks_budget_instead_of_fixed_count() -> None:
    """条数由 §6.1 的字符预算折算而来，预算调大就多带、调小就少带。"""
    derived = history_message_limit()
    assert derived == max(
        1, int(DEFAULT_BUDGET_CHARS * HISTORY_BUDGET_SHARE) // ESTIMATED_MESSAGE_CHARS
    )
    assert history_message_limit(budget=1) < derived < history_message_limit(budget=1_000_000)


def test_history_limit_never_drops_below_one() -> None:
    """预算再小也要留一条：调用方至少要拿到"最近一条"这个最小上下文。"""
    assert history_message_limit(budget=1) == 1
    assert history_message_limit(budget=DEFAULT_BUDGET_CHARS, share=0.0) == 1
    assert history_message_limit(budget=DEFAULT_BUDGET_CHARS, share=5.0) == history_message_limit(
        budget=DEFAULT_BUDGET_CHARS, share=1.0
    )


def test_history_limit_rejects_non_positive_message_size() -> None:
    with pytest.raises(ValueError):
        history_message_limit(message_chars=0)


def test_encode_shrinks_an_oversized_block_below_budget() -> None:
    """P1 溢出自适应编码：产物短于原文、卡在预算内，且逐字仍来自原文（§5.1 / §5.3）。"""
    text = "\n".join(
        f"- [proposed] todo_service/store_{index}.py（+1/-1，1 个改动块） id={index}"
        for index in range(40)
    )
    encoded = _encode(text, budget=400, query="改 store 的缺陷")

    assert encoded is not None
    assert len(encoded) < len(text)
    assert len(encoded) <= 400
    # 确定性：同一段文本 + 同一个查询永远得到同一份产物
    assert _encode(text, budget=400, query="改 store 的缺陷") == encoded
    # 抽取不是生成：除标记行外，每一行都是原文里逐字存在的一行
    for line in encoded.splitlines():
        if line.startswith(("# ", "…", "[×", "[L")):
            continue
        assert line in text


def test_encode_gives_up_without_budget() -> None:
    """余量为零时编码无权开工，直接交回 None，由调用方整条降级为 L3 引用。"""
    assert _encode("anything at all", budget=0, query="x") is None
    assert _encode("anything at all", budget=-5, query="x") is None
