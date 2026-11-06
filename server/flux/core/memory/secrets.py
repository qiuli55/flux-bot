"""记忆红线：密钥 / 令牌 / 凭证永不入库、不入上下文（批次② §4.2）。

纯函数、零依赖：所有记忆写入路径（User / Environment / Project）共用同一份判定，
避免"这个入口拦、那个入口漏"。判定取保守策略——明显像凭证的内容直接拒绝落库；
占位符（`<your-key>`、`${ENV}`、`YOUR_API_KEY` 等）不拦，避免把文档示例堵死。

命中原因只回标签、不回原文：错误信息本身也不得把疑似密钥带进调用方日志。
"""

from __future__ import annotations

import re

from flux.errors import ValidationError

#: 每种模式配一个可读的原因标签，错误信息里说清"像什么"。
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OpenAI / DeepSeek 风格的 sk- 密钥", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("GitHub 令牌", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("Flux 接入令牌", re.compile(r"\bfxt_[0-9a-fA-F]{32,}\b")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Slack 令牌", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "带赋值的口令 / 密钥字段",
        re.compile(
            r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)"
            r"\b\s*[:=]\s*[\"']?([A-Za-z0-9+/=_\-.]{8,})"
        ),
    ),
)

#: 占位符特征：文档 / 示例里写"这里填你的 key"不该被拦。
_PLACEHOLDER_MARKERS = (
    "<",
    ">",
    "${",
    "{{",
    "your",
    "example",
    "sample",
    "placeholder",
    "changeme",
    "xxxx",
    "****",
    "...",
)

_ASSIGNMENT_LABEL = "带赋值的口令 / 密钥字段"


def find_secret(text: str) -> str | None:
    """返回命中的敏感模式标签；没有命中返回 None。"""
    for label, pattern in _PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        if label == _ASSIGNMENT_LABEL and _is_placeholder(match.group(2)):
            continue
        return label
    return None


def ensure_no_secret(text: str, *, where: str) -> None:
    """写入前的硬闸门：疑似凭证一律拒绝（`flux.errors.ValidationError`）。"""
    kind = find_secret(text)
    if kind is not None:
        raise ValidationError(
            f"{where}内容疑似包含密钥/令牌/凭证（{kind}），按红线拒绝写入",
            details={"policy": "secrets are never stored and never enter context"},
        )


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)
