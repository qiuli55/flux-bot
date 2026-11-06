"""上下文打包器（FLUX_CONTEXT_MECHANISM §6.2 / §6.3）。

`context.get` 的实现：把 Flux 侧已有的工程事实（任务、项目 Brain、近期提案）
按预算拼成一段可直接投喂 agent 的文本。

装配规则来自 §6.1–6.3：

- **预算**由 `budget.FeedBudget` 推导（窗口 × 安全系数 − 预留输出 − 系统占用）；
  调用方显式传 `budget` 时以传入值为上限。
- **分区**（`Zone`）：任务与项目 Brain 是必留区（`P0`，先占预算、一条不减）；
  近期提案是可压区（`P1`，装得下就原文，装不下才走 §5.3 流水线编码）。
- **等级**（`Level`）：装得下 → `L1` 原文；需编码 → `L2`；落不下 → `L3` 仅 ref。
- **分层**（`Tier`，§6.5）：按年资分热 / 温 / 冷——装配按**热 → 温 → 冷**占预算，
  预算吃紧时**温层先编码、冷层最后降级为 ref**，热层永不替换（渐进替换，避免语义断层）。

全程不调模型：编码用 S2 结构抽取（+ 必要时 S3 抽取式筛选），产物先过 S5 逐字自校验
（对不上就整条作废、降级为引用），再把 §5.6 的度量一并报出（§5.1 / §5.3）。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from flux.core.context.budget import DEFAULT_BUDGET_CHARS, Level, Zone
from flux.core.context.compress import extract, select, verify
from flux.core.context.metrics import FeedMetrics
from flux.core.context.redact import redact
from flux.core.context.tiers import Tier, tier_rank
from flux.errors import ValidationError
from flux.logging import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:  # pragma: no cover - 仅为类型标注，避免容器与本模块互相导入
    from flux.container import Container

#: 打包时最多带多少条近期提案（只带元信息，正文由 workspace.read 取）
MAX_CHANGES = 20
#: 引用清单自身的上限，避免"被裁掉的条目"反过来把预算吃光
MAX_DROPPED_REFS = 50
#: 失败提案回灌给下一轮的失败原因上限：只留尾部（测试输出最有用的部分在末尾）
APPLY_ERROR_CONTEXT_CHARS = 800
#: 上下文里最多带几条历史 Run（终止原因回写用；只带最近的几次就够）
MAX_RECENT_RUNS = 5
#: 单条 Run 的错误摘要上限：错误可能是整段栈，上下文只需要能定位问题的尾部
RUN_ERROR_CONTEXT_CHARS = 400
#: S3 折算行数时的单行估算长度：把"剩余字符"换成"还能留几行"
_ENCODED_LINE_CHARS = 120

#: 固定写进每个包尾的采信规则（§6.4）
PROVENANCE_NOTE = (
    "本包内容全部是 Flux 侧存档的工程事实，历史条目为快照；"
    "断言不等于事实，动手前请用 workspace.read / workspace.diff 核对磁盘当前状态。"
)

#: 每类 entry 属于哪个分区（§6.2）：必留区先占预算，可压区才能被编码，可取区只留 ref
_ZONE_BY_KIND: dict[str, Zone] = {
    "task": Zone.P0,
    "verify": Zone.P0,
    "brain": Zone.P0,
    "runs": Zone.P1,
    "proposals": Zone.P1,
    "steps": Zone.P2,
}

#: 每类 entry 属于哪一层（§6.5）：任务与 Brain 是当轮事实（热，永不替换）；
#: 近期 Run 与提案是中段，预算吃紧时先编码；历史 step 是远段，最后才降级为 ref。
_TIER_BY_KIND: dict[str, Tier] = {
    "task": Tier.HOT,
    "verify": Tier.HOT,
    "brain": Tier.HOT,
    "runs": Tier.WARM,
    "proposals": Tier.WARM,
    "steps": Tier.COLD,
}

#: 被裁掉的引用 → 取回它的调用提示（§6.3 第三档"只给引用"的消费方）。
#: 打包器只给引用，agent 需要细节时按提示去取，而不是要求把原文塞进投喂。
_RETRIEVE_HINTS: dict[str, str] = {
    "task": "context.get(task_id=<id>)",
    "verify": "context.get(task_id=<id>)",
    "brain": "context.get(project_id=<id>)",
    "runs": "context.get(task_id=<id>) / context.steps(run_id=<run_id>)",
    "proposals": "context.get(task_id=<id>) / workspace.diff(change_id=<提案 id>)",
    "step": "context.fetch(step_id=<id>)",
}


def _retrieve_hint(ref: str) -> str:
    return _RETRIEVE_HINTS.get(ref.split(":", 1)[0], "context.get()")


async def package_context(
    container: Container,
    *,
    task_id: str | None = None,
    project_id: str | None = None,
    budget: int | None = None,
) -> dict[str, Any]:
    """打一个上下文包。task_id 为空表示只按 project_id（或全工作区）打包。

    `budget` 为 `None` 时用 §6.1 推导的默认预算（`budget.DEFAULT_BUDGET_CHARS`）。
    """
    if budget is not None and budget <= 0:
        raise ValidationError("budget 必须是正整数", details={"budget": budget})
    ceiling = DEFAULT_BUDGET_CHARS if budget is None else budget
    task_uuid = _parse_uuid(task_id, field="task_id")
    project_uuid = _parse_uuid(project_id, field="project_id")

    entries: list[dict[str, Any]] = []
    query = ""
    if task_uuid is not None:
        task = await container.task_repo.get(task_uuid)
        # 任务自带项目归属时自动带上该项目的 Brain，省掉调用方一次显式传参
        if project_uuid is None and task.project_id is not None:
            project_uuid = task.project_id
        query = task.description
        entries.append(
            _entry(
                kind="task",
                title=f"任务 {task.id}",
                text="\n".join(
                    [
                        "# 当前任务（L1）",
                        f"- id：{task.id}",
                        f"- 状态：{task.status}",
                        f"- 优先级：{task.priority}",
                        f"- 项目：{task.project_id or '未关联'}",
                        "",
                        task.description,
                    ]
                ),
                ref=f"task:{task.id}",
            )
        )
        recent_runs = await _recent_runs_entry(container, task_uuid)
        if recent_runs is not None:
            entries.append(recent_runs)

    if project_uuid is not None:
        brain_text = await container.brain.context(project_uuid)
        entries.append(
            _entry(
                kind="brain",
                title=f"项目 Brain {project_uuid}",
                text=brain_text,
                ref=f"brain:{project_uuid}",
            )
        )

    # 验证命令（P0）：这是 Apply 之后平台**真会执行**的那条命令，不是建议。
    # 提前告诉 agent，是为了让它提交提案前先自己跑一遍，而不是交卷后才发现测试挂。
    gate = (container.settings.test_command or "").strip()
    if gate:
        entries.append(_verify_entry(gate, task_id=task_uuid, project_id=project_uuid))
        logger.debug("上下文包含验证命令 task=%s gate=%s", task_uuid, gate)

    # P1 只带与本次打包直接相关的提案：有任务就按任务取，否则按项目取。
    # 不能两个条件同时下（仓库层是 AND）——提案常常只挂了 task_id 没有 project_id，
    # 那样会被过滤成空；也不能在任务包里顺带拉全项目提案，那正是 §6 要防的跨任务污染。
    if task_uuid is not None:
        changes = await container.workspace.list(task_id=task_uuid)
    elif project_uuid is not None:
        changes = await container.workspace.list(project_id=project_uuid)
    else:
        changes = await container.workspace.list()
    recent = changes[-MAX_CHANGES:]
    if recent:
        lines = ["# 近期提案（P1 可压区，人审队列）"]
        for change in recent:
            lines.append(_proposal_line(change))
        failed = sum(1 for change in recent if (getattr(change, "apply_error", None) or ""))
        if failed:
            logger.info("上下文回灌失败原因 task=%s failed=%d/%d", task_uuid, failed, len(recent))
        entries.append(
            _entry(
                kind="proposals",
                title=f"近期提案（{len(recent)}/{len(changes)} 条）",
                text="\n".join(lines),
                ref=f"proposals:{task_uuid or project_uuid or 'workspace'}",
            )
        )

    result = _apply_budget(
        entries, budget=ceiling, project_id=project_uuid, task_id=task_uuid, query=query
    )
    # 回补率观测（§6.7）：把本次给出去的引用数记进观测账，取回时再记 fetch。
    # 持续偏高即说明压过头，应回退装配等级——判据由观测账统一给出。
    observer = container.context_backfill
    observer.note_offered(result["dropped_count"])
    result["backfill"] = observer.as_dict()
    return result


def _entry(*, kind: str, title: str, text: str, ref: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "title": title,
        "text": text,
        "ref": ref,
        "zone": _ZONE_BY_KIND[kind],
        "tier": _TIER_BY_KIND[kind],
    }


async def _recent_runs_entry(container: Container, task_id: uuid.UUID) -> dict[str, Any] | None:
    """把该任务最近若干 Run 的终态与终止原因带回上下文（P1，温层）。

    上一轮失败/超时的 `finish_reason` 与 `error` 原本只落在 `agent_runs` 表里，
    Agent 下一轮读不到——它只能看到任务还在、却不知道自己上次为什么没做完。
    没有终态 Run 时不产出条目，避免给每个新任务加一条空壳。
    """
    from flux.core.agent_runtime.run_repository import is_terminal

    runs = await container.run_repo.list_by_task(task_id, limit=MAX_RECENT_RUNS)
    finished = [run for run in runs if is_terminal(run.status)]
    if not finished:
        return None
    lines = ["# 最近 Run（L1，历史终态）"]
    for run in finished:
        lines.append(
            f"- [{run.status}] run={run.id}"
            f"  finish_reason={run.finish_reason or '-'}"
            + (f"  finished_at={run.finished_at.isoformat()}" if run.finished_at else "")
        )
        error = (run.error or "").strip()
        if error:
            # Run 的错误可能带出模型输出里的密钥；进上下文前统一过脱敏
            lines.append(f"  错误：{redact(error[-RUN_ERROR_CONTEXT_CHARS:]).text}")
    return _entry(
        kind="runs",
        title=f"最近 Run（{len(finished)} 条）",
        text="\n".join(lines),
        ref=f"runs:{task_id}",
    )


def _verify_entry(gate: str, *, task_id: uuid.UUID | None, project_id: uuid.UUID | None) -> dict:
    """把 Apply 门禁命令打进必留区，让 agent 在提交提案前先自查（§7.6）。"""
    scope = str(task_id or project_id or "workspace")
    text = "\n".join(
        [
            "# 验证命令（L1，Apply 之后平台会真跑这条）",
            f"- 命令：{gate}",
            "- 提交提案前用 proposal.preview 带同一份 payload 预演一遍（改动落进临时副本",
            "  再跑这条命令）；terminal.execute 跑的是尚未落盘的旧代码，不能当自检；",
            "  不通过就改到通过，别把没过测试的改动交进人审队列。",
        ]
    )
    return _entry(kind="verify", title="验证命令（提交前自检）", text=text, ref=f"verify:{scope}")


def _proposal_line(change: Any) -> str:
    """一条提案的单行摘要；失败提案额外带上 Apply 的失败原因。

    这是「反馈回灌」的入口：上一轮挂在测试上的提案，下一轮 agent 一读上下文就能看到
    失败输出，而不是在同一个坑里重犯。输出按字符截断，避免一条长栈把预算吃光。
    """
    line = (
        f"- [{change.status}] {change.file_path}"
        f"（+{change.added_lines}/-{change.removed_lines}，{change.hunks} 个改动块）"
        f" id={change.id}" + (f" 说明：{change.summary}" if change.summary else "")
    )
    error = (getattr(change, "apply_error", None) or "").strip()
    if not error:
        return line
    tail = error[-APPLY_ERROR_CONTEXT_CHARS:]
    return f"{line}\n  失败原因：{tail}"


def _apply_budget(
    entries: list[dict[str, Any]],
    *,
    budget: int,
    project_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
    query: str,
) -> dict[str, Any]:
    """分级装配（§6.2 / §6.3）。

    必留区（`P0`）先占预算、**一条不减**；可压区（`P1`）装得下就原文（`L1`），
    装不下才编码（`L2`），编码后仍装不下就整条退化成引用（`L3`）——不做半截截断，
    半截历史比没有历史更容易误导。

    装配次序按层（§6.5）：热 → 温 → 冷。这是**稳定排序**，同层保持调用方给的次序，
    于是"温层先编码、冷层最后降级"自然成立——先装配的层先吃掉余量，轮到冷层时余量
    已尽，只能退化成 ref；热层排在最前，故永不落到这一步。
    """
    entries = sorted(entries, key=lambda entry: tier_rank(entry["tier"]))
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    required = sum(len(entry["text"]) for entry in entries if entry["zone"] is Zone.P0)
    source_chars = sum(len(entry["text"]) for entry in entries)
    used = 0
    remaining = budget - required
    #: S5 的账（§5.6「重建一致率」）：`verified` 是跑过的自校验次数，`verified_ok` 是
    #: 通过的次数。两者不等即为 bug——不过的产物已经作废、整条降级为引用。
    verified = 0
    verified_ok = 0

    for entry in entries:
        zone = entry["zone"]
        size = len(entry["text"])
        if zone is Zone.P0:
            kept.append({**entry, "level": Level.L1, "chars": size})
            used += size
            continue
        if zone is Zone.P2:
            # 可取区（§6.2）：只留 ref，按需 fetch
            dropped.append(entry["ref"])
            continue
        if size <= remaining:
            kept.append({**entry, "level": Level.L1, "chars": size})
            used += size
            remaining -= size
            continue
        if remaining > 0:
            verified += 1
        encoded = _encode(entry["text"], budget=remaining, query=query)
        if encoded is not None:
            verified_ok += 1
            kept.append({**entry, "text": encoded, "level": Level.L2, "chars": len(encoded)})
            used += len(encoded)
            remaining -= len(encoded)
        else:
            dropped.append(entry["ref"])

    content = "\n\n".join(f"## {item['title']}\n{item['text']}" for item in kept)
    metrics = FeedMetrics(
        source_chars=source_chars,
        fed_chars=used,
        # 必留区一条不减（§5.4）：命中率恒 100% 的观测形式就是这两项相等
        required_total=required,
        required_kept=required,
        verified=verified,
        verified_ok=verified_ok,
        dropped_count=len(dropped),
    )
    return {
        "task_id": str(task_id) if task_id else None,
        "project_id": str(project_id) if project_id else None,
        "budget": budget,
        # 必留区体积与可压区余量：§6.1 公式的账，供调用方核对与观测
        "required": required,
        "available": max(0, budget - required),
        "used": used,
        # §5.6 度量：压缩率 / 必留项命中率 / 重建一致率（另三项在别处量）
        "metrics": metrics.as_dict(),
        "entries": [
            {
                "kind": item["kind"],
                "title": item["title"],
                "ref": item["ref"],
                "zone": str(item["zone"]),
                "tier": str(item["tier"]),
                "level": str(item["level"]),
                "chars": item["chars"],
            }
            for item in kept
        ],
        "dropped_refs": dropped[:MAX_DROPPED_REFS],
        # 每条被裁引用配一个取回提示：第三档降级不再是"断了"的死路（§7.1 / §11）
        "dropped": [
            {"ref": ref, "retrieve": _retrieve_hint(ref)} for ref in dropped[:MAX_DROPPED_REFS]
        ],
        "dropped_count": len(dropped),
        "content": content,
        "note": PROVENANCE_NOTE,
    }


def _encode(text: str, *, budget: int, query: str) -> str | None:
    """P1 溢出自适应编码：S2 结构抽取 → 必要时 S3 抽取式筛选 → S5 自校验（§5.3）。

    全程是**选择**而非生成：产物里每一行都逐字来自原文。三道关都过了才交回产物：

    1. 结构抽取的结果装得下（`len <= budget`）就直接用它；
    2. 装不下：再按"与当前任务的相关性"筛掉中段——温层先编码、冷层才降级（§6.5）；
    3. **S5 逐字回验**（`verify`）：不过就作废，返回 `None`，由调用方整条降级为引用。

    "作废"也包含"编码后仍装不下"：宁可不压，不可压错（§5.3 S5 / §6.2）。
    """
    if budget <= 0:
        return None
    extraction = extract(text)
    rendered = "\n".join(extraction.lines)
    if len(rendered) <= budget:
        candidate = rendered
    else:
        limit = max(1, budget // _ENCODED_LINE_CHARS)
        candidate = "\n".join(select(extraction.lines, query, limit=limit).items)
    if not candidate or len(candidate) > budget:
        return None
    if not verify(text, candidate).ok:
        return None
    return candidate


def _parse_uuid(value: str | None, *, field: str) -> uuid.UUID | None:
    if value is None or not str(value).strip():
        return None
    try:
        return uuid.UUID(str(value).strip())
    except ValueError as exc:
        raise ValidationError(
            f"{field} 必须是合法 UUID：{value}", details={field: str(value)}
        ) from exc
