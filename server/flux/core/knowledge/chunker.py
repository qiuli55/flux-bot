"""知识库切块：文档按标题层级、对话按「会话 × 时间窗」。

两类来源的语义不同，切法也不同：

- 文档：先按 Markdown 标题切 section，section 内按空行分段，再按 token 预算打包；
  超长段落按句末标点切分，并保留少量重叠，避免答案正好落在切口。
- 对话：按消息顺序装满一个 token 窗口；窗口之间重叠 1 条消息，保留问答衔接。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from flux.core.knowledge.text import estimate_tokens

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_SENTENCE_RE = re.compile(r"[^。！？!?；;.!?\n]+[。！？!?；;.!?\n]*")


@dataclass(frozen=True)
class TextChunk:
    """切块结果：序号由调用方按顺序重排前的原始序号。"""

    seq: int
    title: str | None
    content: str
    token_count: int
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConversationMessage:
    """参与切块的一条对话消息（已脱敏）。"""

    seq: int
    role: str
    content: str
    occurred_at: datetime | None = None


# --- 文档 ---


def chunk_markdown(
    text: str,
    *,
    max_tokens: int,
    overlap_tokens: int,
) -> list[TextChunk]:
    """把 Markdown 文本切成带标题路径的块。"""
    if not text.strip():
        return []
    sections = _markdown_sections(text)
    chunks: list[TextChunk] = []
    seq = 0
    for title, body in sections:
        for content in _pack(_split_paragraphs(body), max_tokens, overlap_tokens):
            stripped = content.strip()
            if not stripped:
                continue
            seq += 1
            heading_path = title.split(" / ") if title else []
            chunks.append(
                TextChunk(
                    seq=seq,
                    title=title,
                    content=stripped,
                    token_count=estimate_tokens(stripped),
                    meta={"heading_path": heading_path},
                )
            )
    return chunks


def _markdown_sections(text: str) -> list[tuple[str | None, str]]:
    """按标题栈把文本拆成 (标题路径, 正文) 列表。"""
    sections: list[tuple[str | None, str]] = []
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []
    current_title: str | None = None

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            sections.append((current_title, body))
        buffer.clear()

    for line in text.splitlines():
        match = _HEADING_RE.match(line.strip())
        if match is None:
            buffer.append(line)
            continue
        flush()
        level = len(match.group(1))
        title = match.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        current_title = " / ".join(item for _, item in stack)
    flush()
    return sections


def _split_paragraphs(text: str) -> list[str]:
    """按空行分段；代码块内部的空行不算分段。"""
    paragraphs: list[str] = []
    buffer: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and not line.strip():
            if buffer:
                paragraphs.append("\n".join(buffer).strip())
                buffer.clear()
            continue
        buffer.append(line)
    if buffer:
        paragraphs.append("\n".join(buffer).strip())
    return [paragraph for paragraph in paragraphs if paragraph]


def _pack(paragraphs: list[str], max_tokens: int, overlap_tokens: int) -> list[str]:
    """把段落装进 token 预算；切口处保留 overlap_tokens 的尾部上下文。"""
    pieces: list[str] = []
    for paragraph in paragraphs:
        pieces.extend(_split_long(paragraph, max_tokens))

    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for piece in pieces:
        tokens = estimate_tokens(piece)
        if current and current_tokens + tokens > max_tokens:
            chunks.append("\n\n".join(current))
            tail: list[str] = []
            tail_tokens = 0
            for previous in reversed(current):
                previous_tokens = estimate_tokens(previous)
                if tail_tokens + previous_tokens > overlap_tokens:
                    break
                tail.insert(0, previous)
                tail_tokens += previous_tokens
            current = tail
            current_tokens = tail_tokens
        current.append(piece)
        current_tokens += tokens
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _split_long(text: str, max_tokens: int) -> list[str]:
    """超过预算的段落按句子切分；单句仍超长时按字符硬切。"""
    if max_tokens <= 0 or estimate_tokens(text) <= max_tokens:
        return [text]
    pieces: list[str] = []
    buffer = ""
    for sentence in _SENTENCE_RE.findall(text):
        if not buffer:
            buffer = sentence
        elif estimate_tokens(buffer + sentence) <= max_tokens:
            buffer += sentence
        else:
            pieces.append(buffer)
            buffer = sentence
        while estimate_tokens(buffer) > max_tokens:
            cut = _cut_point(buffer, max_tokens)
            pieces.append(buffer[:cut])
            buffer = buffer[cut:]
    if buffer:
        pieces.append(buffer)
    return [piece for piece in pieces if piece.strip()]


def _cut_point(text: str, max_tokens: int) -> int:
    """二分出「估算 token 不超过预算」的最长前缀长度（至少 1）。"""
    low, high = 1, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle]) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return max(1, low)


# --- 对话 ---


def chunk_conversation(
    messages: Sequence[ConversationMessage],
    *,
    max_tokens: int,
    overlap_messages: int = 1,
    title: str | None = None,
) -> list[TextChunk]:
    """把一段会话消息切成时间窗块。"""
    if not messages:
        return []
    chunks: list[TextChunk] = []
    window: list[ConversationMessage] = []
    window_tokens = 0
    seq = 0

    def append(content: str, first_seq: int, last_seq: int) -> None:
        nonlocal seq
        body = content.strip()
        if not body:
            return
        seq += 1
        chunks.append(
            TextChunk(
                seq=seq,
                title=title,
                content=body,
                token_count=estimate_tokens(body),
                meta={"first_seq": first_seq, "last_seq": last_seq},
            )
        )

    def flush() -> None:
        nonlocal window, window_tokens
        if window:
            append(
                "\n\n".join(_render(message) for message in window),
                window[0].seq,
                window[-1].seq,
            )
        window = window[-overlap_messages:] if overlap_messages > 0 else []
        window_tokens = sum(_message_tokens(message) for message in window)
        if window_tokens >= max_tokens:
            window = []
            window_tokens = 0

    for message in messages:
        rendered = _render(message)
        tokens = estimate_tokens(rendered)
        if tokens > max_tokens:
            # 单条消息就超预算：不参与窗口，按句子独立切块。
            flush()
            window = []
            window_tokens = 0
            for piece in _split_long(rendered, max_tokens):
                append(piece, message.seq, message.seq)
            continue
        if window and window_tokens + tokens > max_tokens:
            flush()
        window.append(message)
        window_tokens += tokens
    flush()
    return chunks


def _render(message: ConversationMessage) -> str:
    return f"{message.role}: {message.content}"


def _message_tokens(message: ConversationMessage) -> int:
    return estimate_tokens(_render(message))
