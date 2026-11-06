"""执行过程中的决策点（文档 §5 决策模式）。

Agent 执行中遇到可选方案时，按任务级策略分两条路：

- 模式 A（`DecisionMode.AUTO`）：平台按候选方案里的推荐项自行拍板，不打断用户，
  决策记录照样留痕（"最终结果里能查到用了哪个方案"）；
- 模式 B（`DecisionMode.MANUAL`）：任务停在 `waiting_for_user_decision`，
  候选方案、影响与推荐依据展示给用户，用户选完再从原状态继续。

本模块只负责"决策点记录的构造与校验"这类纯逻辑，不碰数据库与 HTTP——
落库在 TaskRepository，路由在 api/v1/tasks.py。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from flux.enums import DecisionMode, DecisionStatus
from flux.errors import ValidationError

#: 一次决策最多给几个候选方案（再多用户也读不完，反而更慢做决定）
MAX_OPTIONS = 6


def build_decision(
    *,
    question: str,
    options: Any,
    context: str | None,
    recommendation: str | None,
    mode: DecisionMode,
) -> dict[str, Any]:
    """构造一条决策点记录：auto 模式当场按推荐方案拍板，manual 模式挂起等用户。"""
    parsed = parse_options(options)
    auto = mode is DecisionMode.AUTO
    chosen = pick_recommended(parsed) if auto else None
    now = _now()
    return {
        "id": uuid.uuid4().hex,
        "question": question.strip(),
        "context": (context or "").strip() or None,
        "options": parsed,
        "recommendation": (recommendation or "").strip() or None,
        "mode": str(mode),
        "status": str(DecisionStatus.AUTO_RESOLVED if auto else DecisionStatus.PENDING),
        "chosen": chosen,
        "note": None,
        "raised_at": now,
        "resolved_at": now if auto else None,
    }


def parse_options(raw: Any) -> list[dict[str, Any]]:
    """校验候选方案：2~6 个、label 非空且互不重复。"""
    if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_OPTIONS:
        raise ValidationError(
            f"候选方案必须是 2~{MAX_OPTIONS} 项的数组", details={"field": "options"}
        )
    options: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValidationError("候选方案每项必须是对象", details={"item": item})
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            raise ValidationError("候选方案的 label 不能为空", details={"item": item})
        key = label.strip()
        if key in seen:
            raise ValidationError(f"候选方案的 label 重复：{key}", details={"label": key})
        seen.add(key)
        options.append(
            {
                "label": key,
                "description": _text(item.get("description")),
                "impact": _text(item.get("impact")),
                "recommended": item.get("recommended") is True,
            }
        )
    return options


def pick_recommended(options: list[dict[str, Any]]) -> str:
    """auto 模式用：取标记为推荐的方案；都没标就用第一个（总要有个结论）。"""
    for option in options:
        if option["recommended"]:
            return str(option["label"])
    return str(options[0]["label"])


def resolve_decision(record: dict[str, Any], *, option: str, note: str | None) -> dict[str, Any]:
    """用户选了某个候选方案：写回选项与备注，决策点转为 resolved。"""
    labels = [option_item["label"] for option_item in record.get("options") or []]
    if option not in labels:
        raise ValidationError(f"所选项不在候选方案里：{option}", details={"allowed": labels})
    return {
        **record,
        "status": str(DecisionStatus.RESOLVED),
        "chosen": option,
        "note": (note or "").strip() or None,
        "resolved_at": _now(),
    }


def reject_decision(record: dict[str, Any], *, note: str | None) -> dict[str, Any]:
    """用户拒绝全部候选方案：决策点转 rejected，等 Agent 重新给方案（用户可再被问一次）。"""
    return {
        **record,
        "status": str(DecisionStatus.REJECTED),
        "chosen": None,
        "note": (note or "").strip() or None,
        "resolved_at": _now(),
    }


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "MAX_OPTIONS",
    "build_decision",
    "parse_options",
    "pick_recommended",
    "reject_decision",
    "resolve_decision",
]
