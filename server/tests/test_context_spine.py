"""事实脊柱测试（FLUX_CONTEXT_MECHANISM §4.2 / §4.3 / 验收 §13-6）。

§4.3 的要求是「第 N 代拿到的事实与第 1 代**逐字一致**」——这条要求只有在脊柱行是
**确定性纯函数**产出时才可能成立。所以这里不测"大致长什么样"，而是把渲染结果**逐字**
钉死：一旦有人往渲染里加了归一化、排序或改写，测试会当场炸。

覆盖三件事：
1. `evidence_line`：四类自带证据（命令 exit / 文件 hash / 用例名 + 结果 / URL）各取所需；
   叙述性 step（模型自述）**不进**脊柱——那是 §4.2 说的"审核对象"。
2. `spine_entries`：结构化 `facts` 被原样渲染（键序稳定、空值过滤、非标量走 JSON）。
3. `fact_spine`：跨 step 逐字去重，且保持**首次出现**的顺序。
"""

from __future__ import annotations

from flux.core.context import fact_spine
from flux.core.context.spine import evidence_line, spine_entries
from flux.enums import StepKind, StepStatus
from flux.models.context import ContextStep

_SHA_A = "9f3c" + "0" * 60


def _step(
    step_id: int,
    *,
    kind: StepKind = StepKind.COMMAND,
    status: StepStatus = StepStatus.OK,
    tool: str | None = None,
    target_ref: str | None = None,
    exit_code: int | None = None,
    result_sha256: str | None = None,
    facts: list[object] | None = None,
) -> ContextStep:
    """构造一条 step（不落库）——脊柱是纯函数，无需数据库参与。"""
    step = ContextStep(
        run_id="run-1",
        turn_id="turn-1",
        seq_in_turn=step_id,
        kind=kind.value,
        status=status.value,
        agent_fp="codex:deepseek-v3",
        tool=tool,
        target_ref=target_ref,
        exit_code=exit_code,
        result_sha256=result_sha256,
        facts=facts,
    )
    step.id = step_id
    return step


def test_command_evidence_carries_exit_code() -> None:
    step = _step(
        1, kind=StepKind.COMMAND, target_ref="pytest -x", exit_code=1, status=StepStatus.FAIL
    )
    assert evidence_line(step) == "pytest -x  exit=1"


def test_command_without_exit_code_falls_back_to_status() -> None:
    step = _step(1, kind=StepKind.COMMAND, target_ref="ls -la", status=StepStatus.OK)
    assert evidence_line(step) == "ls -la  OK"


def test_command_without_target_uses_tool_name() -> None:
    step = _step(1, kind=StepKind.COMMAND, tool="bash", status=StepStatus.OK)
    assert evidence_line(step) == "bash  OK"


def test_file_evidence_truncates_hash_to_twelve_chars() -> None:
    """hash 只露前 12 位——够核对，又不把一行撑爆（与目录行同一取舍）。"""
    step = _step(1, kind=StepKind.FILE_OP, target_ref="src/auth.py", result_sha256=_SHA_A)
    assert evidence_line(step) == f"src/auth.py  sha256={_SHA_A[:12]}"


def test_file_without_hash_is_not_evidence() -> None:
    """没记下内容 hash 的文件操作不可核对，不算证据。"""
    step = _step(1, kind=StepKind.FILE_OP, target_ref="src/auth.py")
    assert evidence_line(step) is None


def test_assertion_evidence_carries_case_name_and_result() -> None:
    step = _step(
        1, kind=StepKind.ASSERTION, target_ref="test_login_expired", status=StepStatus.FAIL
    )
    assert evidence_line(step) == "test_login_expired  FAIL"


def test_url_evidence_wins_over_kind() -> None:
    """URL 自带证据，压过 kind——模型调用只要挂的是可核查的 URL，也算事实。"""
    step = _step(1, kind=StepKind.MODEL_CALL, target_ref="https://docs.example/auth")
    assert evidence_line(step) == "https://docs.example/auth  OK"


