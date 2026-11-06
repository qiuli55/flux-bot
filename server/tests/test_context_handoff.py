"""交接文档生成测试（FLUX_CONTEXT_MECHANISM §8.3 / §9 / 验收 §13-6）。

`build_handoff` 是**纯函数**（只吃 `steps` 与 `brief`），所以这里直接构造模型对象、
不落库——交接文档的正确性不该依赖数据库行为。

三条底线：九节齐全且有序、叙述层照抄不编、事实脊柱够接管方**不取冷库原文**就能核对（§13-6）。
"""

from __future__ import annotations

from flux.core.context import HandoffBrief, build_handoff
from flux.enums import StepKind, StepStatus
from flux.models.context import ContextStep

_SHA_A = "9f3c" + "0" * 60
_SHA_B = "beef" + "1" * 60

_HEADINGS = [
    "## 1. 任务与验收标准",
    "## 2. 约束（技术 / 业务 / 环境）",
    "## 3. 已排除方案及原因",
    "## 4. 事实脊柱（核对依据）",
    "## 5. 未决问题",
    "## 6. 下一步建议",
    "## 7. 涉及文件清单",
    "## 8. 目录行摘要（按 turn）",
    "## 9. 相关 step_id（便捷路径，非权威依据）",
]


def _step(
    step_id: int,
    *,
    run_id: str = "run-1",
    turn_id: str = "turn-1",
    seq_in_turn: int = 0,
    kind: StepKind = StepKind.COMMAND,
    status: StepStatus = StepStatus.OK,
    tool: str | None = None,
    target_ref: str | None = None,
    exit_code: int | None = None,
    result_sha256: str | None = None,
    facts: list[dict[str, object]] | None = None,
) -> ContextStep:
    """构造一条 step（不落库；`id` 手动指定，因为纯函数要它来写 `#编号`）。"""
    step = ContextStep(
        run_id=run_id,
        turn_id=turn_id,
        seq_in_turn=seq_in_turn,
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


def _sample_run() -> list[ContextStep]:
    return [
        _step(
            1,
            seq_in_turn=0,
            kind=StepKind.COMMAND,
            target_ref="pytest tests/test_auth.py -x",
            exit_code=1,
            status=StepStatus.FAIL,
        ),
        _step(
            2,
            seq_in_turn=1,
            kind=StepKind.FILE_OP,
            target_ref="src/auth/session.py",
            result_sha256=_SHA_A,
        ),
        _step(
            3,
            seq_in_turn=2,
            kind=StepKind.ASSERTION,
            target_ref="test_login_expired",
            status=StepStatus.FAIL,
        ),
        _step(4, seq_in_turn=3, kind=StepKind.MODEL_CALL, target_ref="https://docs.example/auth"),
    ]


def test_document_has_all_nine_sections_in_order() -> None:
    doc = build_handoff("run-1", _sample_run())
    positions = [doc.index(heading) for heading in _HEADINGS]
    assert positions == sorted(positions)
    assert doc.startswith("# 交接文档 · run `run-1`")


def test_narrative_sections_render_brief_verbatim() -> None:
    brief = HandoffBrief(
        goal=("修好 token 过期分支的 refresh", "验收：test_login_expired 通过"),
        constraints=("不许改公共 API 签名",),
        excluded=("重写 validator —— 影响面太大",),
        open_questions=("refresh token 是否要落库？",),
        next_steps=("先复现 test_login_expired",),
    )
    doc = build_handoff("run-1", _sample_run(), brief)

    assert "- 修好 token 过期分支的 refresh" in doc
    assert "- 验收：test_login_expired 通过" in doc
    assert "- 不许改公共 API 签名" in doc
    assert "- 重写 validator —— 影响面太大" in doc
    assert "- refresh token 是否要落库？" in doc
    assert "- 先复现 test_login_expired" in doc


def test_undeclared_narrative_sections_render_placeholder() -> None:
    """没给的节不猜不编，一律「（未声明）」。"""
    doc = build_handoff("run-1", _sample_run())
    assert doc.count("（未声明）") >= 5


def test_fact_spine_carries_evidence_anchors() -> None:
    """§4.3 的四类证据都要在脊柱里：命令 exit、文件 hash、用例名、URL。"""
    doc = build_handoff("run-1", _sample_run())
    assert "pytest tests/test_auth.py -x  exit=1" in doc
    assert f"src/auth/session.py  sha256={_SHA_A[:12]}" in doc
    assert "test_login_expired  FAIL" in doc
    assert "https://docs.example/auth  OK" in doc


def test_fact_spine_excludes_narrative_steps() -> None:
    """叙述性 step（模型自述）不进脊柱——它没有自带证据（§4.2）。"""
    steps = [_step(7, kind=StepKind.MODEL_CALL, target_ref="我觉得应该没问题")]
    spine = build_handoff("run-1", steps).split("## 4.")[1].split("## 5.")[0]
    assert "我觉得应该没问题" not in spine


def test_files_section_keeps_latest_version() -> None:
    steps = [
        _step(
            1, seq_in_turn=0, kind=StepKind.FILE_OP, target_ref="src/auth.py", result_sha256=_SHA_A
        ),
        _step(
            5, seq_in_turn=1, kind=StepKind.FILE_OP, target_ref="src/auth.py", result_sha256=_SHA_B
        ),
    ]
    doc = build_handoff("run-1", steps)
    files = doc.split("## 7.")[1].split("## 8.")[0]
    # 只留最新一次出现的 step（#0005 与 SHA_B），旧版本不重复列
    assert f"- `src/auth.py` — #0005 sha256={_SHA_B[:12]}" in files
    assert _SHA_A[:12] not in files


def test_catalog_section_groups_by_turn_in_first_seen_order() -> None:
    steps = [
        _step(1, turn_id="turn-b", seq_in_turn=0),
        _step(2, turn_id="turn-a", seq_in_turn=0),
        _step(3, turn_id="turn-b", seq_in_turn=1),
    ]
    doc = build_handoff("run-1", steps)
    catalog = doc.split("## 8.")[1].split("## 9.")[0]
    assert catalog.index("turn `turn-b`") < catalog.index("turn `turn-a`")
    # 目录行露出 step 编号，接管方才能取件
    assert "#0001" in catalog and "#0003" in catalog


def test_step_ids_section_lists_every_step() -> None:
    doc = build_handoff("run-1", _sample_run())
    ids = doc.split("## 9.")[1].split("> ")[0]
    for step_id in (1, 2, 3, 4):
        assert f"#{step_id:04d}" in ids


def test_empty_run_still_produces_a_document() -> None:
    """交接现场最不需要的就是"生成失败"：空 run 也要给出一份结构完整的文档。"""
    doc = build_handoff("run-empty", [])
    for heading in _HEADINGS:
        assert heading in doc
    assert "0 条 step" in doc


def test_document_is_self_sufficient_without_fetching_raw() -> None:
    """§13-6：不取冷库原文即可核对——hash 与 exit code 都在文档里。"""
    doc = build_handoff("run-1", _sample_run())
    assert _SHA_A[:12] in doc
    assert "exit=1" in doc
