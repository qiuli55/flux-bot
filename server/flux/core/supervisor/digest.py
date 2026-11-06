"""线程收尾摘要（P4）：把一条线程的工单与报告压成可检索、可进 Brain 的文本。

规则式生成，不调模型：摘要只陈述平台已记录的事实（阶段 + 报告结论），
模型判断留在 eval 里，两件事不混。生成时机由 orchestrator 控制：
线程内全部工单都已收尾，才允许写一份，且按 thread_id 幂等。
"""

from __future__ import annotations

import uuid
from typing import Any

from flux.enums import TicketStage

__all__ = [
    "PROGRESS_HEADER",
    "TERMINAL_STAGES",
    "build_thread_digest",
    "is_closed",
    "render_progress",
]

#: 视为"已收尾"的阶段：done 与 stopped（停止也是明确结论，不再等）
TERMINAL_STAGES = frozenset({TicketStage.DONE.value, TicketStage.STOPPED.value})

_STAGE_LABELS: dict[str, str] = {
    TicketStage.DONE.value: "完成",
    TicketStage.STOPPED.value: "停止",
}

PROGRESS_HEADER = "## 项目进度（由助理自动汇总，覆盖写入）"


def is_closed(items: list[dict[str, Any]]) -> bool:
    """线程是否已收尾：至少一张工单，且全部处于终态。"""
    return bool(items) and all(item.get("stage") in TERMINAL_STAGES for item in items)


def build_thread_digest(
    *,
    thread_id: uuid.UUID,
    thread_key: str,
    project_id: uuid.UUID | None,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """把一条线程的工单快照压成 `summary / decisions / ticket_ids`。"""
    ordered = sorted(items, key=lambda item: (item.get("sequence") or 0, str(item["id"])))
    done = sum(1 for item in ordered if item.get("stage") == TicketStage.DONE.value)
    stopped = sum(1 for item in ordered if item.get("stage") == TicketStage.STOPPED.value)

    lines = [f"会话 {thread_key}：{len(ordered)} 张工单，完成 {done}、停止 {stopped}。"]
    decisions: list[dict[str, Any]] = []
    for item in ordered:
        report = item.get("report") or {}
        label = _STAGE_LABELS.get(str(item.get("stage")), str(item.get("stage")))
        summary = str(report.get("summary") or "").strip()
        lines.append(f"- [{label}] {item['title']}：{summary or '无报告'}")
        decisions.append(
            {
                "ticket_id": str(item["id"]),
                "title": item["title"],
                "stage": item.get("stage"),
                "report_status": report.get("status"),
                "summary": summary,
            }
        )
    return {
        "thread_id": thread_id,
        "project_id": project_id,
        "summary": "\n".join(lines),
        "decisions": decisions,
        "ticket_ids": [str(item["id"]) for item in ordered],
    }


def render_progress(digests: list[dict[str, Any]]) -> str:
    """把最近若干条线程摘要渲染成 Project Brain 的 progress 分区内容。"""
    blocks: list[str] = [PROGRESS_HEADER]
    for digest in digests:
        summary = str(digest.get("summary") or "").strip()
        if not summary:
            continue
        blocks.append(f"\n{summary}")
    return "\n".join(blocks).strip()