def test_url_extracted_from_multiword_target() -> None:
    """target 里混着别的话时也要能把 URL 挑出来。"""
    step = _step(1, kind=StepKind.TOOL_CALL, target_ref="see https://docs.example/auth for ref")
    assert evidence_line(step) == "https://docs.example/auth  OK"


def test_narrative_step_is_not_evidence() -> None:
    """§4.2：模型自述没有自带证据，不进脊柱。"""
    step = _step(1, kind=StepKind.MODEL_CALL, target_ref="我觉得应该没问题")
    assert evidence_line(step) is None


def test_fact_kind_without_url_is_not_evidence() -> None:
    """`fact` 类 step 的证据靠 `facts` 字段承载，主键位没有锚时不硬凑一行。"""
    step = _step(1, kind=StepKind.FACT, target_ref="some note")
    assert evidence_line(step) is None


def test_entries_render_structured_facts_sorted_by_key() -> None:
    step = _step(
        1,
        kind=StepKind.FACT,
        facts=[{"exit": 1, "case": "test_login_expired"}],
    )
    assert spine_entries(step) == ("case=test_login_expired  exit=1",)


def test_entries_render_non_scalar_facts_as_json() -> None:
    """列表 / 字典走 JSON（键序固定、无多余空白）——保证跨代逐字可比。"""
    step = _step(1, kind=StepKind.FACT, facts=[{"paths": ["b.py", "a.py"], "n": 2}])
    assert spine_entries(step) == ('n=2  paths=["b.py","a.py"]',)


def test_entries_filter_blank_values() -> None:
    step = _step(1, kind=StepKind.FACT, facts=[{"keep": "v", "drop": None, "empty": ""}])
    assert spine_entries(step) == ("keep=v",)


def test_entries_skip_empty_dict_fact() -> None:
    step = _step(1, kind=StepKind.FACT, facts=[{}])
    assert spine_entries(step) == ()


def test_entries_render_plain_string_fact_verbatim() -> None:
    step = _step(1, kind=StepKind.FACT, facts=["raw evidence line"])
    assert spine_entries(step) == ("raw evidence line",)


def test_entries_put_evidence_before_facts() -> None:
    step = _step(
        1,
        kind=StepKind.COMMAND,
        target_ref="pytest -x",
        exit_code=0,
        facts=[{"duration_ms": 128}],
    )
    assert spine_entries(step) == ("pytest -x  exit=0", "duration_ms=128")


def test_spine_dedupes_verbatim_and_keeps_first_seen_order() -> None:
    """同一行出现多次只留一次，且保留首次出现的顺序（不做任何归一化）。"""
    steps = [
        _step(1, target_ref="pytest -x", exit_code=1, status=StepStatus.FAIL),
        _step(2, target_ref="ls", exit_code=0),
        _step(3, target_ref="pytest -x", exit_code=1, status=StepStatus.FAIL),
        _step(4, target_ref="pytest -x", exit_code=0, status=StepStatus.FAIL),
    ]
    assert fact_spine(steps) == ("pytest -x  exit=1", "ls  exit=0", "pytest -x  exit=0")


def test_spine_is_empty_for_narrative_only_run() -> None:
    steps = [_step(1, kind=StepKind.MODEL_CALL, target_ref="我修好了")]
    assert fact_spine(steps) == ()


def test_spine_is_stable_across_repeated_evaluation() -> None:
    """§4.3 的关键前提：同样的 steps 每次产出**逐字相同**的脊柱。"""
    steps = [
        _step(1, target_ref="pytest -x", exit_code=1, status=StepStatus.FAIL),
        _step(2, kind=StepKind.FILE_OP, target_ref="src/auth.py", result_sha256=_SHA_A),
        _step(3, kind=StepKind.FACT, facts=[{"a": 1, "b": [2, 3]}]),
    ]
    assert fact_spine(steps) == fact_spine(steps)
