"""渐进替换的三层（热 / 温 / 冷，FLUX_CONTEXT_MECHANISM §6.5）。

§6.5 的要害是"替换**必须渐进**"：不能一次性把中段全换成摘要，否则会在
"还记着"和"只剩摘要"之间出现语义断层。故按**年资**分三层：

| 层 | 内容 | 何时替换 |
| --- | --- | --- |
| 热 | 最近 N 轮 | 永不替换 |
| 温 | 中段 | 预算吃紧时先编码 / 压缩 |
| 冷 | 远段 | 最后才降级为 ref |

两条次序是本节唯一不变量，别处不再各判一遍：

- **装配优先级** = 热 → 温 → 冷（`TIER_ORDER`），年资越浅越先占预算；
- **替换升级次序** = 温先编码、冷最后降级（`REPLACEMENT_LADDER`），热永不替换。

年资以**轮（turn）**计量：`age_turns=0` 是刚发生的一轮，数越大越远。分界由
`TierPolicy` 定死（§11 教训：不散落魔数），本模块只做判定，实际替换发生在打包器。
"""

from __future__ import annotations

from dataclasses import dataclass

from flux.enums import StrEnum


class Tier(StrEnum):
    """三层（§6.5）：决定一条历史在预算吃紧时被替换的先后。"""

    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


#: 装配优先级（热 → 温 → 冷）：下标即优先级，越靠前越先占预算。
TIER_ORDER: tuple[Tier, ...] = (Tier.HOT, Tier.WARM, Tier.COLD)

#: 预算吃紧时的替换升级次序（§6.5）：温层先编码，冷层最后降级为 ref；热层永不替换。
REPLACEMENT_LADDER: tuple[Tier, ...] = (Tier.WARM, Tier.COLD)


@dataclass(frozen=True)
class TierPolicy:
    """分层阈值（§6.5 只给语义，数值在此定死，便于整体替换与测试）。

    `hot_turns` 是热层的轮数（永不替换），其后的 `warm_turns` 轮为温层，再远为冷层。
    """

    #: 热层轮数：最近这么多轮永不替换
    hot_turns: int = 3
    #: 温层轮数：热层之后、进入冷层之前的轮数
    warm_turns: int = 12


DEFAULT_TIER_POLICY = TierPolicy()


def tier_of(age_turns: int, policy: TierPolicy = DEFAULT_TIER_POLICY) -> Tier:
    """按年资（距最新一轮的轮数）判层——纯函数，同输入必同输出。

    `age_turns=0` 是刚发生的一轮；`age_turns < 0` 没有物理意义，直接拒绝。
    """
    if age_turns < 0:
        raise ValueError("age_turns 不能为负：年资以'距最新一轮的轮数'计量")
    if age_turns < policy.hot_turns:
        return Tier.HOT
    if age_turns < policy.hot_turns + policy.warm_turns:
        return Tier.WARM
    return Tier.COLD


def tier_rank(tier: Tier) -> int:
    """装配优先级的名次（§6.5）：热层 0、温层 1、冷层 2，越小越先装配。"""
    return TIER_ORDER.index(tier)
