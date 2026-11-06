"""S1 符号表字典编码（FLUX_CONTEXT_MECHANISM §5.3 S1 / §5.2）。

压缩的四个冗余来源里，第一个是**重复**（§5.2）：同一路径、同一命令、同一 URL
在日志里反复出现。S1 把这类**高频长串**登记进符号表，正文只引 `@1`、`@2`：

    @1 = src/auth/session/token_validator.py
    @2 = pytest tests/test_auth.py -x -q

**纯机械替换，零信息损失**：`decode(encode(x)) == x` 恒成立。这是 I2（原文满保真）
在压缩层的落点——投喂时把符号表放在文首（`render()`），要原文时逐字还原。
本模块不产生任何新内容，也就没有任何幻觉可言（§5.1：压缩 = 选择 + 编码）。

两条实现上的硬约束，都是为了让"可逆"真的成立：

1. **按"最长连续字符段"整体替换**，不按子串匹配。`_TOKEN_RUN_RE` 取的是极大段，
   所以 `x/src/a.py` 不会被当成 `src/a.py` 替换掉——统计与替换用同一把尺子，
   两边永远不会漂移。
2. **符号名不得与原文撞车**。`@1` 里的 `@` 也在字符段内，故原文若已含 `@` + 数字，
   解码时会被误展开；`_choose_sigil` 因此先挑一个原文里没出现过的前缀
   （几乎总是 `@`，极端情况才退到 `§` 等）。段与段之间必有非字符段字符，
   替换后也不会拼出 `@12` 这种"越界数字"，故解码单遍正则即可精确还原。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache

#: 多短的长串不值得编码（符号本身也要占位置）
_MIN_TOKEN_CHARS = 12
#: 只编码**反复出现**的长串——出现一次的不叫重复（§5.2）
_MIN_COUNT = 2
#: 符号表条目上限，避免"为了压缩反而撑爆文首"
_MAX_SYMBOLS = 64
#: 定义一行的固定开销：`@k = ` + 换行
_DEFINITION_OVERHEAD = 6
#: 依次尝试的符号前缀；正常文本几乎总落在第一个
_SIGILS = ("@", "§", "¶", "¤", "Ω")
#: 候选长串的字符集：路径 / URL / 模块名 / 长标识符会遇到的全部字符
_TOKEN_CHARS = r"A-Za-z0-9_@./\\:\-"
#: 字符段两端必须是字母 / 数字 / 下划线，否则是标点尾巴（如 `src/a.py.`）而非长串
_EDGE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
#: "结构化"长串：含路径分隔符 / 点 / 下划线 / scheme 之一，排除普通长单词
_STRUCTURED_RE = re.compile(r"[./\\_@]")


@lru_cache(maxsize=8)
def _token_run_re(min_chars: int) -> re.Pattern[str]:
    """极大字符段正则：同一把尺子既用来统计、也用来替换（见模块 docstring 第 1 条）。"""
    return re.compile(rf"[{_TOKEN_CHARS}]{{{min_chars},}}")


@dataclass(frozen=True)
class SymbolTable:
    """一张符号表（§5.3 S1）：`entries` 是 `(原串, 符号)`，序号即编码顺序。

    只有 `build_symbol_table` 产出的表才保证可逆——`encode` / `decode` 必须配成一对，
    拿别人的编码内容来解，或者用一张表编、另一张表解，都会静默错位。
    """

    sigil: str = "@"
    entries: tuple[tuple[str, str], ...] = ()
    #: 建表时用的字符段长度下限；`encode` 必须用同一把尺子（否则替换面会漂移）
    min_chars: int = _MIN_TOKEN_CHARS

    def __bool__(self) -> bool:
        return bool(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def encode(self, text: str) -> str:
        """把文本里登记过的长串换成符号（按极大字符段整体替换）。"""
        if not self.entries:
            return text
        lookup = self.symbols
        pattern = _token_run_re(self.min_chars)
        return pattern.sub(lambda match: lookup.get(match.group(0), match.group(0)), text)

    def decode(self, text: str) -> str:
        """把符号换回原串——`decode(encode(x)) == x` 是这里的全部意义（I2）。"""
        if not self.entries:
            return text
        lookup = self._tokens
        pattern = re.compile(rf"{re.escape(self.sigil)}(\d+)")
        return pattern.sub(lambda match: lookup.get(match.group(1), match.group(0)), text)

    def render(self) -> str:
        """渲染符号定义，放在投喂文首（§5.3 S1：符号表在文首给出定义）。"""
        return "\n".join(f"{symbol} = {token}" for token, symbol in self.entries)

    @property
    def symbols(self) -> dict[str, str]:
        """`原串 → 符号`。"""
        return {token: symbol for token, symbol in self.entries}

    @property
    def _tokens(self) -> dict[str, str]:
        """`序号 → 原串`（解码用；序号是符号去掉前缀后的部分）。"""
        return {symbol[len(self.sigil) :]: token for token, symbol in self.entries}


def build_symbol_table(
    texts: Iterable[str],
    *,
    min_chars: int = _MIN_TOKEN_CHARS,
    min_count: int = _MIN_COUNT,
    limit: int = _MAX_SYMBOLS,
) -> SymbolTable:
    """从若干文本里挑出值得编码的长串，编成一张符号表。

    选取是**确定性**的：同一批文本永远得到同一张表（同序号、同顺序），
    否则压缩产物就不可复现，I7（幂等）与 S5（自校验）都无从谈起。

    收益按"编码后净省多少字符"排序，只留净收益为正的：编码一个只出现两次的短串，
    省下的还不如文首定义它的开销——那种长串不如交给 S2 / S3。
    """
    corpus = list(texts)
    counts: Counter[str] = Counter()
    run_re = _token_run_re(min_chars)
    for text in corpus:
        counts.update(match.group(0) for match in run_re.finditer(text))

    candidates = [
        (token, count)
        for token, count in counts.items()
        if count >= min_count and _is_compressible(token)
    ]
    # 符号名长度按上限估：序号最多到 limit，估值与实际几乎一致，且避免"边排序边变长"
    symbol_cost = len(_SIGILS[0]) + len(str(max(limit, 1)))
    ranked = sorted(
        ((_net_saving(token, count, symbol_cost), token) for token, count in candidates),
        key=lambda item: (-item[0], item[1]),
    )
    chosen = [token for saving, token in ranked[:limit] if saving > 0]
    if not chosen:
        return SymbolTable()

    sigil = _choose_sigil(corpus)
    entries = tuple((token, f"{sigil}{index}") for index, token in enumerate(chosen, start=1))
    return SymbolTable(sigil=sigil, entries=entries, min_chars=min_chars)


def _is_compressible(token: str) -> bool:
    """是不是"值得编码的长串"：结构化的、两端干净的路径 / URL / 模块名 / 标识符。"""
    if token[0] not in _EDGE_CHARS or token[-1] not in _EDGE_CHARS:
        return False
    return bool(_STRUCTURED_RE.search(token))


def _net_saving(token: str, count: int, symbol_cost: int) -> int:
    """编码这个长串的净收益（字符数）：省下的替换量减去文首的定义开销。"""
    return count * (len(token) - symbol_cost) - len(token) - _DEFINITION_OVERHEAD


def _choose_sigil(corpus: Sequence[str]) -> str:
    """挑一个原文里没出现过 `前缀+数字` 的符号前缀，保证解码不误伤原文（见模块 docstring）。"""
    for sigil in _SIGILS:
        pattern = re.compile(rf"{re.escape(sigil)}\d")
        if not any(pattern.search(text) for text in corpus):
            return sigil
    return _SIGILS[-1]  # pragma: no cover - 需要文本同时含全部前缀才可能走到
