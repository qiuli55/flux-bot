"""反向索引测试（FLUX_CONTEXT_MECHANISM §7.2）。

上半部分测**词条抽取**（纯函数）：线索抽得全不全、归一化一不一致。
下半部分测**查询**：五类查询（文件 / 命令 / URL / 错误关键字 / 时间窗）都能反查到 step_id，
以及"手里只有线索"这个场景——agent 只记得文件名，不知道 id。

关键的一条：写入与查询共用 `normalize_token`，所以两边规则不会漂移；
如果哪天有人改了写入侧的归一化、忘了查询侧，`test_lookup_is_case_insensitive` 会先红。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest

from flux.config import Settings
from flux.core.context import ContextStepRepository, extract_tokens, normalize_token
from flux.db.session import create_engine, create_session_factory
from flux.enums import ContextTokenKind, StepKind, StepStatus
from flux.models.context import ContextStep


def _step(
    *,
    kind: StepKind = StepKind.COMMAND,
    tool: str | None = None,
    target_ref: str | None = None,
    result_head: str | None = None,
) -> ContextStep:
    return ContextStep(
        run_id="run-1",
        turn_id="turn-1",
        seq_in_turn=0,
        kind=kind.value,
        status=StepStatus.OK.value,
        agent_fp="agent-1:model-x",
        tool=tool,
        target_ref=target_ref,
        result_head=result_head,
    )


def _tokens(step: ContextStep) -> set[tuple[str, str]]:
    return {(kind.value, token) for kind, token in extract_tokens(step)}


# --- 词条抽取 ---


def test_command_yields_program_and_file_tokens() -> None:
    """`pytest tests/test_auth.py` → 命令 `pytest` + 文件（全路径与 basename 都要）。"""
    tokens = _tokens(_step(target_ref="pytest tests/test_auth.py"))
    assert (ContextTokenKind.COMMAND.value, "pytest") in tokens
    assert (ContextTokenKind.FILE.value, "tests/test_auth.py") in tokens
    assert (ContextTokenKind.FILE.value, "test_auth.py") in tokens


def test_tool_call_yields_tool_name_as_command() -> None:
    tokens = _tokens(_step(kind=StepKind.TOOL_CALL, tool="read_file", target_ref="src/auth.py"))
    assert (ContextTokenKind.COMMAND.value, "read_file") in tokens
    assert (ContextTokenKind.FILE.value, "src/auth.py") in tokens
    assert (ContextTokenKind.FILE.value, "auth.py") in tokens


def test_url_yields_host() -> None:
    tokens = _tokens(_step(target_ref="https://docs.example.com/a/b?x=1"))
    assert (ContextTokenKind.URL.value, "docs.example.com") in tokens
    # 完整 URL 不该作为文件词条混进来
    assert not any(kind == ContextTokenKind.FILE.value for kind, _ in tokens)


def test_result_head_yields_error_tokens() -> None:
    tokens = _tokens(
        _step(
            target_ref="pytest tests/test_a.py",
            result_head="FAILED tests/test_a.py::test_x - AssertionError: boom",
        )
    )
    assert (ContextTokenKind.ERROR.value, "assertionerror") in tokens
    assert (ContextTokenKind.ERROR.value, "failed") in tokens


def test_flags_are_not_paths() -> None:
    """`-v` 这种开关不能被当成文件路径，否则索引里全是噪声。"""
    tokens = _tokens(_step(target_ref="pytest -v tests/test_a.py"))
    assert (ContextTokenKind.FILE.value, "-v") not in tokens
    assert (ContextTokenKind.FILE.value, "test_a.py") in tokens


def test_duplicate_tokens_are_deduped() -> None:
    tokens = extract_tokens(_step(target_ref="src/auth.py src/auth.py"))
    assert len(tokens) == len(set(tokens))
    assert tokens.count((ContextTokenKind.FILE, "auth.py")) == 1


def test_normalize_token_is_shared_by_write_and_read() -> None:
    assert normalize_token(ContextTokenKind.FILE, "SRC/Auth.py") == "src/auth.py"
    assert normalize_token(ContextTokenKind.FILE, "./src/auth.py") == "src/auth.py"
    assert normalize_token(ContextTokenKind.URL, "https://Docs.Example.com/x") == "docs.example.com"
    # 查询时只给域名也要能命中（写入侧存的就是域名）
    assert normalize_token(ContextTokenKind.URL, "docs.example.com") == "docs.example.com"
    assert normalize_token(ContextTokenKind.COMMAND, "  Pytest ") == "pytest"


def test_step_without_clues_yields_nothing() -> None:
    assert extract_tokens(_step(kind=StepKind.FACT)) == []


# --- 查询 ---


@pytest.fixture()
async def repo(settings: Settings, db_schema: None) -> AsyncIterator[ContextStepRepository]:
    engine = create_engine(settings.database_url)
    try:
        yield ContextStepRepository(create_session_factory(engine))
    finally:
        await engine.dispose()


#: 时间窗用宽区间：本机 SQLite 的 created_at 精度到秒，窄窗容易 flaky（§7.2 第五类查询）
_WIDE = timedelta(days=1)


async def test_lookup_by_file_path_and_basename(repo: ContextStepRepository) -> None:
    step = await repo.append(
        run_id="run-1",
        turn_id="turn-1",
        kind=StepKind.FILE_OP,
        status=StepStatus.OK,
        target_ref="src/auth.py",
    )
    assert await repo.lookup(token="src/auth.py", kind=ContextTokenKind.FILE) == [step.id]
    # "手里只有线索"的核心场景：只记得文件名
    assert await repo.lookup(token="auth.py", kind=ContextTokenKind.FILE) == [step.id]


async def test_lookup_by_command(repo: ContextStepRepository) -> None:
    step = await repo.append(
        run_id="run-1",
        turn_id="turn-1",
        kind=StepKind.COMMAND,
        status=StepStatus.OK,
        target_ref="pytest tests/test_auth.py",
    )
    await repo.append(
        run_id="run-1",
        turn_id="turn-1",
        kind=StepKind.COMMAND,
        status=StepStatus.OK,
        target_ref="ruff check flux",
    )
    assert await repo.lookup(token="pytest", kind=ContextTokenKind.COMMAND) == [step.id]


async def test_lookup_by_url(repo: ContextStepRepository) -> None:
    step = await repo.append(
        run_id="run-1",
        turn_id="turn-1",
        kind=StepKind.TOOL_CALL,
        status=StepStatus.OK,
        tool="fetch",
        target_ref="https://docs.example.com/guide",
    )
    assert await repo.lookup(token="https://docs.example.com/other", kind=ContextTokenKind.URL) == [
        step.id
    ]


async def test_lookup_by_error_keyword(repo: ContextStepRepository) -> None:
    step = await repo.append(
        run_id="run-1",
        turn_id="turn-1",
        kind=StepKind.COMMAND,
        status=StepStatus.FAIL,
        target_ref="pytest tests/test_auth.py",
        exit_code=1,
        result_head="FAILED tests/test_auth.py - AssertionError: nope",
    )
    assert await repo.lookup(token="AssertionError", kind=ContextTokenKind.ERROR) == [step.id]


async def test_lookup_is_case_insensitive(repo: ContextStepRepository) -> None:
    """写入侧把 `AssertionError` 归一成小写，查询侧给原样大小写也要命中。"""
    step = await repo.append(
        run_id="run-1",
        turn_id="turn-1",
        kind=StepKind.COMMAND,
        status=StepStatus.FAIL,
        result_head="AssertionError: nope",
    )
    assert await repo.lookup(token="assertionerror", kind=ContextTokenKind.ERROR) == [step.id]


async def test_lookup_by_time_window(repo: ContextStepRepository) -> None:
    """第五类查询：不给线索，只给时间窗，直接扫 step 的时间戳。"""
    step = await repo.append(
        run_id="run-1", turn_id="turn-1", kind=StepKind.FACT, status=StepStatus.OK
    )
    now = datetime.now()
    assert await repo.lookup(since=now - _WIDE, until=now + _WIDE) == [step.id]
    # 未来窗口应查空
    assert await repo.lookup(since=now + _WIDE, until=now + 2 * _WIDE) == []


async def test_lookup_filters_by_run_id(repo: ContextStepRepository) -> None:
    mine = await repo.append(
        run_id="run-a",
        turn_id="turn-1",
        kind=StepKind.COMMAND,
        status=StepStatus.OK,
        target_ref="pytest tests",
    )
    await repo.append(
        run_id="run-b",
        turn_id="turn-2",
        kind=StepKind.COMMAND,
        status=StepStatus.OK,
        target_ref="pytest tests",
    )
    assert await repo.lookup(token="pytest", kind=ContextTokenKind.COMMAND, run_id="run-a") == [
        mine.id
    ]


async def test_lookup_returns_recent_matches_in_order(repo: ContextStepRepository) -> None:
    """命中多时先取最近 N 条，再按时间升序返回（方便直接喂目录行）。"""
    ids = []
    for index in range(3):
        step = await repo.append(
            run_id="run-1",
            turn_id=f"turn-{index}",
            kind=StepKind.COMMAND,
            status=StepStatus.OK,
            target_ref=f"pytest tests/test_{index}.py",
        )
        ids.append(step.id)

    assert await repo.lookup(token="pytest", kind=ContextTokenKind.COMMAND, limit=2) == ids[-2:]


async def test_lookup_without_token_returns_empty_for_blank(repo: ContextStepRepository) -> None:
    await repo.append(run_id="run-1", turn_id="turn-1", kind=StepKind.FACT, status=StepStatus.OK)
    assert await repo.lookup(token="   ") == []


async def test_tokens_of_exposes_index_rows(repo: ContextStepRepository) -> None:
    """索引词条随 append 一起落库，不依赖调用方额外补写。"""
    step = await repo.append(
        run_id="run-1",
        turn_id="turn-1",
        kind=StepKind.COMMAND,
        status=StepStatus.OK,
        target_ref="pytest tests/test_auth.py",
    )
    tokens = {(t.kind, t.token) for t in await repo.tokens_of(step.id)}
    assert (ContextTokenKind.COMMAND.value, "pytest") in tokens
    assert (ContextTokenKind.FILE.value, "test_auth.py") in tokens
    assert all(t.step_id == step.id for t in await repo.tokens_of(step.id))
