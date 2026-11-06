"""对话上下文占用度量（手机端 / 桌面端「上下文快满」提醒的同一份口径）。

这里度量的不是模型标称窗口，而是**执行 Agent 下一轮真正会收到的内容**：
平台每轮装配的执行指令已经把历史压成「对话轨迹」（最近 12 条 / 3000 字封顶）
与「上一轮工作记忆」（1200 字封顶），所以对话再长，投喂体积也不会无限增长。

因此占用只看字符体积，不再把「消息条数」当成满格信号：条数一旦达到上限就
永远显示 100%，会让用户误以为压缩没生效，而实际投喂的仍是压缩后的那段内容。
原始消息条数 / 条数上限只作为解释信息返回，不参与比例与等级。

预算由 `flux.core.context.budget` 推导，不在这里另写常量，换模型或调预算时
只改那一处。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flux.core.context.budget import (
    DEFAULT_BUDGET_CHARS,
    ESTIMATED_MESSAGE_CHARS,
    history_message_limit,
)

#: 进入模型上下文的消息类型：正文消息、上一段对话的前情摘要，以及安全输入的
#: 卡片与「已安全存储」回执（两者的正文都是安全文案，真正的密钥从不落消息表）。
#: 助手历史、上下文度量、摘要注入必须共用这一份清单，否则三处口径会漂移。
CONTEXT_MESSAGE_KINDS: tuple[str, ...] = (
    "text",
    "summary",
    "handoff",
    "secure_input",
    "secure_input_result",
)

#: 达到这个占用比例就提醒「上下文快满了，要不要新建对话」。
CONTEXT_WARNING_RATIO = 0.8

#: 占用等级：normal / warning / full。
LEVEL_NORMAL = "normal"
LEVEL_WARNING = "warning"
LEVEL_FULL = "full"


def context_budget_chars() -> int:
    """执行 Agent 一轮真正能收到的字符预算（§6.1 的完整可投喂预算）。

    旧口径只算「分给对话历史的 40%」，与执行 Agent 实际收到的
    「指令（含对话轨迹/工作记忆）+ 最近消息」不是同一件事，百分比会偏乐观。
    """
    return DEFAULT_BUDGET_CHARS


def context_message_limit() -> int:
    """一次能带进模型的消息条数上限（与 API 取历史的 limit 同源）。"""
    return history_message_limit()


@dataclass(frozen=True, slots=True)
class ContextUsage:
    """一段对话的上下文占用快照（API 响应体）。"""

    #: 一次能带上的消息条数
    limit: int
    #: 对话里可进上下文的消息总数
    message_count: int
    #: 实际进入下一轮的条数（超过 limit 时等于 limit）
    used_messages: int
    #: 实际进入下一轮的字符数（执行指令本身；历史已压缩在指令内）
    used_chars: int
    #: 其中执行指令（约束 + 任务 + 对话轨迹 + 工作记忆 + 计划）的字符数
    instruction_chars: int
    #: 执行 Agent 一轮可投喂的字符预算
    budget_chars: int
    #: 原始占用比例（可能 > 1，客户端展示时再夹到 100%）
    ratio: float
    #: 展示用百分比（0~100）
    percent: int
    #: normal / warning / full
    level: str
    #: 每条消息的估算字符数（前端解释口径用）
    message_chars: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "message_count": self.message_count,
            "used_messages": self.used_messages,
            "used_chars": self.used_chars,
            "instruction_chars": self.instruction_chars,
            "budget_chars": self.budget_chars,
            "ratio": self.ratio,
            "percent": self.percent,
            "level": self.level,
            "message_chars": self.message_chars,
        }


def measure_context(
    messages: list[Any],
    *,
    message_count: int,
    limit: int | None = None,
    extra_chars: int = 0,
) -> ContextUsage:
    """按最近 `limit` 条消息算占用。`messages` 为按 seq 升序的尾部消息。

    `message_count` 是任务内可进上下文的消息总数（由仓储统计，不受本次取数
    limit 影响）；两者在条数维度上不混用——否则「只取了 30 条」会被误判成
    「整段对话只有 30 条」。

    `extra_chars` 是这一轮执行指令的真实字符数（约束 + 任务 + 压缩后的对话轨迹 +
    工作记忆 + 计划）。历史在指令里已经是压缩态，调用方不要再把原始消息传进来
    重复计一次；`messages` 只用于尚未装配指令的兼容场景。

    比例只由字符体积决定：条数不在比例与等级里，否则对话一旦达到条数上限就会
    永远显示 100%，而实际投喂的已经是压缩后的轨迹。
    """
    effective_limit = max(1, limit if limit is not None else context_message_limit())
    window = messages[-effective_limit:]
    instruction_chars = max(extra_chars, 0)
    used_chars = sum(len((message.content or "").strip()) for message in window) + instruction_chars
    budget_chars = max(1, context_budget_chars())

    used_messages = min(max(message_count, 0), effective_limit)
    raw_ratio = used_chars / budget_chars

    if raw_ratio >= 1.0:
        level = LEVEL_FULL
    elif raw_ratio >= CONTEXT_WARNING_RATIO:
        level = LEVEL_WARNING
    else:
        level = LEVEL_NORMAL

    return ContextUsage(
        limit=effective_limit,
        message_count=max(message_count, 0),
        used_messages=used_messages,
        used_chars=used_chars,
        instruction_chars=instruction_chars,
        budget_chars=budget_chars,
        ratio=round(raw_ratio, 3),
        percent=min(100, int(round(raw_ratio * 100))),
        level=level,
        message_chars=ESTIMATED_MESSAGE_CHARS,
    )


__all__ = [
    "CONTEXT_MESSAGE_KINDS",
    "CONTEXT_WARNING_RATIO",
    "LEVEL_FULL",
    "LEVEL_NORMAL",
    "LEVEL_WARNING",
    "ContextUsage",
    "context_budget_chars",
    "context_message_limit",
    "measure_context",
]
