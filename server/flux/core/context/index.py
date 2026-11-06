"""反向索引的词条抽取（FLUX_CONTEXT_MECHANISM §7.2）。

纯函数，不碰数据库——这样"抽得对不对"可以单独测，也方便日后换实现（如从 facts 里抽更多线索）。

规范里的五类查询，这里负责四类的词条：

| 类别 | 线索从哪来 | 归一化 |
| --- | --- | --- |
| 文件 | `target_ref` 里像路径的词 | 去 `./` 前缀、**同时收全路径与 basename** |
| 命令 | `kind=command` 时 `target_ref` 的首词；`kind=tool_call` 时 `tool` | 去路径留程序名，小写 |
| URL | `target_ref` 里带 `://` 的词 | 只留域名（host），小写 |
| 错误关键字 | `result_head` 的 `*Error` / `FAILED` 等 | 统一小写 |

写入与查询共用 `normalize_token`，两边规则不会漂移。第五类「按时间窗」不需要词条——
直接查 `context_steps.created_at`。
"""

from __future__ import annotations

import re

from flux.enums import ContextTokenKind, StepKind
from flux.models.context import ContextStep

#: 像文件路径：含目录分隔符，或带一个不太短的扩展名（`a.py` / `x.ts` / `f.tar.gz`）
_PATH_RE = re.compile(r"[./\\]")
_EXT_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,9}$")
#: 像 URL：`scheme://host/...`
_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://(?P<host>[^/?#]+)")
#: 错误类名：`AssertionError` / `ValueError` / `TimeoutException` …（保留原始大小写）
_ERROR_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Failure))\b")
#: pytest / 构建工具的错误态标记
_ERROR_MARKERS = ("FAILED", "Traceback", "panic:", "FATAL")

#: 目录分隔符：Windows 的反斜杠也当分隔符，取 basename 时两种都认
_SEPARATORS = "\\/"


def extract_tokens(step: ContextStep) -> list[tuple[ContextTokenKind, str]]:
    """把一条 step 的线索抽成反向索引词条；按 (类别, 词条) 去重，保持出现顺序。"""
    found: list[tuple[ContextTokenKind, str]] = []
    seen: set[tuple[str, str]] = set()

    def emit(kind: ContextTokenKind, token: str) -> None:
        normalized = normalize_token(kind, token)
        if not normalized:
            return
        key = (kind.value, normalized)
        if key in seen:
            return
        seen.add(key)
        found.append((kind, normalized))

    for word in _words(step.target_ref):
        host = _url_host(word)
        if host is not None:
            emit(ContextTokenKind.URL, host)
            continue
        if _looks_like_path(word):
            emit(ContextTokenKind.FILE, word)
            emit(ContextTokenKind.FILE, _basename(word))

    for token in _commands_of(step):
        emit(ContextTokenKind.COMMAND, token)

    for token in _errors_of(step.result_head):
        emit(ContextTokenKind.ERROR, token)

    return found


def normalize_token(kind: ContextTokenKind, value: str) -> str:
    """查询与写入共用同一套归一化——否则两边规则一漂移，索引就静默查不到。"""
    value = value.strip()
    if not value:
        return ""
    if kind is ContextTokenKind.URL:
        # 查询可能给完整 URL，也可能只给域名；给完整 URL 时同样收敛到 host
        return (_url_host(value) or value).lower()
    if kind is ContextTokenKind.FILE:
        return _normalize_path(value).lower()
    return value.lower()


def _words(value: str | None) -> list[str]:
    return value.split() if value else []


def _url_host(word: str) -> str | None:
    """`https://docs.example.com/a/b?x=1` → `docs.example.com`。"""
    match = _URL_RE.match(word)
    if match is None:
        return None
    host = match.group("host").lower()
    return host.rsplit("@", 1)[-1].split(":", 1)[0] or None


def _looks_like_path(word: str) -> bool:
    if word.startswith("-"):  # 命令行开关，不是路径
        return False
    return bool(_PATH_RE.search(word) or _EXT_RE.search(word))


def _normalize_path(word: str) -> str:
    return word[2:] if word.startswith("./") else word


def _basename(word: str) -> str:
    for separator in _SEPARATORS:
        word = word.rsplit(separator, 1)[-1]
    return word


def _commands_of(step: ContextStep) -> list[str]:
    """命令 / 程序名：`pytest tests/test_auth.py` → `pytest`；tool_call 则用工具名。"""
    if step.kind == StepKind.TOOL_CALL.value:
        return [step.tool.lower()] if step.tool else []
    if step.kind != StepKind.COMMAND.value:
        return []
    words = _words(step.target_ref)
    if not words:
        return []
    program = _basename(words[0]).lower()
    return [program] if program else []


def _errors_of(result_head: str | None) -> list[str]:
    if not result_head:
        return []
    found = [match.group(1) for match in _ERROR_RE.finditer(result_head)]
    found.extend(marker for marker in _ERROR_MARKERS if marker in result_head)
    return found
