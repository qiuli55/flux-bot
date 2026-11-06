"""对话交接摘要：把旧对话压成一段「前情」，带进新对话的上下文。

只由平台模型通道（ModelRouter）真实生成；模型返回空内容时直接失败，
不拿占位文本冒充摘要——新对话的上下文可以短，但不能是编的。

摘要进新对话有两条路径，都从这里产出的同一段文本：
1. 落库为一条 `kind=summary` 的消息，进入助手历史；
2. 拼进 Agent 执行指令的「前情摘要」段（见 TaskRunService）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from flux.core.model_gateway.base import ChatMessage
from flux.core.model_gateway.router import ModelRouter
from flux.errors import ValidationError
from flux.logging import get_logger

logger = get_logger(__name__)

#: 摘要正文上限（字符）。模型偶尔会写成长文，超出的部分直接截断——
#: 摘要本身是为了省上下文，不能变成新的上下文负担。
MAX_SUMMARY_CHARS = 2_000

#: 送进摘要模型的原话上限（字符）。只取最近的对话，更早的丢弃并标注。
MAX_TRANSCRIPT_CHARS = 48_000

SUMMARY_SYSTEM_PROMPT = """你是 Flux 的对话交接助手。把下面这段开发对话压缩成一份前情摘要，\
供新的对话接着做。

要求：
- 用中文，不超过 500 字；
- 按四段输出，每段以「目标：」「已确认的结论：」「已完成的事：」「待办：」开头，各占一段；
- 只写对话里真实出现过的事实，不补全、不猜测；某一段没有内容就写「无」；
- 不要输出 JSON、Markdown 代码块或额外解释。"""


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    """一次摘要生成的结果（含真实模型调用元信息）。"""

    text: str
    provider: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    source_messages: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.text,
            "provider": self.provider,
            "model": self.model,
            "usage": self.usage,
            "latency_ms": self.latency_ms,
            "source_messages": self.source_messages,
            "truncated": self.truncated,
        }


class ConversationSummarizer:
    """把一段对话压成前情摘要。messages 为 (role, content) 的升序序列。"""

    def __init__(
        self,
        router: ModelRouter,
        *,
        max_transcript_chars: int = MAX_TRANSCRIPT_CHARS,
        max_summary_chars: int = MAX_SUMMARY_CHARS,
    ) -> None:
        self._router = router
        self._max_transcript_chars = max(1, max_transcript_chars)
        self._max_summary_chars = max(1, max_summary_chars)

    async def summarize(
        self,
        *,
        description: str,
        messages: list[tuple[str, str]],
    ) -> ConversationSummary:
        transcript, kept, truncated = self._transcript(description, messages)
        if kept == 0:
            raise ValidationError(
                "这段对话还没有可以摘要的内容",
                details={"messages": len(messages)},
            )

        started = time.perf_counter()
        result = await self._router.chat(
            [
                ChatMessage(role="system", content=SUMMARY_SYSTEM_PROMPT),
                ChatMessage(role="user", content=transcript),
            ]
        )
        text = _clean_summary(result.content, self._max_summary_chars)
        if not text:
            raise ValidationError("摘要模型没有返回内容，请稍后重试")

        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "对话摘要完成 provider=%s model=%s 来源消息=%d 截断=%s 字数=%d 耗时=%dms",
            result.provider,
            result.model,
            kept,
            truncated,
            len(text),
            latency_ms,
        )
        return ConversationSummary(
            text=text,
            provider=str(result.provider),
            model=result.model,
            usage={
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "total_tokens": result.usage.total_tokens,
            },
            latency_ms=latency_ms,
            source_messages=kept,
            truncated=truncated,
        )

    def _transcript(
        self, description: str, messages: list[tuple[str, str]]
    ) -> tuple[str, int, bool]:
        """按字符预算从新到旧收消息；更早的丢弃并明确标注，不静默截断。"""
        lines: list[str] = []
        if description.strip():
            lines.append(f"任务描述：{description.strip()}")

        kept: list[str] = []
        used = 0
        truncated = False
        for role, content in reversed(messages):
            text = (content or "").strip()
            if not text:
                continue
            line = f"{'用户' if role == 'user' else '助手'}：{text}"
            if used + len(line) > self._max_transcript_chars:
                truncated = True
                break
            kept.append(line)
            used += len(line)
        kept.reverse()
        if truncated:
            lines.append("（更早的消息因长度限制已省略）")
        lines.extend(kept)
        return "\n".join(lines), len(kept), truncated


def _clean_summary(raw: str, limit: int) -> str:
    """去掉模型可能的 ``` 包裹与首尾空白，并按上限截断。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            text = "\n".join(lines[1:-1]).strip()
    return text[:limit].strip()


__all__ = [
    "MAX_SUMMARY_CHARS",
    "MAX_TRANSCRIPT_CHARS",
    "ConversationSummarizer",
    "ConversationSummary",
]
