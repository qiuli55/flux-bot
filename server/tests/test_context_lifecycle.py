"""会话级联删除测试（FLUX_CONTEXT_MECHANISM §8.2 / 不变量 I7）。

删除是 I2「永不主动丢弃」的**唯一例外**：删就删干净。这里逐条核对 §8.2 的清单落到本仓库
是否删全了 —— step、反向索引、孤儿原文；并确认两件容易做错的事：

1. **共享原文不能误删**：`context_blobs` 全局去重（§8.4），同一份原文可能被别的 turn 还在引用，
   删一个 turn 时它必须活下来；
2. **幂等**（I7）：重复删、删不存在的 turn / run 都不报错，返回全 0。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest

from flux.config import Settings
from flux.core.context import (
    ContentStore,
    ContextLifecycle,
    ContextStepRepository,
    RedactionStore,
    VerificationLedger,
    redact,
)
from flux.db.session import create_engine, create_session_factory
from flux.enums import (
    ClaimKind,
    ContextTokenKind,
    StepKind,
    StepStatus,
    VerificationOutcome,
)


@dataclass
class Harness:
    lifecycle: ContextLifecycle
    steps: ContextStepRepository
    store: ContentStore
    ledger: VerificationLedger
    redactions: RedactionStore


@pytest.fixture()
async def harness(settings: Settings, db_schema: None) -> AsyncIterator[Harness]:
    """一个共享同一 SQLite 库的五件套；用完显式 dispose（范式同 test_context_store）。"""
    engine = create_engine(settings.database_url)
    try:
        factory = create_session_factory(engine)
        yield Harness(
            lifecycle=ContextLifecycle(factory),
            steps=ContextStepRepository(factory),
            store=ContentStore(factory),
            ledger=VerificationLedger(factory),
            # settings fixture 不设 redaction_key，故这里显式给一把测试密钥
            redactions=RedactionStore(factory, key="test-redaction-key"),
        )
    finally:
        await engine.dispose()


async def _seed(
    harness: Harness,
    *,
    text: str | None,
    run_id: str,
    turn_id: str,
    target_ref: str,
) -> tuple[int, str | None]:
    """把一个 blob 与一条引用它的 step 落库，返回 (step_id, sha256)。"""
    sha = None
    size = None
    if text is not None:
        blob = await harness.store.put(text.encode())
        sha, size = blob.sha256, blob.size_raw
    step = await harness.steps.append(
        run_id=run_id,
        turn_id=turn_id,
        kind=StepKind.COMMAND,
        status=StepStatus.OK,
        tool="terminal.execute",
        target_ref=target_ref,
        exit_code=0,
        result_sha256=sha,
        result_bytes=size,
    )
    return step.id, sha


async def test_delete_turn_removes_only_that_turn(harness: Harness) -> None:
    step_a, sha_a = await _seed(
        harness,
        text="turn a output\n",
        run_id="run-1",
        turn_id="turn-a",
        target_ref="todo_service/store.py",
    )
    await _seed(
        harness,
        text="turn b output\n",
        run_id="run-1",
        turn_id="turn-b",
        target_ref="todo_service/store.py",
    )

    counts = await harness.lifecycle.delete_turn("turn-a")

    # 一条 step、三条词条（全路径 + basename 的 FILE，程序名的 COMMAND）、一个孤儿 blob
    assert counts == {
        "steps": 1,
        "tokens": 3,
        "verifications": 0,
        "redactions": 0,
        "blobs": 1,
    }
    assert await harness.steps.list_by_turn("turn-a") == []
    remaining = await harness.steps.list_by_turn("turn-b")
    assert len(remaining) == 1
    assert remaining[0].id != step_a
    assert not await harness.store.has(sha_a or "")


async def test_delete_turn_keeps_shared_blob(harness: Harness) -> None:
    """两个 turn 引用同一份原文（内容寻址去重），删一个不能把原文删掉。"""
    shared = "identical bytes\n" * 4
    _, sha_a = await _seed(
        harness, text=shared, run_id="run-1", turn_id="turn-a", target_ref="auth.py"
    )
    _, sha_b = await _seed(
        harness, text=shared, run_id="run-1", turn_id="turn-b", target_ref="auth.py"
    )
    assert sha_a == sha_b

    counts = await harness.lifecycle.delete_turn("turn-a")

    assert counts["blobs"] == 0  # 仍有 turn-b 的 step 引用它
    assert await harness.store.has(sha_a or "")
    assert await harness.store.get(sha_a or "") == shared.encode()


async def test_delete_run_removes_all_turns(harness: Harness) -> None:
    await _seed(harness, text="one\n", run_id="run-1", turn_id="turn-a", target_ref="a.py")
    await _seed(harness, text="two\n", run_id="run-1", turn_id="turn-b", target_ref="b.py")
    step_c, sha_c = await _seed(
        harness, text="three\n", run_id="run-2", turn_id="turn-c", target_ref="c.py"
    )

    counts = await harness.lifecycle.delete_run("run-1")

    assert counts["steps"] == 2
    assert await harness.steps.list_by_run("run-1") == []
    # 别的 run 一根汗毛都不能动
    assert [s.id for s in await harness.steps.list_by_run("run-2")] == [step_c]
    assert await harness.store.has(sha_c or "")


async def test_delete_is_idempotent(harness: Harness) -> None:
    """I7：重复删除第二次返回全 0，不报错。"""
    await _seed(harness, text="x\n", run_id="run-1", turn_id="turn-a", target_ref="a.py")

    first = await harness.lifecycle.delete_turn("turn-a")
    second = await harness.lifecycle.delete_turn("turn-a")

    assert first["steps"] == 1
    assert second == {
        "steps": 0,
        "tokens": 0,
        "verifications": 0,
        "redactions": 0,
        "blobs": 0,
    }


async def test_delete_unknown_turn_is_noop(harness: Harness) -> None:
    assert await harness.lifecycle.delete_turn("never-existed") == {
        "steps": 0,
        "tokens": 0,
        "verifications": 0,
        "redactions": 0,
        "blobs": 0,
    }


async def test_delete_removes_reverse_index_entries(harness: Harness) -> None:
    """§8.2 清单里的「反向索引」（五类）必须同删，否则删完还能查出幽灵 step。"""
    step_a, _ = await _seed(
        harness,
        text="boom\n",
        run_id="run-1",
        turn_id="turn-a",
        target_ref="todo_service/store.py",
    )
    step_b, _ = await _seed(
        harness,
        text="ok\n",
        run_id="run-1",
        turn_id="turn-b",
        target_ref="todo_service/store.py",
    )

    found = await harness.steps.lookup(token="todo_service/store.py", kind=ContextTokenKind.FILE)
    assert found == [step_a, step_b]
    assert len(await harness.steps.tokens_of(step_a)) == 3

    await harness.lifecycle.delete_turn("turn-a")

    assert await harness.steps.lookup(
        token="todo_service/store.py", kind=ContextTokenKind.FILE
    ) == [step_b]
    assert await harness.steps.tokens_of(step_a) == []
    # 别的 turn 的词条不受影响
    assert len(await harness.steps.tokens_of(step_b)) == 3


async def test_delete_run_clears_reverse_index(harness: Harness) -> None:
    """按 run 删时，词条也得跟着走（词条上冗余了 run_id，正是为这一步免 join）。"""
    step_a, _ = await _seed(
        harness, text="a\n", run_id="run-1", turn_id="turn-a", target_ref="a.py"
    )
    await harness.lifecycle.delete_run("run-1")

    assert await harness.steps.tokens_of(step_a) == []
    assert await harness.steps.lookup(token="a.py", kind=ContextTokenKind.FILE) == []


async def test_delete_run_clears_verification_ledger(harness: Harness) -> None:
    """验证账本是派生数据，也必须随会话删干净（§8.2 / §13-5）。"""
    await _seed(harness, text="a\n", run_id="run-1", turn_id="turn-a", target_ref="a.py")
    await _seed(harness, text="b\n", run_id="run-2", turn_id="turn-b", target_ref="b.py")
    for run_id, turn_id in (("run-1", "turn-a"), ("run-2", "turn-b")):
        await harness.ledger.record(
            agent_fp="claude:opus",
            claim_kind=ClaimKind.TEST,
            outcome=VerificationOutcome.PASS,
            run_id=run_id,
            turn_id=turn_id,
        )

    counts = await harness.lifecycle.delete_run("run-1")

    assert counts["verifications"] == 1
    # 别的 run 的记录不受影响
    assert len(await harness.ledger.records_of("claude:opus", ClaimKind.TEST)) == 1


async def test_delete_run_clears_redaction_mapping(harness: Harness) -> None:
    """脱敏映射表存的正是秘密，也必须随会话删干净（§8.2 / §13-5）。"""
    await _seed(harness, text="a\n", run_id="run-1", turn_id="turn-a", target_ref="a.py")
    await _seed(harness, text="b\n", run_id="run-2", turn_id="turn-b", target_ref="b.py")
    for run_id, turn_id, secret in (
        ("run-1", "turn-a", "sk-abcdefghijklmnop"),
        ("run-2", "turn-b", "sk-zyxwvutsrqponmlk"),
    ):
        result = redact(f"token={secret}")
        written = await harness.redactions.record(
            run_id=run_id, turn_id=turn_id, matches=result.matches
        )
        assert written == 1

    counts = await harness.lifecycle.delete_run("run-1")

    assert counts["redactions"] == 1
    # turn-a 的映射随会话消失，turn-b 的原样保留
    assert await harness.redactions.resolve("turn-a", "[REDACTED:key]") is None
    assert await harness.redactions.resolve("turn-b", "[REDACTED:key]") == "sk-zyxwvutsrqponmlk"
