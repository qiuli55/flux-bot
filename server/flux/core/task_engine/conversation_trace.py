"""把任务对话压成一段可注入执行指令的轨迹（跨轮记忆的「对话」半边）。

执行 Agent 此前每轮只拿到当前消息与最初任务描述，看不到「中间聊过什么」，
于是长对话里会把最初的需求当成当前任务重做。这里从 `task_messages` 取最近若干
条可读消息，按时间顺序压成短行，装进指令。

只收用户能看懂、且对下一轮有用的类型：
- `text`：用户/助手的正文；
- `summary`：从上一段对话接手的摘要；
- `attachments` / `confirmation`：用户传过什么、确认过什么；
- `run_finished`：上一轮的结果落款（成功/失败/无文字回复）；
- `handoff`：切换 Agent 时平台生成的交接卡（新 Agent 接手必读）。

预算从最旧开始丢：越新的消息越接近当前意图，必须留下。
"""

from __future__ import annotations

from typing import Any

#: 进入轨迹的消息类型（与聊天记录的可读类型对齐，不进纯系统事件）
TRACE_MESSAGE_KINDS: tuple[str, ...] = (
    "text",
    "summary",
    "attachments",
    "confirmation",
    "run_finished",
    "handoff",
)

#: 最多带多少条消息进轨迹
TRACE_MAX_MESSAGES = 12
#: 单条消息在轨迹里的字符上限
TRACE_MESSAGE_CHARS = 600
#: 整段轨迹的字符预算
TRACE_BUDGET_CHARS = 3000
#: 续聊（Runtime 已复用会话）时的轨迹收缩口径：历史本来就在会话里，
#: 这里只为「复用失败退回新会话」留一条安全带，不需要再整段复述。
TRACE_RESUMED_MAX_MESSAGES = 4
TRACE_RESUMED_BUDGET_CHARS = 800

_ROLE_LABELS = {"user": "用户", "assistant": "助手"}
_KIND_LABELS = {
    "summary": "前情摘要",
    "attachments": "附件",
    "confirmation": "需求确认",
    "run_finished": "上一轮结果",
    "handoff": "Agent 交接",
}


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _label(message: Any) -> str:
    kind = str(getattr(message, "kind", "") or "")
    if kind in _KIND_LABELS:
        return _KIND_LABELS[kind]
    return _ROLE_LABELS.get(str(getattr(message, "role", "") or ""), "对话")


def build_dialogue_trace(
    messages: list[Any],
    *,
    budget_chars: int = TRACE_BUDGET_CHARS,
    max_messages: int = TRACE_MAX_MESSAGES,
    exclude_text: str = "",
    exclude_texts: tuple[str, ...] = (),
    exclude_kinds: tuple[str, ...] = (),
) -> str:
    """按 seq 升序的消息 → 轨迹段；没有可用消息时返回空串。

    `exclude_text` / `exclude_texts` 用于排除本轮指令里已经单独给出的用户消息
    （本轮任务、原任务背景）：同一句话在指令里出现两次就是白付 token。

    `exclude_kinds` 用于排除「指令里已有独立段落」的消息类型：前情摘要 / 附件 /
    需求确认这些内容一旦单独成段，再进轨迹就是同一份信息付两遍 token。
    """
    excluded_norms = {
        " ".join(text.split()) for text in (exclude_text, *exclude_texts) if text.strip()
    }
    skipped_kinds = {kind for kind in exclude_kinds if kind}
    lines: list[str] = []
    for message in messages:
        kind = str(getattr(message, "kind", "") or "")
        if kind not in TRACE_MESSAGE_KINDS or kind in skipped_kinds:
            continue
        content = (getattr(message, "content", "") or "").strip()
        if not content:
            continue
        normalized = " ".join(content.split())
        if (
            excluded_norms
            and str(getattr(message, "role", "") or "") == "user"
            and normalized in excluded_norms
        ):
            continue
        lines.append(f"[{_label(message)}] {_clip(content, TRACE_MESSAGE_CHARS)}")
    if not lines:
        return ""

    # 先限制条数，再从最旧开始丢字符：新消息永远优先保留
    selected = lines[-max(1, max_messages) :]
    kept: list[str] = []
    used = 0
    for line in reversed(selected):
        cost = len(line) + 1
        if kept and used + cost > max(1, budget_chars):
            break
        if not kept and cost > max(1, budget_chars):
            # 最新一条也超预算：硬截到预算内，至少留下当前意图
            line = _clip(line, max(1, budget_chars))
            cost = len(line)
        kept.append(line)
        used += cost
    kept.reverse()
    header = f"## 对话轨迹（最近 {len(kept)} 条，已处理过的内容不要重做）"
    return header + "\n" + "\n".join(kept)


__all__ = [
    "TRACE_BUDGET_CHARS",
    "TRACE_MAX_MESSAGES",
    "TRACE_MESSAGE_CHARS",
    "TRACE_MESSAGE_KINDS",
    "TRACE_RESUMED_BUDGET_CHARS",
    "TRACE_RESUMED_MAX_MESSAGES",
    "build_dialogue_trace",
]
