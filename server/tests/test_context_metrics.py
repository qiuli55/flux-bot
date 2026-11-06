"""投喂度量测试（FLUX_CONTEXT_MECHANISM §5.6）。

`FeedMetrics` 把一次投喂的原始计数换算成三个本地可算的指标，并给出两条硬红线的
`healthy` 判定。这里盯的是：

1. **口径**：压缩率 = 投喂 / 原文、必留项命中率 = 留住 / 必留、重建一致率 =
   S5 通过 / S5 跑过，三个都是简单比值，没有隐藏的加权；
2. **零分母不炸也不撒谎**：原文为空 / 没有必留项 / 没跑过自校验时，一律记 1.0
   （"没压也没胀""一条不减""一次不差"），不能除零，也不能报 0 把红线误判成不达标；
3. **红线判定**：`healthy` 只在两条都满值时为真，任一出现缺口即为假。
"""

from __future__ import annotations

import pytest

from flux.core.context.metrics import (
    BackfillObserver,
    BackfillPolicy,
    BackfillStats,
    FeedMetrics,
)


def _metrics(
    *,
    source_chars: int = 1000,
    fed_chars: int = 250,
    required_total: int = 5,
    required_kept: int = 5,
    verified: int = 3,
    verified_ok: int = 3,
    dropped_count: int = 0,
) -> FeedMetrics:
    return FeedMetrics(
        source_chars=source_chars,
        fed_chars=fed_chars,
        required_total=required_total,
        required_kept=required_kept,
        verified=verified,
        verified_ok=verified_ok,
        dropped_count=dropped_count,
    )


def test_compression_ratio_is_fed_over_source() -> None:
    assert _metrics(source_chars=1000, fed_chars=250).compression_ratio == 0.25


def test_compression_ratio_is_one_when_source_is_empty() -> None:
    """原文为空时记 1.0：没压也没胀，而不是除零或 0。"""
    assert _metrics(source_chars=0, fed_chars=0).compression_ratio == 1.0
    assert _metrics(source_chars=0, fed_chars=99).compression_ratio == 1.0


def test_required_hit_rate_is_full_when_all_kept() -> None:
    assert _metrics(required_total=5, required_kept=5).required_hit_rate == 1.0


def test_required_hit_rate_drops_when_something_is_missing() -> None:
    assert _metrics(required_total=4, required_kept=3).required_hit_rate == 0.75


def test_required_hit_rate_is_one_when_nothing_is_required() -> None:
    assert _metrics(required_total=0, required_kept=0).required_hit_rate == 1.0


def test_reconstruction_rate_is_full_when_all_verifications_pass() -> None:
    assert _metrics(verified=3, verified_ok=3).reconstruction_rate == 1.0


def test_reconstruction_rate_drops_when_a_verification_fails() -> None:
    assert _metrics(verified=4, verified_ok=3).reconstruction_rate == 0.75


def test_reconstruction_rate_is_one_when_nothing_was_verified() -> None:
    """一段都没跑自校验（没有可压区）也算一次不差，而不是 0/0。"""
    assert _metrics(verified=0, verified_ok=0).reconstruction_rate == 1.0


@pytest.mark.parametrize(
    ("required_total", "required_kept", "verified", "verified_ok", "expected"),
    [
        (5, 5, 3, 3, True),
        (5, 4, 3, 3, False),
        (5, 5, 3, 2, False),
        (5, 4, 3, 2, False),
        (0, 0, 0, 0, True),
    ],
)
def test_healthy_needs_both_red_lines(
    required_total: int,
    required_kept: int,
    verified: int,
    verified_ok: int,
    expected: bool,
) -> None:
    result = _metrics(
        required_total=required_total,
        required_kept=required_kept,
        verified=verified,
        verified_ok=verified_ok,
    )
    assert result.healthy is expected


def test_as_dict_flat_fields_and_rounding() -> None:
    result = _metrics(
        source_chars=1000,
        fed_chars=333,
        required_total=7,
        required_kept=7,
        verified=3,
        verified_ok=3,
        dropped_count=2,
    ).as_dict()

    assert result["source_chars"] == 1000
    assert result["fed_chars"] == 333
    assert result["compression_ratio"] == 0.333
    assert result["required_total"] == 7
    assert result["required_kept"] == 7
    assert result["required_hit_rate"] == 1.0
    assert result["verified"] == 3
    assert result["verified_ok"] == 3
    assert result["reconstruction_rate"] == 1.0
    assert result["dropped_count"] == 2
    assert result["healthy"] is True


# ---------------------------------------------------------------------------
# 回补率观测（§6.7）
# ---------------------------------------------------------------------------


def test_backfill_rate_is_fetched_over_offered() -> None:
    assert BackfillStats(offered=10, fetched=2).rate == 0.2


def test_backfill_rate_is_zero_when_nothing_was_offered() -> None:
    """一条引用都没给出去时无从回补，记 0.0 而不是除零。"""
    assert BackfillStats(offered=0, fetched=0).rate == 0.0
    assert BackfillStats(offered=0, fetched=5).rate == 0.0


def test_backfill_rollback_needs_enough_samples() -> None:
    """样本太少不给结论：offered 未达 min_samples 时，哪怕比率超标也不回退。"""
    policy = BackfillPolicy(max_rate=0.25, min_samples=20)
    assert BackfillStats(offered=10, fetched=10).should_rollback(policy) is False


def test_backfill_rollback_only_when_rate_exceeds_max() -> None:
    """样本够多且回补率超限，才是真的压过头（§6.7）。"""
    policy = BackfillPolicy(max_rate=0.25, min_samples=20)
    assert BackfillStats(offered=20, fetched=5).should_rollback(policy) is False  # 0.25 未超
    assert BackfillStats(offered=20, fetched=6).should_rollback(policy) is True  # 0.30 超限


def test_backfill_stats_as_dict() -> None:
    policy = BackfillPolicy(max_rate=0.25, min_samples=20)
    result = BackfillStats(offered=8, fetched=1).as_dict(policy)
    assert result == {"offered": 8, "fetched": 1, "rate": 0.125, "rollback": False}


def test_backfill_observer_accumulates_and_resets() -> None:
    observer = BackfillObserver()
    observer.note_offered(3)
    observer.note_offered(2)
    observer.note_fetched()
    observer.note_fetched()

    assert observer.stats() == BackfillStats(offered=5, fetched=2)
    assert observer.as_dict()["rate"] == 0.4

    observer.reset()
    assert observer.stats() == BackfillStats(offered=0, fetched=0)
    assert observer.should_rollback() is False


def test_backfill_observer_clamps_negative_offers() -> None:
    """调用方算错把 offer 记成负的，也不该让观测账倒欠。"""
    observer = BackfillObserver()
    observer.note_offered(-5)
    observer.note_offered(2)
    assert observer.stats().offered == 2


def test_backfill_observer_uses_its_own_policy() -> None:
    observer = BackfillObserver(BackfillPolicy(max_rate=0.1, min_samples=2))
    observer.note_offered(2)
    observer.note_fetched()
    assert observer.should_rollback() is True
