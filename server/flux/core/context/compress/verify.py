"""S5 自校验（FLUX_CONTEXT_MECHANISM §5.3 S5 / §5.6）。

前四段流水线全是「选择 + 编码」（§5.1）：S1 换符号、S2 按形态抽取、S3 按相关性挑句、
S4 按预算装配。选择难免出错——符号表错位、抽取切到半行、筛选混进别处的行。S5 是最后
一道闸：**逐字回验**，对不上就整条作废。

§5.3 S5 给的判据是「sha256 / 行号 / 命令 / exit code / 用例名是否与原文一致」，本模块
把它落成两类检查：

1. **行级**：产物里每一行"声称是原文"的行，必须在原文里**逐字存在**；`[L{k}]` 行号
   标记指向的下一行必须等于原文第 k 行（行号不是装饰，是坐标）。
2. **令牌级**：sha256、用例名、exit code、命令行这类高价值常量，只要**出现在产物里**
   就必须能在原文里逐字找到——防的是编码过程"顺手改写"它们。

标记行（`…` / `[×` / `[L` / `# `）是 S2 合成的结构描述，本就不来自原文，一律跳过。

不变量：`verify(source, extract(source).render())` 恒通过——这正是 §5.6「重建一致率
恒 100%」在单段上的形式。不通过就是 bug，不允许"压对一半"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from flux.core.context.compress.extract import (
    _FOLD_PREFIX,
    _LOCATE_PREFIX,
    _REPEAT_PREFIX,
    _SYNTHETIC_PREFIX,
)

#: 标记行前缀（S2 合成的结构描述，不是原文）：逐字比对时整体跳过
_MARKER_PREFIXES = (_FOLD_PREFIX, _REPEAT_PREFIX, _LOCATE_PREFIX, _SYNTHETIC_PREFIX)
#: 行号标记：独占一行，形如 `[L12]`
_LOCATOR_RE = re.compile(r"^\[L(\d+)\]$")
#: 高价值常量：出现在产物里就必须能在原文找到（§5.3 S5 点名的五类）
_PROTECTED_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sha256", re.compile(r"\b[0-9a-fA-F]{32,64}\b")),
    ("用例名", re.compile(r"[\w./\\-]+\.py::[\w\[\]:.-]+")),
    ("exit code", re.compile(r"\bexit (?:code|status)\s*[:= ]\s*\d+", re.IGNORECASE)),
    ("命令", re.compile(r"^\s*[$>]\s+\S[^\n]*$", re.MULTILINE)),
)
#: 报错信息里贴多长的一行就够定位（太长的行不该把告警本身撑爆）
_CLIP = 60


@dataclass(frozen=True)
class Verification:
    """一次自校验的结论：`ok` 为假即产物作废，调用方必须整条降级为引用。"""

    ok: bool
    checked: int
    failures: tuple[str, ...]


def verify(source: str, rendered: str) -> Verification:
    """逐字回验 `rendered` 是否忠实于 `source`（§5.3 S5）。"""
    source_lines = source.splitlines()
    known = set(source_lines)
    lines = rendered.splitlines()
    checked = 0
    failures: list[str] = []

    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip():
            continue
        locator = _LOCATOR_RE.match(line)
        if locator is not None:
            checked += 1
            number = int(locator.group(1))
            expected = source_lines[number - 1] if 1 <= number <= len(source_lines) else None
            if expected is None or _next_content(lines, index) != expected:
                failures.append(f"行号 {number} 对不上原文")
            continue
        if line.startswith(_MARKER_PREFIXES):
            continue
        checked += 1
        if line not in known:
            failures.append(f"非逐字行：{_clip(line)}")

    for label, pattern in _PROTECTED_RES:
        for match in pattern.finditer(rendered):
            token = match.group(0)
            if token.strip() and token not in source:
                failures.append(f"{label} 与原文不一致：{_clip(token)}")

    return Verification(not failures, checked, tuple(failures))


def _next_content(lines: list[str], start: int) -> str | None:
    """`[L{k}]` 之后紧跟的那行原文（跳过空行）。"""
    for index in range(start, len(lines)):
        if lines[index].strip():
            return lines[index]
    return None


def _clip(text: str) -> str:
    return text if len(text) <= _CLIP else text[:_CLIP] + "…"
