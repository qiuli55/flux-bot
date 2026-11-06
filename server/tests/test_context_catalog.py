"""目录行渲染测试（FLUX_CONTEXT_MECHANISM §6.4）。

目录行是 agent 进入冷库的**唯一入口**，所以这里盯的是四件事有没有落地：

1. `step_id` 露出来——否则取不了件；
2. 状态信号 + 非 ok 的 `! ` 视觉标记——否则没人会回头看；
3. 内联事实最小锚（`exit=` / `sha=` / 目标）——不取件也能核对；
4. 连续重复折叠成一行统计——目录行自身可压缩，且**只折连续**（跨段合并会丢顺序）。

这些 step 都是内存里手搓的：渲染器读哪些字段，就必须显式给哪些字段——
`default=` 是列插入默认值，不在实例构造时生效。
"""

from __future__ import annotations

from flux.core.context.catalog import catalog_line, render_catalog
from flux.enums import StepKind, StepStatus
from flux.models.context import ContextStep

_SHA = "a1b2c3d4" + "0" * 56


def _step(
    step_id: int,
    *,
    run_id: str = "run-1",
    turn_id: str = "turn-1",
    seq_in_turn: int = 0,
    kind: StepKind = StepKind.COMMAND,
    status: StepStatus = StepStatus.OK,
    agent_fp: str = "agent-1:model-x",
    tool: str | None = None,
    target_ref: str | None = None,
    exit_code: int | None = None,
    result_sha256: str | None = None,
) -> ContextStep:
    return ContextStep(
        id=step_id,
        run_id=run_id,
        turn_id=turn_id,
        seq_in_turn=seq_in_turn,
        kind=kind.value,
        status=status.value,
        agent_fp=agent_fp,
        tool=tool,
        target_ref=target_ref,
        exit_code=exit_code,
        result_sha256=result_sha256,
    )


def test_catalog_line_exposes_id_and_fact_anchors() -> None:
    """id、状态、`exit=`、短 sha 都要在行里——不取件也能核对。"""
    line = catalog_line(
        _step(7, target_ref="pytest tests/test_auth.py", exit_code=0, result_sha256=_SHA)
    )
    assert line.startswith("#0007")
    assert "turn-1/s0" in line
    assert "cmd" in line
    assert "pytest tests/test_auth.py" in line
    assert "ok" in line
    assert "exit=0" in line
    assert f"sha={_SHA[:8]}" in line
    assert not line.startswith("! ")


def test_failed_step_is_flagged() -> None:
    """非 ok 的 step 前置 `! `——回看通道要显眼，否则没人走。"""
    line = catalog_line(_step(3, status=StepStatus.FAIL, exit_code=1))
    assert line.startswith("! ")
    assert "fail" in line


def test_ok_step_has_no_marker() -> None:
    assert not catalog_line(_step(4)).startswith("! ")


def test_tool_call_uses_tool_name_as_action() -> None:
    """tool_call 的动作词直接用工具名，而不是泛泛的 `tool_call`。"""
    line = catalog_line(_step(5, kind=StepKind.TOOL_CALL, tool="read_file"))
    assert "read_file" in line
    assert "tool_call" not in line


def test_long_target_is_clipped() -> None:
    """目录行是极简投影，长目标截断加 `…`。"""
    long_target = "x" * 200
    line = catalog_line(_step(6, target_ref=long_target))
    assert long_target not in line
    assert "…" in line


def test_missing_target_renders_placeholder() -> None:
    """没有目标时给个占位符，避免字段错位看不出来。"""
    line = catalog_line(_step(8, target_ref=None))
    assert "-" in line


def test_consecutive_repeats_fold_into_one_line() -> None:
    """连续三次同样的命令 → 一行，带 `×3` 与"最近一次"的结局。"""
    steps = [
        _step(1, seq_in_turn=0, exit_code=0),
        _step(2, seq_in_turn=1, exit_code=0),
        _step(3, seq_in_turn=2, status=StepStatus.FAIL, exit_code=1),
    ]
    rendered = render_catalog(steps)
    assert rendered.count("\n") == 0
    assert "#0001-#0003" in rendered
    assert "×3" in rendered
    assert "最近=fail" in rendered
    assert "exit=1" in rendered


def test_non_consecutive_repeats_are_not_folded() -> None:
    """中间夹了别的事，就不能合并——否则发生顺序会丢。"""
    steps = [
        _step(1, seq_in_turn=0, target_ref="pytest"),
        _step(2, seq_in_turn=1, target_ref="ruff"),
        _step(3, seq_in_turn=2, target_ref="pytest"),
    ]
    rendered = render_catalog(steps)
    assert rendered.count("\n") == 2
    assert "×" not in rendered


def test_collapse_false_keeps_every_line() -> None:
    steps = [_step(1, seq_in_turn=0), _step(2, seq_in_turn=1)]
    rendered = render_catalog(steps, collapse=False)
    assert rendered.count("\n") == 1
    assert "#0001" in rendered
    assert "#0002" in rendered
    assert "×" not in rendered
