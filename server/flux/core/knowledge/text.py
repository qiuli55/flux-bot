"""知识库文本基元：token 估算、分词、片段截取。

不追求与真实 tokenizer 对齐：只用于切块预算和 BM25 打分，口径稳定比精确更重要。
CJK 按「一字一 token」，其余按 4 字符一 token 折算。
"""

from __future__ import annotations

import re

_ASCII_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


def estimate_tokens(text: str) -> int:
    """估算文本 token 数（CJK 逐字，其余 4 字符折 1）。"""
    if not text:
        return 0
    cjk = sum(len(run) for run in _CJK_RUN_RE.findall(text))
    other = max(0, len(text) - cjk)
    return cjk + (other + 3) // 4


def tokenize(text: str) -> list[str]:
    """检索用分词：ASCII 词 + CJK 单字 + 相邻 CJK bigram。

    中文没有空格，只切单字会把「部署」和「部署失败」当成弱相关；补 bigram
    能把短语召回拉回来，同时保留单字兜底。
    """
    if not text:
        return []
    lowered = text.lower()
    tokens = _ASCII_TOKEN_RE.findall(lowered)
    for run in _CJK_RUN_RE.findall(lowered):
        tokens.extend(run)
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def snippet(text: str, terms: list[str], *, max_chars: int = 280) -> str:
    """围绕首个命中词截一段展示文本；无命中时取开头。"""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    lowered = text.lower()
    position = -1
    for term in terms:
        key = term.strip().lower()
        if not key:
            continue
        found = lowered.find(key)
        if found >= 0 and (position < 0 or found < position):
            position = found
    if position < 0:
        return text[:max_chars].rstrip() + "…"
    start = max(0, position - max_chars // 3)
    end = min(len(text), start + max_chars)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end].strip() + suffix
