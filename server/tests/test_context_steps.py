"""context_steps 仓储测试（FLUX_CONTEXT_MECHANISM §3.3）。

盯三件事：

1. `seq_in_turn` 在 turn 内递增、跨 turn 互不干扰——目录行的顺序靠它；
2. 只追加、按序读回；
3. 取不到就抛 `NotFoundError`，不返回 None 让调用方猜。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from flux.config import Settings
from flux.core.context.repository import ContextStepRepository
from flux.db.session import create_engine, create_session_factory
from flux.enums import StepKind, StepStatus
from flux.errors import NotFoundError


@pytest.fixture()
async def repo(settings: Settings, db_schema: None) -> AsyncIterator[ContextStepRepository]:
    """用完显式 dispose：避免 aiosqlite 的连接线程活过事件循环。"""
    engine = create_engine(settings.database_url)
    try:
        yield ContextStepRepository(create_session_factory(engine))
    finally:
        await engine.dispose()


async def test_seq_increments_within_turn(repo: ContextStepRepository) -> None:
    first = await repo.append(
        run_id="run-1", turn_id="turn-1", kind=StepKind.COMMAND, status=StepStatus.OK
    )
    second = await repo.append(
        run_id="run-1", turn_id="turn-1", kind=StepKind.COMMAND, status=StepStatus.OK
    )
    assert first.seq_in_turn == 0
    assert second.seq_in_turn == 1
    assert second.id > first.id


async def test_seq_is_scoped_per_turn(repo: ContextStepRepository) -> None:
    """两个 turn 各自从 0 开始，互不干扰。"""
    a0 = await repo.append(
        run_id="run-1", turn_id="turn-a", kind=StepKind.COMMAND, status=StepStatus.OK
    )
    b0 = await repo.append(
        run_id="run-1", turn_id="turn-b", kind=StepKind.COMMAND, status=StepStatus.OK
    )
    a1 = await repo.append(
        run_id="run-1", turn_id="turn-a", kind=StepKind.COMMAND, status=StepStatus.OK
    )
    assert (a0.seq_in_turn, b0.seq_in_turn, a1.seq_in_turn) == (0, 0, 1)


async def test_get_returns_appended_step(repo: ContextStepRepository) -> None:
    step = await repo.append(
        run_id="run-1",
        turn_id="turn-1",
        kind=StepKind.COMMAND,
        status=StepStatus.OK,
        target_ref="pytest tests",
        exit_code=0,
        result_sha256="f" * 64,
    )
    fetched = await repo.get(step.id)
    assert fetched.id == step.id
    assert fetched.target_ref == "pytest tests"
    assert fetched.exit_code == 0


async def test_get_missing_step_raises(repo: ContextStepRepository) -> None:
    with pytest.raises(NotFoundError):
        await repo.get(999999)


async def test_list_by_turn_orders_by_seq(repo: ContextStepRepository) -> None:
    await repo.append(run_id="run-1", turn_id="turn-x", kind=StepKind.COMMAND, status=StepStatus.OK)
    await repo.append(
        run_id="run-1", turn_id="turn-x", kind=StepKind.TOOL_CALL, status=StepStatus.OK, tool="ls"
    )
    # 别的 turn 的 step 不该混进来
    await repo.append(run_id="run-1", turn_id="turn-y", kind=StepKind.COMMAND, status=StepStatus.OK)

    steps = await repo.list_by_turn("turn-x")
    assert [s.seq_in_turn for s in steps] == [0, 1]
    assert all(s.turn_id == "turn-x" for s in steps)
    assert steps[1].tool == "ls"


async def test_list_by_run_spans_turns(repo: ContextStepRepository) -> None:
    await repo.append(run_id="run-z", turn_id="turn-1", kind=StepKind.COMMAND, status=StepStatus.OK)
    await repo.append(run_id="run-z", turn_id="turn-2", kind=StepKind.COMMAND, status=StepStatus.OK)
    await repo.append(run_id="other", turn_id="turn-1", kind=StepKind.COMMAND, status=StepStatus.OK)

    steps = await repo.list_by_run("run-z")
    assert len(steps) == 2
    assert [s.id for s in steps] == sorted(s.id for s in steps)
