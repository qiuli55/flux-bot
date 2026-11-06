"""热 / 温 / 冷分层测试（FLUX_CONTEXT_MECHANISM §6.5）。

§6.5 的要点是"替换必须渐进"：最近几轮（热）永不替换，中段（温）预算吃紧时先编码，
远段（冷）最后才降级为 ref。这里盯的是两条次序不变量，它们一旦漂移，渐进替换就名存实亡：

1. **年资分界**：以轮（turn）计量，`age_turns < hot_turns` 为热、其后 `warm_turns` 轮为
   温、再远为冷；负数没有物理意义，直接拒绝；
2. **两条次序**：装配优先级 `TIER_ORDER` = 热 → 温 → 冷；替换升级次序 `REPLACEMENT_LADDER`
   = 温 → 冷（热永不替换）。
"""

from __future__ import annotations

import pytest

from flux.core.context.tiers import (
    DEFAULT_TIER_POLICY,
    REPLACEMENT_LADDER,
    TIER_ORDER,
    Tier,
    TierPolicy,
    tier_of,
    tier_rank,
)


def test_tier_values() -> None:
    assert [tier.value for tier in Tier] == ["hot", "warm", "cold"]


def test_assembly_priority_is_hot_then_warm_then_cold() -> None:
    """装配优先级（§6.5）：热 → 温 → 冷，年资越浅越先占预算。"""
    assert TIER_ORDER == (Tier.HOT, Tier.WARM, Tier.COLD)


def test_replacement_ladder_excludes_hot() -> None:
    """替换升级次序（§6.5）：温先编码、冷最后降级；热层永不替换，不在阶梯上。"""
    assert REPLACEMENT_LADDER == (Tier.WARM, Tier.COLD)
    assert Tier.HOT not in REPLACEMENT_LADDER


@pytest.mark.parametrize(
    ("age_turns", "expected"),
    [
        (0, Tier.HOT),
        (2, Tier.HOT),
        (3, Tier.WARM),
        (14, Tier.WARM),
        (15, Tier.COLD),
        (1000, Tier.COLD),
    ],
)
def test_tier_of_boundaries(age_turns: int, expected: Tier) -> None:
    """默认策略（hot=3 / warm=12）：0–2 热、3–14 温、15 起冷。"""
    assert tier_of(age_turns) == expected


def test_tier_of_rejects_negative_age() -> None:
    """年资是"距最新一轮的轮数"，负数没有物理意义。"""
    with pytest.raises(ValueError):
        tier_of(-1)


def test_tier_of_honors_custom_policy() -> None:
    policy = TierPolicy(hot_turns=1, warm_turns=1)
    assert tier_of(0, policy) == Tier.HOT
    assert tier_of(1, policy) == Tier.WARM
    assert tier_of(2, policy) == Tier.COLD


def test_default_policy_values() -> None:
    assert DEFAULT_TIER_POLICY.hot_turns == 3
    assert DEFAULT_TIER_POLICY.warm_turns == 12


def test_tier_rank_matches_assembly_priority() -> None:
    assert tier_rank(Tier.HOT) == 0
    assert tier_rank(Tier.WARM) == 1
    assert tier_rank(Tier.COLD) == 2
