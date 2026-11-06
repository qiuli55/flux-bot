"""验证账本与信任分级（FLUX_CONTEXT_MECHANISM §4.4）。

"不信任"是默认起点，但**必须是动态量**：恒定的不信任等于每代全量重验，成本一爆，
人最后干脆盲信——这是必然后果，不是可能。故 §4.4 要给信任一条能**长回来**、也能一次
被抓就**摔回去**的路径，本模块就是那条路径：

1. **账本**（`VerificationLedger`）：按「指纹 × 断言类」记账，一行 = 一次验证的结论；
2. **分级**（`grade`）：由账本记录算出该指纹在该断言类上应受的信任等级 L0–L3。

分级是**纯函数**：给定一串按发生顺序（旧 → 新）排列的结论，等级唯一确定，不掺时间、
不掺随机——否则接管方"按等级取信"就等于掷骰子。

等级判定（§4.4 只给了语义，数值阈值由 `TrustPolicy` 定死、可整体替换）：

| 等级 | 条件 | 行为 |
| --- | --- | --- |
| L0 | 无记录，或最近一次 FAIL | 全量重验（新指纹 / 刚被证伪） |
| L1 | 最近一次通过，但连胜未到稳定 | 抽查 |
| L2 | 连胜 ≥ `stable_streak` 且通过率 ≥ `min_pass_rate` | 只验关键项 |
| L3 | 连胜 ≥ `long_stable_streak` 且通过率 ≥ `min_pass_rate` | 采信但留痕 |

阶梯单调（`stable_streak` < `long_stable_streak`）、无空洞、每级都有出口：连败即回 L0，
连胜即逐级长回 L3。通过率不达标则压在 L1——连胜再多也补不上"错得不少"。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.enums import ClaimKind, TrustLevel, VerificationOutcome
from flux.models.context import ContextVerification

#: 判级只看该指纹最近这么多条记录。账本可无限增长，但分级该反映**近期**表现；
#: 且任何一级都在 `long_stable_streak` 条内可判出，无需全量回扫。
_MAX_RECORDS = 200


@dataclass(frozen=True)
class TrustPolicy:
    """信任分级的阈值（§4.4 只定语义，数值在此定死，便于整体替换与测试）。"""

    #: 窗口内通过率下限：低于它即压住 L2 / L3
    min_pass_rate: float = 0.9
    #: 升 L2 所需的连续通过数
    stable_streak: int = 10
    #: 升 L3 所需的连续通过数
    long_stable_streak: int = 30


DEFAULT_POLICY = TrustPolicy()


@dataclass(frozen=True)
class TrustStats:
    """账本的统计投影：等级结论 + 支撑它的数字，让调用方能把"凭什么"一并交代。"""

    total: int
    passes: int
    fails: int
    #: 自最近一次 FAIL 之后的连续通过数（无记录时为 0）
    streak: int
    #: 窗口内通过率
    pass_rate: float
    level: TrustLevel


def grade(
    outcomes: Sequence[VerificationOutcome],
    policy: TrustPolicy = DEFAULT_POLICY,
) -> TrustStats:
    """把按发生顺序（旧 → 新）排列的结论算成分级——纯函数，同输入必同输出。"""
    total = len(outcomes)
    passes = sum(1 for outcome in outcomes if outcome is VerificationOutcome.PASS)
    streak = 0
    for outcome in reversed(outcomes):
        if outcome is not VerificationOutcome.PASS:
            break
        streak += 1
    pass_rate = passes / total if total else 0.0
    return TrustStats(
        total=total,
        passes=passes,
        fails=total - passes,
        streak=streak,
        pass_rate=pass_rate,
        level=_level_of(streak, pass_rate, policy),
    )


def _level_of(streak: int, pass_rate: float, policy: TrustPolicy) -> TrustLevel:
    if streak == 0:
        # 无记录（新指纹）或最近一次 FAIL（刚被证伪）—— §4.4 对两者同判：全量重验
        return TrustLevel.L0
    if pass_rate < policy.min_pass_rate:
        return TrustLevel.L1
    if streak >= policy.long_stable_streak:
        return TrustLevel.L3
    if streak >= policy.stable_streak:
        return TrustLevel.L2
    return TrustLevel.L1


class VerificationLedger:
    """验证账本：记账（`record`）与查账（`records_of` / `stats_of`）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        *,
        agent_fp: str,
        claim_kind: ClaimKind,
        outcome: VerificationOutcome,
        run_id: str = "",
        turn_id: str = "",
        step_id: int | None = None,
        note: str | None = None,
    ) -> ContextVerification:
        """记一次验证结论；`note` 是证伪理由，故只在 FAIL 时保留。"""
        row = ContextVerification(
            agent_fp=agent_fp,
            claim_kind=claim_kind.value,
            outcome=outcome.value,
            run_id=run_id,
            turn_id=turn_id,
            step_id=step_id,
            note=note if outcome is VerificationOutcome.FAIL else None,
        )
        async with self._session_factory() as db:
            db.add(row)
            await db.commit()
            return row

    async def records_of(
        self,
        agent_fp: str,
        claim_kind: ClaimKind,
        *,
        limit: int = _MAX_RECORDS,
    ) -> list[ContextVerification]:
        """该「指纹 × 断言类」的记录，**旧 → 新**（分级要的正是这个顺序）。"""
        statement = (
            select(ContextVerification)
            .where(
                ContextVerification.agent_fp == agent_fp,
                ContextVerification.claim_kind == claim_kind.value,
            )
            .order_by(ContextVerification.id.desc())
            .limit(limit)
        )
        async with self._session_factory() as db:
            newest_first = list(await db.scalars(statement))
        return list(reversed(newest_first))

    async def stats_of(
        self,
        agent_fp: str,
        claim_kind: ClaimKind,
        policy: TrustPolicy = DEFAULT_POLICY,
    ) -> TrustStats:
        """该「指纹 × 断言类」当前的信任统计与等级。"""
        rows = await self.records_of(agent_fp, claim_kind)
        return grade([VerificationOutcome(row.outcome) for row in rows], policy)
