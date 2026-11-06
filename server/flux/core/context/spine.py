"""事实脊柱（FLUX_CONTEXT_MECHANISM §4.3）。

§4.2 的二分是这套机制的地基：**事实**（`fact`）自带证据——文件 hash、命令 exit code、
工具返回原文、用例名、URL，可核查；**断言**（`assertion`）是 agent 自述"我修好了"，
默认 `unverified`，接管方必须走验证路径。没有这个二分，"不信任"就没有可核的对象。

§4.3 的问题：若 A 的产物压一次给 B，B 的产物再压一次给 C，事实会逐代被"摘要"磨平，
到第 N 代，C 想核对的东西已经不存在了。对策是**把事实拎成一条脊柱，逐代原样透传、
永不重压**——只让叙述层递归压缩。

所以脊柱行必须由**同一个纯函数**产出：交接文档（§9 第 4 节）与目录行（§6.4 内联锚）
都从它取，不接受任何二次加工。本模块不碰数据库、不调模型，只做确定性渲染——
这正是"第 N 代与第 1 代逐字一致"的前提。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from flux.enums import StepKind
from flux.models.context import ContextStep

#: 脊柱里 sha 只露前几位——够核对，又不把一行撑爆（与目录行同一取舍）
_SHORT_HASH_CHARS = 12
#: 像 URL 的词：`scheme://host/...`
_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/?#\s]+")


def fact_spine(steps: Sequence[ContextStep]) -> tuple[str, ...]:
    """把一次 run 的全部 step 收敛成事实脊柱（去重、保持发生顺序）。

    去重是**逐字**的：同一行出现两次就只留一次，不做任何归一化、折叠或改写——
    一旦在这里动了手脚，下面几代拿到的就不是"原样"了，§4.3 也就白设计了。
    """
    lines: list[str] = []
    seen: set[str] = set()
    for step in steps:
        for entry in spine_entries(step):
            if entry in seen:
                continue
            seen.add(entry)
            lines.append(entry)
    return tuple(lines)


def spine_entries(step: ContextStep) -> tuple[str, ...]:
    """一条 step 贡献的脊柱行：自带证据的锚点（若有）+ 它记下的结构化 `facts`。

    `facts` 是调用方在写 step 时就落好证据的地方；这里**只渲染不改写**，
    渲染结果跨代稳定（字典按键序、非标量走 JSON），故可逐字比对。
    """
    entries: list[str] = []
    evidence = evidence_line(step)
    if evidence:
        entries.append(evidence)
    entries.extend(_fact_lines(step.facts))
    return tuple(entries)


def evidence_line(step: ContextStep) -> str | None:
    """从一个 step 抽出事实脊柱的一行；不是「自带证据」的 step 返回 `None`（§4.2）。

    只有命令（exit code）、文件操作（内容 hash）、断言（用例名 + 结果）与 URL 算证据；
    `fact` 之外的叙述性 step 不进脊柱——那正是 §4.3 说的"审核对象"。
    """
    status = step.status.upper()
    url = _url_target(step.target_ref)
    if url:
        return f"{url}  {status}"
    if step.kind == StepKind.COMMAND.value:
        target = step.target_ref or step.tool or "command"
        if step.exit_code is not None:
            return f"{target}  exit={step.exit_code}"
        return f"{target}  {status}"
    if step.kind == StepKind.FILE_OP.value and step.result_sha256:
        target = step.target_ref or "file"
        return f"{target}  sha256={step.result_sha256[:_SHORT_HASH_CHARS]}"
    if step.kind == StepKind.ASSERTION.value:
        return f"{step.target_ref or 'assertion'}  {status}"
    return None


def _fact_lines(facts: Sequence[Any] | None) -> tuple[str, ...]:
    """把 step 上记的结构化事实渲染成脊柱行（逐字透传，见模块文档）。"""
    if not facts:
        return ()
    lines: list[str] = []
    for fact in facts:
        rendered = _render_fact(fact)
        if rendered:
            lines.append(rendered)
    return tuple(lines)


def _render_fact(fact: Any) -> str:
    if isinstance(fact, dict):
        pairs = [
            f"{key}={_scalar_or_json(value)}"
            for key, value in sorted(fact.items())
            if value is not None and str(value) != ""
        ]
        return "  ".join(pairs)
    return "" if fact is None else str(fact)


def _scalar_or_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int | float | bool):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _url_target(value: str | None) -> str | None:
    for word in (value or "").split():
        if _URL_RE.match(word):
            return word
    return None
