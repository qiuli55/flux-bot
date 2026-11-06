"""验证账本与信任分级测试（FLUX_CONTEXT_MECHANISM §4.4）。

分两层验：

1. **分级是纯函数**：给定一串按发生顺序排列的结论，等级唯一确定——同输入必同输出，
   不掺时间、不掺随机。阶梯单调、无空洞、每级都有出口（连败回 L0，连胜逐级长回 L3）。
2. **账本**：按「指纹 × 断言类」记账，互不串味；判级只看自己那一格。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from flux.config import Settings
from flux.core.context import (
    TrustPolicy,
    TrustStats,
    VerificationLedger,
    grade,
)
from flux.db.session import create_engine, create_session_factory
from flux.enums import ClaimKind, TrustLevel, VerificationOutcome

PASS = VerificationOutcome.PASS
FAIL = VerificationOutcome.FAIL
L0, L1, L2, L3 = TrustLevel.L0, TrustLevel.L1, TrustLevel.L2, TrustLevel.L3

#: 测试用小阈值：几条连胜就能跨级，省得每条用例都造 30 条记录
_SMALL = TrustPolicy(min_pass_rate=0.5, stable_streak=3, long_stable_streak=5)


@pytest.fixture()
async def ledger(settings: Settings, db_schema: None) -> AsyncIterator[VerificationLedger]:
    engine = create_engine(settings.database_url)
    try:
        yield VerificationLedger(create_session_factory(engine))
    finally:
        await engine.dispose()


# --- 分级（纯函数） ---


def test_no_records_is_l0() -> None:
    """新指纹：没有任何记录就是默认的"全量重验"。"""
    stats = grade([])

    assert stats == TrustStats(total=0, passes=0, fails=0, streak=0, pass_rate=0.0, level=L0)


def test_single_pass_reaches_l1_only() -> None:
    """刚通过一次只够抽查——一次通过撑不起"稳定"。"""
    assert grade([PASS], _SMALL).level == L1


def test_streak_ladder_is_monotonic_and_gapless() -> None:
    """连胜阶梯必须单调且无空洞：0 → L0，1..2 → L1，3..4 → L2，≥5 → L3。"""
    levels = [grade([PASS] * n, _SMALL).level for n in range(8)]

    assert levels == [L0, L1, L1, L2, L2, L3, L3, L3]


def test_streak_boundaries_are_exact() -> None:
    """跨级正好卡在阈值上：差一条不升，够一条即升。"""
    assert grade([PASS] * (_SMALL.stable_streak - 1), _SMALL).level == L1
    assert grade([PASS] * _SMALL.stable_streak, _SMALL).level == L2
    assert grade([PASS] * (_SMALL.long_stable_streak - 1), _SMALL).level == L2
    assert grade([PASS] * _SMALL.long_stable_streak, _SMALL).level == L3


def test_last_failure_drops_to_l0() -> None:
    """抓到一次错误就摔回 L0——不管之前连胜多长。"""
    stats = grade([PASS] * 20 + [FAIL], _SMALL)

    assert stats.level == L0
    assert stats.streak == 0
    assert stats.fails == 1


def test_trust_can_grow_back_after_failure() -> None:
    """信任能长回来：证伪后重新连胜，等级逐级回升（而不是一票否决到永远）。"""
    recovered = [PASS] * 20 + [FAIL] + [PASS] * _SMALL.stable_streak

    stats = grade(recovered, _SMALL)

    assert stats.streak == _SMALL.stable_streak
    assert stats.level == L2


def test_low_pass_rate_caps_level_at_l1() -> None:
    """连胜再长也补不上"错得不少"：通过率不达标就压在 L1。"""
    stats = grade([FAIL] * 20 + [PASS] * 15, _SMALL)

    assert stats.streak == 15
    assert stats.pass_rate < _SMALL.min_pass_rate
    assert stats.level == L1


def test_pass_rate_at_threshold_is_accepted() -> None:
    """通过率恰好等于阈值算达标（下限是闭区间，否则"刚好达标"会被无谓地压一级）。"""
    outcomes = [FAIL] * _SMALL.stable_streak + [PASS] * _SMALL.stable_streak

    stats = grade(outcomes, _SMALL)

    assert stats.pass_rate == 0.5
    assert stats.level == L2  # 阈值若按开区间处理，这里会被压到 L1


def test_stats_reports_supporting_numbers() -> None:
    """等级之外还要给出支撑它的数字，调用方才能把"凭什么"交代清楚。"""
    stats = grade([PASS, PASS, FAIL, PASS, PASS], _SMALL)

    assert stats.total == 5
    assert stats.passes == 4
    assert stats.fails == 1
    assert stats.streak == 2
    assert stats.pass_rate == pytest.approx(0.8)


def test_grade_is_deterministic() -> None:
    """同输入必同输出——否则"按等级取信"就是掷骰子。"""
    outcomes = [PASS, FAIL, PASS, PASS, PASS, PASS, PASS]

    assert grade(outcomes, _SMALL) == grade(list(outcomes), _SMALL)


def test_default_policy_thresholds_are_stricter() -> None:
    """默认阈值比测试用阈值保守：要到 10 连胜才 L2、30 连胜才 L3。"""
    assert grade([PASS] * 10).level == L2
    assert grade([PASS] * 30).level == L3


# --- 账本（落库） ---


async def test_ledger_records_newest_last(ledger: VerificationLedger) -> None:
    """记录按发生顺序返回（旧 → 新）：分级要的正是这个顺序。"""
    for outcome in (PASS, FAIL, PASS):
        await ledger.record(agent_fp="claude:opus", claim_kind=ClaimKind.TEST, outcome=outcome)

    rows = await ledger.records_of("claude:opus", ClaimKind.TEST)

    assert [row.outcome for row in rows] == [PASS.value, FAIL.value, PASS.value]
    assert rows[0].id < rows[-1].id


async def test_ledger_is_scoped_by_fingerprint_and_claim_kind(
    ledger: VerificationLedger,
) -> None:
    """账本粒度是「指纹 × 断言类」：一格被证伪不该连累别格的信用。"""
    await ledger.record(agent_fp="a:m", claim_kind=ClaimKind.TEST, outcome=FAIL)
    await ledger.record(agent_fp="b:m", claim_kind=ClaimKind.TEST, outcome=PASS)
    await ledger.record(agent_fp="a:m", claim_kind=ClaimKind.FILE, outcome=PASS)

    assert await ledger.stats_of("a:m", ClaimKind.TEST, _SMALL) == TrustStats(
        total=1, passes=0, fails=1, streak=0, pass_rate=0.0, level=L0
    )
    assert (await ledger.stats_of("b:m", ClaimKind.TEST, _SMALL)).level == L1
    assert (await ledger.stats_of("a:m", ClaimKind.FILE, _SMALL)).level == L1


async def test_ledger_note_only_kept_on_failure(ledger: VerificationLedger) -> None:
    """`note` 是证伪理由：通过无需解释，故落库时被丢掉。"""
    await ledger.record(
        agent_fp="a:m",
        claim_kind=ClaimKind.TEST,
        outcome=FAIL,
        note="测试其实没跑起来",
    )
    await ledger.record(agent_fp="a:m", claim_kind=ClaimKind.TEST, outcome=PASS, note="多余的辩解")

    rows = await ledger.records_of("a:m", ClaimKind.TEST)

    assert rows[0].note == "测试其实没跑起来"
    assert rows[1].note is None


async def test_ledger_level_grows_with_successful_records(ledger: VerificationLedger) -> None:
    """端到端：一条 FAIL 把等级摁住，之后连胜才逐级长回来。"""
    await ledger.record(agent_fp="a:m", claim_kind=ClaimKind.TEST, outcome=PASS)
    await ledger.record(agent_fp="a:m", claim_kind=ClaimKind.TEST, outcome=FAIL)

    assert (await ledger.stats_of("a:m", ClaimKind.TEST, _SMALL)).level == L0

    for _ in range(_SMALL.long_stable_streak):
        await ledger.record(agent_fp="a:m", claim_kind=ClaimKind.TEST, outcome=PASS)

    stats = await ledger.stats_of("a:m", ClaimKind.TEST, _SMALL)

    assert stats.streak == _SMALL.long_stable_streak
    assert stats.level == L3


async def test_ledger_keeps_run_and_turn_keys(ledger: VerificationLedger) -> None:
    """记录必须带上归属 / 删除键，否则删会话时清不掉（§8.2）。"""
    row = await ledger.record(
        agent_fp="a:m",
        claim_kind=ClaimKind.COMMAND,
        outcome=PASS,
        run_id="run-1",
        turn_id="turn-a",
        step_id=42,
    )

    assert (row.run_id, row.turn_id, row.step_id) == ("run-1", "turn-a", 42)
    assert row.to_dict()["verification_id"] == row.id
