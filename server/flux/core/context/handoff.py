"""交接文档生成（FLUX_CONTEXT_MECHANISM §8.3 / §9）。

交接 = 开一个新 run：**原文不搬**，只搬「事实脊柱 + 意图 + 约束 + 目录行 + 验收标准」，
汇成一份**自给自足**的文档（§8.3），作为接管 agent 的**首投喂**（§9 开头）。本模块生成它。

九节固定（§9），逐节来源与可信度：

| 节 | 来源 | 为什么可信 |
| --- | --- | --- |
| 1 任务与验收标准 | `HandoffBrief.goal` | 退出方显式声明 |
| 2 约束 | `HandoffBrief.constraints` | 同上 |
| 3 已排除方案及原因 | `HandoffBrief.excluded` | 防接管方重走死路 |
| 4 事实脊柱 | 命令 exit / 文件 hash / 用例名 / URL / 结构化 facts | §4.3：取自日志，逐字透传 |
| 5 未决问题 | `HandoffBrief.open_questions` | 退出方显式声明 |
| 6 下一步建议 | `HandoffBrief.next_steps` | 同上 |
| 7 涉及文件清单 | 文件类词条 → 最新一次出现的 step + hash | 从日志聚合 |
| 8 目录行摘要 | `render_catalog` 按 turn 分组 | §6.4 |
| 9 相关 step_id 列表 | 全部 step 的 id | 便捷路径，**非权威依据** |

**叙述层为什么必须由调用方显式给**：§9 的 1/2/3/5/6 节是退出方的自述，日志里的 step
是"做了什么"（证据），不是"想干什么"（意图）。两者不能混（§4.2 的 fact / assertion 二分），
所以这里不猜、不编：`HandoffBrief` 没给的节一律渲染成「（未声明）」。

**第 9 节的已知限制**（§9）：这些 id 指向的第二层原文留在**原会话**，原会话被删即失效。
故第 9 节只是**便捷路径**；权威依据是第 4 节的**事实脊柱**——本文档必须能自给自足地支撑接手。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from flux.core.context.catalog import render_catalog
from flux.core.context.index import extract_tokens
from flux.core.context.spine import fact_spine
from flux.enums import ContextTokenKind
from flux.models.context import ContextStep

#: 事实脊柱里 sha 只露前几位——够核对，又不把一行撑爆（与目录行同一取舍）
_SHORT_HASH_CHARS = 12
#: 未声明的小节占位——不猜、不编
_UNDECLARED = "（未声明）"


@dataclass(frozen=True)
class HandoffBrief:
    """交接文档里的**叙述层**（§9 的 1 / 2 / 3 / 5 / 6 节），由退出方显式给出。

    每项是一串条目（渲染成无序列表）；留空则该节渲染为「（未声明）」。
    """

    goal: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()


def build_handoff(
    run_id: str,
    steps: Sequence[ContextStep],
    brief: HandoffBrief | None = None,
) -> str:
    """把一次 run 的 step 日志 + 退出方叙述，渲染成 §9 的九节交接文档。

    纯函数：不读库、不写盘，`steps` 由调用方从 `ContextStepRepository.list_by_run` 取。
    空 run 也能生成（各节降级为占位），绝不抛错——交接现场最不需要的就是"生成失败"。
    """
    brief = brief or HandoffBrief()
    turns = _turn_groups(steps)
    return "\n\n".join(
        [
            _header(run_id, steps, turns),
            _bullet_section(1, "任务与验收标准", brief.goal),
            _bullet_section(2, "约束（技术 / 业务 / 环境）", brief.constraints),
            _bullet_section(3, "已排除方案及原因", brief.excluded),
            _spine_section(steps),
            _bullet_section(5, "未决问题", brief.open_questions),
            _bullet_section(6, "下一步建议", brief.next_steps),
            _files_section(steps),
            _catalog_section(turns),
            _step_ids_section(steps),
        ]
    )


def _header(
    run_id: str,
    steps: Sequence[ContextStep],
    turns: list[tuple[str, list[ContextStep]]],
) -> str:
    """文档抬头：这是什么、多大、权威依据在哪（§9 的引言）。"""
    span = _time_span(steps)
    lines = [
        f"# 交接文档 · run `{run_id}`",
        "",
        f"- 规模：{len(steps)} 条 step，{len(turns)} 个 turn{span}",
        "- 原文不随本文档交接；核对的权威依据是第 4 节**事实脊柱**（取自日志，逐字透传）。",
    ]
    return "\n".join(lines)


def _bullet_section(number: int, title: str, items: Sequence[str]) -> str:
    body = [f"- {item}" for item in items if item.strip()] or [_UNDECLARED]
    return "\n".join([f"## {number}. {title}", *body])


def _spine_section(steps: Sequence[ContextStep]) -> str:
    """第 4 节：事实脊柱（§4.3）——由 `spine.fact_spine` 唯一产出，本文档只负责渲染。

    脊柱行不在这里加工：换行、去重、前缀都在 §4.3 的纯函数里定死，交接文档与目录行
    取的是同一份结果，第 N 代才能与第 1 代逐字一致。
    """
    lines = [f"- {line}" for line in fact_spine(steps)] or [_UNDECLARED]
    return "\n".join(["## 4. 事实脊柱（核对依据）", *lines])


def _files_section(steps: Sequence[ContextStep]) -> str:
    """第 7 节：涉及文件清单——复用反向索引的文件词条规则，取每次出现的最新版本。"""
    latest: dict[str, ContextStep] = {}
    for step in steps:
        for kind, token in extract_tokens(step):
            if kind is ContextTokenKind.FILE:
                # 全路径与 basename 都记（与反向索引同规则）；最新一次出现的 step 说话
                latest[token] = step
    if not latest:
        return "\n".join(["## 7. 涉及文件清单", _UNDECLARED])
    lines = []
    for path in sorted(latest):
        step = latest[path]
        anchor = f"#{step.id:04d}"
        if step.result_sha256:
            anchor = f"{anchor} sha256={step.result_sha256[:_SHORT_HASH_CHARS]}"
        lines.append(f"- `{path}` — {anchor}")
    return "\n".join(["## 7. 涉及文件清单", *lines])


def _catalog_section(turns: list[tuple[str, list[ContextStep]]]) -> str:
    """第 8 节：目录行摘要——按 turn 分组，组内用 §6.4 的目录行渲染。"""
    lines = ["## 8. 目录行摘要（按 turn）"]
    if not turns:
        lines.append(_UNDECLARED)
        return "\n".join(lines)
    for turn_id, group in turns:
        lines.append(f"### turn `{turn_id}`")
        lines.append("```")
        lines.append(render_catalog(group))
        lines.append("```")
    return "\n".join(lines)


def _step_ids_section(steps: Sequence[ContextStep]) -> str:
    """第 9 节：全部 step_id——便捷路径，原文可能已随原会话消失（§9 已知限制）。"""
    ids = ", ".join(f"#{step.id:04d}" for step in steps) or _UNDECLARED
    return "\n".join(
        [
            "## 9. 相关 step_id（便捷路径，非权威依据）",
            ids,
            "",
            "> 这些 id 指向的原文留在原会话，原会话被删即失效；核对应以第 4 节为准。",
        ]
    )


def _turn_groups(steps: Sequence[ContextStep]) -> list[tuple[str, list[ContextStep]]]:
    """按 `turn_id` 分组，保持**首次出现**的顺序（§8.1：turn 是分组呈现单元）。"""
    groups: dict[str, list[ContextStep]] = {}
    order: list[str] = []
    for step in steps:
        if step.turn_id not in groups:
            groups[step.turn_id] = []
            order.append(step.turn_id)
        groups[step.turn_id].append(step)
    return [(turn_id, groups[turn_id]) for turn_id in order]


def _time_span(steps: Sequence[ContextStep]) -> str:
    stamps = [step.created_at for step in steps if step.created_at is not None]
    if not stamps:
        return ""
    return f"，时间跨度 {min(stamps).isoformat()} → {max(stamps).isoformat()}"
