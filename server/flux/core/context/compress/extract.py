"""S2 结构抽取（FLUX_CONTEXT_MECHANISM §5.3 S2 / §5.2）。

§5.2 说冗余有四个来源，S1 只处理「重复」；剩下的「冗长」「过期」「无关」里，
**冗长**（刷屏的中间输出、日志里同模板的成百上千行、diff 里的上下文行、整份文件正文）
由 S2 处理：按产出文本的**形态**分流，每种形态各有自己的抽取方式：

| 形态 | 抽取方式 |
| --- | --- |
| `TERMINAL` 终端 / 命令输出 | 留命令、错误行、exit code、结果汇总行、首尾；中间刷屏折叠 |
| `LOG` 日志 | 模板挖掘（Drain 变体）：变量位掩码成 `<N>` / `<TS>` …，同模板归并只留首次 + `[×N]` |
| `DIFF` 差异 | 只留 hunk 头与变更行，上下文行折叠 |
| `SOURCE` 文件内容 | 生成大纲：符号定位标记 `[L{k}]` + 该行逐字；正文按需 `context.fetch` |
| `DATA` 结构化数据 | schema + 统计摘要（合成行）+ 少量可逐字的样本行 |
| `PLAIN` 其它 | 首尾保留，中段折叠 |

三条铁律，都是为了 S5（自校验）真的做得起来：

1. **抽取是选择，不是生成**（§5.1）。除了显式以 `# ` 开头的**合成行**，产物里每一行
   都必须是原文某一行的**逐字复制**——`Extraction.kept` 记的正是这批行，S5 拿它逐字
   比对原文即可。合成行只承载"结构"（计数、schema、统计），不带任何原文里没有的事实。
2. **标记行独占一行**，只以四种前缀开头，一眼可辨：`…`（折叠了多少行）、
   `[×N]`（同模板重复次数）、`[L{k}]`（该行在原文的行号）、`# `（本模块合成的摘要行）。
   其余行都是逐字原文行。
3. **确定性**：同一段文本永远得到同一份产物（I7 幂等 / S5 可复现），全程无随机、无模型。

不变量：`len(kept) + dropped_lines == source_lines`——凡被丢掉的行都记在 `dropped_lines`
里，压缩**没有静默损失**（§5.6「重建一致率恒 100%」的前提）。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass

# 错误行判据与反向索引共用一套，避免"什么算错误"在两处漂移（§7.2）。
from flux.core.context.index import _ERROR_MARKERS, _ERROR_RE
from flux.enums import StrEnum

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 终端输出首尾各保留多少行：首行常是命令，尾行常是结论
_HEAD_LINES = 3
_TAIL_LINES = 3
#: 兜底形态没有结构可依赖，只能靠更宽的首尾窗口
_PLAIN_HEAD_LINES = 5
_PLAIN_TAIL_LINES = 5
#: 结构化数据的样本窗口
_DATA_HEAD_LINES = 3
_DATA_TAIL_LINES = 1
#: 单行超过此长度就不值得逐字保留（逐字留一行 200KB 的压缩 JSON 等于没压）
_MAX_VERBATIM_LINE = 200
#: 判定形态只看前多少行——分流不需要读完整段
_CLASSIFY_SAMPLE = 200
#: 认表格至少要几行（两行文本里凑巧的分隔符不算表格）
_TABLE_MIN_LINES = 3
#: schema 最多列多少个字段
_SCHEMA_FIELDS_MAX = 20

#: 折叠标记：省略了多少行原文
_FOLD_PREFIX = "…"
#: 重复标记：同一模板一共出现了多少次
_REPEAT_PREFIX = "[×"
#: 行号标记：下一行逐字原文在原文里的行号（1 起）
_LOCATE_PREFIX = "[L"
#: 合成行前缀：本模块生成的摘要行，原文里没有对应行
_SYNTHETIC_PREFIX = "# "


class ContentForm(StrEnum):
    """产出文本的形态（§5.3 S2 的分流依据）。"""

    TERMINAL = "terminal"
    LOG = "log"
    DIFF = "diff"
    SOURCE = "source"
    DATA = "data"
    PLAIN = "plain"


@dataclass(frozen=True)
class Extraction:
    """一份抽取产物。

    `lines` 是投喂用的渲染结果（标记行 + 逐字原文行），`kept` 是其中逐字来自原文的
    那批行——S5 只认 `kept`，逐字比对得上才算抽取没压错。
    """

    form: ContentForm
    lines: tuple[str, ...]
    kept: tuple[str, ...]
    dropped_lines: int
    source_lines: int

    def render(self) -> str:
        return "\n".join(self.lines)


def extract(text: str, form: ContentForm | None = None) -> Extraction:
    """把一段产出原文抽成投喂产物；`form=None` 时先按 `classify` 判形态。"""
    lines = text.splitlines()
    if not lines:
        return Extraction(form or ContentForm.PLAIN, (), (), 0, 0)
    chosen = form if form is not None else classify(text)
    if chosen is ContentForm.TERMINAL:
        return _extract_terminal(lines)
    if chosen is ContentForm.LOG:
        return _extract_log(lines)
    if chosen is ContentForm.DIFF:
        return _extract_diff(lines)
    if chosen is ContentForm.SOURCE:
        return _extract_source(lines)
    if chosen is ContentForm.DATA:
        return _extract_data(lines)
    return _extract_plain(lines, chosen)


def classify(text: str) -> ContentForm:
    """按产出文本的形态分流（§5.3 S2）。

    顺序即优先级：diff 与结构化数据的信号最强、最不易误判，先判；日志与终端的
    信号次之；代码只在"没有更像的东西"时才认——否则 pytest 失败输出里夹的几行
    源码片段会把终端输出误判成源文件。
    """
    lines = text.splitlines()
    if not lines:
        return ContentForm.PLAIN
    sample = lines[:_CLASSIFY_SAMPLE]
    count = len(sample)

    if (
        any(line.startswith("@@ ") for line in sample)
        or sum(1 for line in sample if _DIFF_HEAD_RE.match(line)) >= 2
    ):
        return ContentForm.DIFF
    if _try_json(text) is not _NOT_JSON:
        return ContentForm.DATA
    if count >= _TABLE_MIN_LINES and _table_columns(sample) is not None:
        return ContentForm.DATA
    log_hits = sum(1 for line in sample if _looks_like_log_line(line))
    if log_hits >= 3 and log_hits * 2 >= count:
        return ContentForm.LOG
    if any(_is_error_line(line) or _is_exit_line(line) or _is_result_line(line) for line in sample):
        return ContentForm.TERMINAL
    if any(_PROMPT_RE.match(line) for line in sample):
        return ContentForm.TERMINAL
    code_hits = sum(1 for line in sample if _CODE_RE.search(line))
    if code_hits >= 3 and code_hits * 3 >= count:
        return ContentForm.SOURCE
    return ContentForm.PLAIN


# ---------------------------------------------------------------------------
# 各形态的抽取
# ---------------------------------------------------------------------------


def _extract_terminal(lines: list[str]) -> Extraction:
    """终端 / 命令输出：留命令、错误行、exit code、结果汇总行、首尾，中间折叠。"""
    keep = _window_flags(len(lines), _HEAD_LINES, _TAIL_LINES)
    for index, line in enumerate(lines):
        if _is_error_line(line) or _is_exit_line(line) or _is_result_line(line):
            keep[index] = True
            # 错误行后面常跟一行定位（`File "x.py", line 12`），一起留下
            if index + 1 < len(lines) and _is_continuation(lines[index + 1]):
                keep[index + 1] = True
    return _build(ContentForm.TERMINAL, lines, keep, f"{_FOLD_PREFIX} 省略 {{n}} 行输出")


def _extract_log(lines: list[str]) -> Extraction:
    """日志：模板挖掘，同模板归并——只留首次出现（逐字）+ 一个重复计数标记。"""
    templates = [_template(line) for line in lines]
    counts = Counter(templates)
    seen: set[str] = set()
    rendered: list[str] = []
    kept: list[str] = []
    for line, template in zip(lines, templates, strict=True):
        if template in seen:
            continue
        seen.add(template)
        rendered.append(line)
        kept.append(line)
        if counts[template] > 1:
            rendered.append(f"{_REPEAT_PREFIX}{counts[template]}] 相同模板")
    return Extraction(
        ContentForm.LOG,
        tuple(rendered),
        tuple(kept),
        len(lines) - len(kept),
        len(lines),
    )


def _extract_diff(lines: list[str]) -> Extraction:
    """差异：只留 hunk 头与变更行，上下文行折叠（§5.3 S2）。"""
    keep = [_is_diff_line(line) for line in lines]
    return _build(ContentForm.DIFF, lines, keep, f"{_FOLD_PREFIX} 省略 {{n}} 行上下文")


def _extract_source(lines: list[str]) -> Extraction:
    """文件内容：只生成大纲（符号 + 行号），正文交给 `context.fetch`（L3 仅 ref）。"""
    symbols = [
        index for index, line in enumerate(lines) if _is_symbol_line(line) and _keepable_line(line)
    ]
    if not symbols:
        # 认不出符号（配置 / 纯文本文件）就别硬套大纲，退回首尾窗口更好用
        return _extract_plain(lines, ContentForm.SOURCE)
    rendered = [f"{_SYNTHETIC_PREFIX}文件大纲：共 {len(lines)} 行，符号 {len(symbols)} 个"]
    kept: list[str] = []
    for index in symbols:
        rendered.append(f"{_LOCATE_PREFIX}{index + 1}]")
        rendered.append(lines[index])
        kept.append(lines[index])
    return Extraction(
        ContentForm.SOURCE,
        tuple(rendered),
        tuple(kept),
        len(lines) - len(kept),
        len(lines),
    )


def _extract_data(lines: list[str]) -> Extraction:
    """结构化数据：schema + 统计摘要（合成行）+ 少量逐字样本行。"""
    payload = _try_json("\n".join(lines))
    if payload is not _NOT_JSON:
        summary = _json_summary(payload)
        return _with_summary(lines, summary, ContentForm.DATA)

    columns = _table_columns(lines)
    if columns is not None:
        delimiter, width = columns
        rows = [line for line in lines if line.strip()]
        names = [cell.strip() for cell in rows[0].split(delimiter)][:width]
        summary = _table_summary(rows, delimiter, width, names)
        return _with_summary(lines, summary, ContentForm.DATA)

    return _extract_plain(lines, ContentForm.DATA)


def _extract_plain(lines: list[str], form: ContentForm) -> Extraction:
    """兜底：没有结构可依赖，只保留首尾窗口。"""
    keep = _window_flags(len(lines), _PLAIN_HEAD_LINES, _PLAIN_TAIL_LINES)
    return _build(form, lines, keep, f"{_FOLD_PREFIX} 省略 {{n}} 行")


# ---------------------------------------------------------------------------
# 装配（标记行 + 逐字原文行）
# ---------------------------------------------------------------------------


def _build(form: ContentForm, lines: list[str], flags: list[bool], marker: str) -> Extraction:
    rendered, kept, dropped = _assemble(lines, flags, marker)
    return Extraction(form, tuple(rendered), tuple(kept), dropped, len(lines))


def _with_summary(lines: list[str], summary: list[str], form: ContentForm) -> Extraction:
    """把合成摘要行放在产物最前面，后面接常规的窗口抽取。"""
    keep = _window_flags(len(lines), _DATA_HEAD_LINES, _DATA_TAIL_LINES)
    rendered, kept, dropped = _assemble(lines, keep, f"{_FOLD_PREFIX} 省略 {{n}} 行")
    return Extraction(form, tuple([*summary, *rendered]), tuple(kept), dropped, len(lines))


def _assemble(lines: list[str], flags: list[bool], marker: str) -> tuple[list[str], list[str], int]:
    """把"留 / 丢"标记走一遍，连续丢弃段折成一行标记行（`{n}` 换成段长）。

    空行与超长单行一律不留（`_keepable_line`）——留一行 200KB 的 JSON 不叫抽取，
    那种内容该走 L3 只留 ref、按需 fetch。
    """
    keepable = [flag and _keepable_line(line) for line, flag in zip(lines, flags, strict=True)]
    keepable = _absorb_singletons(keepable, lines)
    rendered: list[str] = []
    kept: list[str] = []
    run = 0
    for line, keep in zip(lines, keepable, strict=True):
        if keep:
            if run:
                rendered.append(marker.format(n=run))
                run = 0
            rendered.append(line)
            kept.append(line)
        else:
            run += 1
    if run:
        rendered.append(marker.format(n=run))
    return rendered, kept, len(lines) - len(kept)


def _absorb_singletons(flags: list[bool], lines: list[str]) -> list[bool]:
    """长度为 1 的丢弃段折成一行标记反而更长，直接留下那一行（若它本身可留）。"""
    result = list(flags)
    index = 0
    while index < len(result):
        if result[index]:
            index += 1
            continue
        end = index
        while end < len(result) and not result[end]:
            end += 1
        if end - index == 1 and _keepable_line(lines[index]):
            result[index] = True
        index = end
    return result


def _window_flags(count: int, head: int, tail: int) -> list[bool]:
    """首 `head` 行与尾 `tail` 行保留，中间留待判定。"""
    return [index < head or index >= count - tail for index in range(count)]


def _keepable_line(line: str) -> bool:
    """空行与超长单行不值得逐字保留。"""
    return bool(line.strip()) and len(line) <= _MAX_VERBATIM_LINE


# ---------------------------------------------------------------------------
# 判据（形态分类 + 什么行该留）
# ---------------------------------------------------------------------------

#: diff 的结构行：文件头、索引行、hunk 头
_DIFF_HEAD_RE = re.compile(r"^(?:diff --git |index |@@ )")
#: diff 的元信息行（不带 `+` / `-` 前缀但仍承载结构）
_DIFF_META_PREFIXES = (
    "new file mode ",
    "deleted file mode ",
    "old mode ",
    "new mode ",
    "similarity index ",
    "dissimilarity index ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "Binary files ",
    "\\ No newline",
)
#: exit code / 退出状态的常见写法
_EXIT_RE = re.compile(
    r"\bexit (?:code|status)\b|\bExited with\b|\bexited with code\b|\breturned non-zero\b",
    re.IGNORECASE,
)
#: 结果汇总行的计数部分（`12 passed` / `3 failed` / `2 files changed`）
_RESULT_COUNT_RE = re.compile(
    r"\b\d+\s+(?:passed|failed|errors?|skipped|warnings?|tests?)\b"
    r"|\b\d+\s+(?:files?|items?)\s+(?:changed|updated|matched)\b",
    re.IGNORECASE,
)
#: 结果汇总行的固定词（大小写敏感：小写的 `ok` 出现在正文里太常见）
_RESULT_TOKEN_RE = re.compile(
    r"\b(?:PASSED|FAILED|OK|SUCCESS|BUILD SUCCESS|BUILD FAILED|All tests passed)\b"
)
#: 日志行：时间戳开头，或日志级别开头
_LOG_HEAD_RE = re.compile(
    r"^\s*\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"
    r"|^\s*\[?\d{2}:\d{2}:\d{2}[.,]\d+\]?"
    r"|^\s*\[?(?:DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL|FATAL|TRACE)\]?\s"
)
#: shell 提示符：命令行回放的第一行
_PROMPT_RE = re.compile(r"^\s*(?:\$|>|PS )")
#: 像代码的行（只在没有更强信号时才据此判 `SOURCE`）
_CODE_RE = re.compile(
    r"^\s*(?:#include\b|import\b|from\b|use\b|package\b|public\b|private\b|protected\b|static\b"
    r"|def\b|class\b|async\s+def\b|function\b|const\b|let\b|var\b|interface\b|type\b|enum\b"
    r"|fn\b|func\b|struct\b|impl\b|trait\b|export\b)"
    r"|=>"
    r"|;\s*$"
    r"|^\s*[}\])]\s*$"
)
#: 符号定义行（文件大纲只收这些）
_SYMBOL_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*(?:async\s+)?def\s+\w+"),
    re.compile(r"^\s*class\s+\w+"),
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+\w+"),
    re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+\w+\s*=\s*"
        r"(?:async\s+)?(?:function\b|\(|[A-Za-z_$][\w$]*\s*=>)"
    ),
    re.compile(r"^\s*(?:export\s+)?(?:interface|type|enum)\s+\w+"),
    re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?\w+"),
    re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+\w+"),
    re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|impl)\b"),
)
#: 日志模板挖掘的变量位掩码，顺序即优先级（先认出更长的结构化变量，再退到数字）
_TEMPLATE_MASKS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "<UUID>",
    ),
    (
        re.compile(
            r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
        ),
        "<TS>",
    ),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<TS>"),
    (re.compile(r"\b(?:0x)?[0-9a-fA-F]{16,}\b"), "<HEX>"),
    (re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\""), "<STR>"),
    (re.compile(r"(?:[A-Za-z]:)?(?:[/\\][\w.-]+)+"), "<PATH>"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<N>"),
)
#: 表格候选分隔符
_TABLE_DELIMITERS = (",", "\t", "|", ";")

#: `_try_json` 的"不是 JSON"哨兵（`None` 是合法 JSON 值，不能拿它当哨兵）
_NOT_JSON = object()


def _is_diff_line(line: str) -> bool:
    """diff 里值得留的行：结构行、元信息行、以及 `+` / `-` 变更行。"""
    return (
        line.startswith(("+", "-", "\\ "))
        or line.startswith(_DIFF_META_PREFIXES)
        or _DIFF_HEAD_RE.match(line) is not None
    )


def _is_error_line(line: str) -> bool:
    return _ERROR_RE.search(line) is not None or any(m in line for m in _ERROR_MARKERS)


def _is_exit_line(line: str) -> bool:
    return _EXIT_RE.search(line) is not None


def _is_result_line(line: str) -> bool:
    return _RESULT_COUNT_RE.search(line) is not None or _RESULT_TOKEN_RE.search(line) is not None


def _is_continuation(line: str) -> bool:
    """缩进而非空——错误行后面那种定位行（`File "x.py", line 12`）。"""
    return line[:1] in (" ", "\t") and bool(line.strip())


def _looks_like_log_line(line: str) -> bool:
    return _LOG_HEAD_RE.match(line) is not None


def _is_symbol_line(line: str) -> bool:
    return any(pattern.match(line) for pattern in _SYMBOL_RES)


def _template(line: str) -> str:
    """把日志行的变量位掩码，得到"模板"（Drain 变体的确定性简化版）。"""
    for pattern, replacement in _TEMPLATE_MASKS:
        line = pattern.sub(replacement, line)
    return line


# ---------------------------------------------------------------------------
# 结构化数据
# ---------------------------------------------------------------------------


def _try_json(text: str) -> object:
    """能解析成 JSON 就返回对象；`_NOT_JSON` 表示不是。

    只认 `{` / `[` 开头的文本：`123` / `true` 这类裸标量解析成功也没意义，
    反而会把普通文本误判成结构化数据。
    """
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return _NOT_JSON
    try:
        return json.loads(stripped)
    except ValueError:
        return _NOT_JSON


def _json_summary(payload: object) -> list[str]:
    """JSON 的 schema 摘要：顶层规模 + 字段类型。"""
    if isinstance(payload, dict):
        summary = [f"{_SYNTHETIC_PREFIX}JSON 对象：{len(payload)} 个键"]
        for key, value in list(payload.items())[:_SCHEMA_FIELDS_MAX]:
            summary.append(f"{_SYNTHETIC_PREFIX}  {key}: {_type_name(value)}")
        if len(payload) > _SCHEMA_FIELDS_MAX:
            rest = len(payload) - _SCHEMA_FIELDS_MAX
            summary.append(f"{_SYNTHETIC_PREFIX}  … 其余 {rest} 个键省略")
        return summary
    if isinstance(payload, list):
        summary = [f"{_SYNTHETIC_PREFIX}JSON 数组：{len(payload)} 项"]
        first = next((item for item in payload if isinstance(item, dict)), None)
        if first is not None:
            keys = ", ".join(str(key) for key in list(first)[:_SCHEMA_FIELDS_MAX])
            summary.append(f"{_SYNTHETIC_PREFIX}  元素字段: {keys}")
        return summary
    return [f"{_SYNTHETIC_PREFIX}JSON 标量: {_type_name(payload)}"]


def _type_name(value: object) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, dict):
        return f"object({len(value)} 键)"
    if isinstance(value, list):
        return f"array({len(value)} 项)"
    if value is None:
        return "null"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return type(value).__name__  # pragma: no cover - json.loads 不会产出别的类型


def _table_columns(lines: list[str]) -> tuple[str, int] | None:
    """认一个表格：同一分隔符在多数行里切出同样多的列（且至少两列）。"""
    best: tuple[str, int] | None = None
    for delimiter in _TABLE_DELIMITERS:
        counts = [line.count(delimiter) for line in lines if line.strip()]
        if not counts:
            continue
        width, hits = Counter(counts).most_common(1)[0]
        if (
            width >= 1
            and hits >= max(2, len(counts) // 2)
            and (best is None or width + 1 > best[1])
        ):
            best = (delimiter, width + 1)
    return best


def _table_summary(rows: list[str], delimiter: str, width: int, names: list[str]) -> list[str]:
    """表格的 schema 与统计摘要（只算整列都能解析成数字的列）。"""
    summary = [f"{_SYNTHETIC_PREFIX}表格：{len(rows)} 行 × {width} 列（分隔符 {delimiter!r}）"]
    if any(names):
        label = " | ".join(name or f"col{index}" for index, name in enumerate(names))
        summary.append(f"{_SYNTHETIC_PREFIX}  列: {label}")
    summary.extend(_numeric_column_stats(rows, delimiter, width, names))
    return summary


def _numeric_column_stats(
    rows: list[str], delimiter: str, width: int, names: list[str]
) -> list[str]:
    stats: list[str] = []
    for index in range(width):
        values: list[float] = []
        for row in rows[1:]:
            cells = row.split(delimiter)
            if index >= len(cells):
                values = []
                break
            try:
                values.append(float(cells[index].strip()))
            except ValueError:
                values = []
                break
        if not values:
            continue
        label = names[index] if index < len(names) and names[index] else f"col{index}"
        stats.append(
            f"{_SYNTHETIC_PREFIX}  数值列 {label}: "
            f"min={min(values):g} max={max(values):g} mean={_mean(values):g}"
        )
    return stats


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)
