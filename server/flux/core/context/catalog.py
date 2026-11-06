"""目录行（FLUX_CONTEXT_MECHANISM §3.4 / §6.4）。

目录行是 step 表的**极简投影**，一行一条 step，是 agent 进入冷库的**唯一入口**。
必须同时满足四件事（§6.4）：

1. 一行一个 step，`step_id` 露出来——否则 agent 取不了件
2. 带"该不该取"的**判断信号**：状态、涉及文件 / 目标
3. 内联**事实最小锚**（`exit_code`、短 hash、目标）——这样**不取件也能核对**
4. 自身可压缩：连续重复的 step 折叠成一行统计

`fail` 等非 ok 的 step 前面带 `! ` 视觉标记：现实中 agent 倾向于"不取件直接动手"，
完全不提示的话，回看通道是通的但没人走（§6.4 提示性标记）。
"""

from __future__ import annotations

from collections.abc import Sequence

from flux.enums import StepKind, StepStatus
from flux.models.context import ContextStep

#: 目录行里 sha 只露前几位——够核对，又不把行撑爆
_HASH_CHARS = 8
#: 目标截断宽度；目录行是极简投影，不是原文
_TARGET_CHARS = 60

#: kind → 目录行里的动作词（tool_call 例外，直接用工具名）
_ACTIONS: dict[str, str] = {
    StepKind.COMMAND.value: "cmd",
    StepKind.MODEL_CALL.value: "model",
    StepKind.FILE_OP.value: "file",
    StepKind.ASSERTION.value: "assert",
    StepKind.FACT.value: "fact",
}


def catalog_line(step: ContextStep) -> str:
    """一行一个 step（§6.4 的第 1–3 条）。"""
    fields = [
        f"#{step.id:04d}",
        f"{step.turn_id}/s{step.seq_in_turn}",
        step.agent_fp or "-",
        _action_of(step),
        _clip(step.target_ref) or "-",
    ]
    anchors = [step.status]
    if step.exit_code is not None:
        anchors.append(f"exit={step.exit_code}")
    if step.result_sha256:
        anchors.append(f"sha={step.result_sha256[:_HASH_CHARS]}")
    marker = "" if step.status == StepStatus.OK.value else "! "
    return f"{marker}{'  '.join(fields)}  {' '.join(anchors)}"


def render_catalog(steps: Sequence[ContextStep], *, collapse: bool = True) -> str:
    """把一段 step 渲染成目录行文本（§6.4 的第 4 条：自身可压缩）。

    只折**连续**的重复——跨段的相同操作不该合成一行，否则发生顺序会丢。
    """
    lines: list[str] = []
    index = 0
    while index < len(steps):
        group_end = index + 1
        if collapse:
            key = _fold_key(steps[index])
            while group_end < len(steps) and _fold_key(steps[group_end]) == key:
                group_end += 1
        if group_end - index > 1:
            lines.append(_fold_line(steps[index:group_end]))
        else:
            lines.append(catalog_line(steps[index]))
        index = group_end
    return "\n".join(lines)


def _action_of(step: ContextStep) -> str:
    if step.kind == StepKind.TOOL_CALL.value:
        return step.tool or "tool"
    return _ACTIONS.get(step.kind, step.kind)


def _clip(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= _TARGET_CHARS:
        return value
    return f"{value[: _TARGET_CHARS - 1]}…"


def _fold_key(step: ContextStep) -> tuple[str, str, str | None, str | None]:
    """折叠判据：同一归属、同一动作、同一目标才算"同一件事重复做"。"""
    return (step.run_id, _action_of(step), step.tool, step.target_ref)


def _fold_line(group: Sequence[ContextStep]) -> str:
    """把一组连续重复折成"共 N 次，最近一次如何"（§6.4 的例子：共 12 次 pytest）。"""
    first, last = group[0], group[-1]
    tail = f"×{len(group)}  最近={last.status}"
    if last.exit_code is not None:
        tail = f"{tail} exit={last.exit_code}"
    fields = [
        f"#{first.id:04d}-#{last.id:04d}",
        f"{first.turn_id}/s{first.seq_in_turn}",
        first.agent_fp or "-",
        _action_of(first),
        _clip(first.target_ref) or "-",
    ]
    return f"{'  '.join(fields)}  {tail}"
