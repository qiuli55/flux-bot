"""S3 抽取式筛选（FLUX_CONTEXT_MECHANISM §5.3 S3）。

S2 处理的是"冗长"（刷屏、同模板、上下文行、整份正文）；中段历史里还剩"无关"这一
类冗余——一大段文字里真正与当前任务有关的只有几行。S3 就是从中**挑**出这几行：

```
score = BM25(句, 任务描述 + 验收标准)
      × 类型先验        （错误行 / 断言 / 结论权重更高）
      × 位置衰减        （越靠后越相关）
      × MMR            （抑制与已选内容重复的句子）
```

两条铁律：

1. **抽取不是生成**（§5.1、§5.3）。选中即**原文逐字照抄**：`Selection.items` 里每一项
   都是输入里某一项的**原样副本**，窗口压缩后 S5 照样逐字比对得上。
2. **确定性**：同一批句子 + 同一个查询，永远得到同一份选择（I7 幂等 / S5 可复现）——
   全程无随机、无模型；同分一律取原文位置靠前者。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

# 错误 / 退出 / 结果三类判据与 S2 共用一套，避免"什么算结论"在两处漂移。
from flux.core.context.compress.extract import (
    _is_error_line,
    _is_exit_line,
    _is_result_line,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: BM25 参数：词频饱和（k1）与文档长度归一（b）
_K1 = 1.5
_B = 0.75
#: MMR 的 λ：相关性权重，`1 - λ` 是多样性权重
_MMR_LAMBDA = 0.6
#: 位置权重：首句 1.0，末句 `_POSITION_TAIL`，中间线性插值（§5.3 S3「越靠后越相关」）
_POSITION_TAIL = 1.5
#: 类型先验：错误 / 退出行最该被看见，结论次之，断言再次之
_PRIOR_ERROR = 2.0
_PRIOR_CONCLUSION = 1.4
_PRIOR_ASSERTION = 1.2
_PRIOR_BASE = 1.0

#: 句子切分：按行，再按句末标点细切（中英文标点都认）
_SENTENCE_RE = re.compile(r"[^。！？!?；;]+[。！？!?；;]?")
#: 断言的写法（不区分大小写；中文断言难以枚举，靠错误行先验兜底）
_ASSERTION_RE = re.compile(r"\b(?:assert|expect)\w*\b", re.IGNORECASE)
#: 分词：ASCII 词元（含下划线）与单个汉字各算一个 token
_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]")


@dataclass(frozen=True)
class Selection:
    """一次抽取式筛选的结果。

    `items` 按**原文顺序**给出（读起来不跳），每一项都逐字来自输入；
    `considered` 是候选总数，`dropped` 是被筛掉的项数。
    """

    items: tuple[str, ...]
    considered: int

    @property
    def dropped(self) -> int:
        return self.considered - len(self.items)


def split_sentences(text: str) -> list[str]:
    """把一段文本切成"句"：先按行，再按句末标点细切。

    空行丢弃，其余片段**逐字保留**（含缩进）——切出来的每一项都是原文的子串，
    S5 才能拿它逐字回验。
    """
    units: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        units.extend(piece for piece in _SENTENCE_RE.findall(line) if piece.strip())
    return units


def select(
    sentences: Sequence[str],
    query: str,
    *,
    limit: int,
    mmr_lambda: float = _MMR_LAMBDA,
) -> Selection:
    """从 `sentences` 里挑出与 `query`（任务描述 + 验收标准）最相关的至多 `limit` 项。

    选中项按原文顺序返回、逐字来自 `sentences`；`limit` 大于等于候选数时全留。
    """
    items = list(sentences)
    count = len(items)
    if count == 0:
        return Selection((), 0)
    if limit <= 0:
        return Selection((), count)

    vectors = [Counter(_tokenize(item)) for item in items]
    relevance = _normalize(_relevance(items, vectors, query))

    chosen = list(range(count)) if limit >= count else _mmr(relevance, vectors, limit, mmr_lambda)
    chosen.sort()
    return Selection(tuple(items[index] for index in chosen), count)


# ---------------------------------------------------------------------------
# 相关性：BM25 × 类型先验 × 位置衰减
# ---------------------------------------------------------------------------


def _relevance(items: list[str], vectors: list[Counter[str]], query: str) -> list[float]:
    """逐句的相关性原始分：BM25 主体，再乘类型先验与位置权重。"""
    count = len(items)
    terms = list(dict.fromkeys(_tokenize(query)))
    lengths = [sum(vector.values()) for vector in vectors]
    avgdl = sum(lengths) / count if count else 0.0

    document_freq = Counter()
    for vector in vectors:
        for term in terms:
            if term in vector:
                document_freq[term] += 1

    scores: list[float] = []
    for index, (vector, length) in enumerate(zip(vectors, lengths, strict=True)):
        total = 0.0
        for term in terms:
            freq = vector.get(term, 0)
            if freq == 0:
                continue
            idf = math.log(1 + (count - document_freq[term] + 0.5) / (document_freq[term] + 0.5))
            norm = 1.0 if avgdl <= 0 else 1 - _B + _B * length / avgdl
            total += idf * freq * (_K1 + 1) / (freq + _K1 * norm)
        scores.append(total * _type_prior(items[index]) * _position_weight(index, count))
    return scores


def _type_prior(line: str) -> float:
    """类型先验：错误 / 退出 > 结论 > 断言 > 其余（§5.3 S3）。"""
    if _is_error_line(line) or _is_exit_line(line):
        return _PRIOR_ERROR
    if _is_result_line(line):
        return _PRIOR_CONCLUSION
    if _ASSERTION_RE.search(line):
        return _PRIOR_ASSERTION
    return _PRIOR_BASE


def _position_weight(index: int, count: int) -> float:
    """位置权重：越靠后越相关（首句 1.0，末句 `_POSITION_TAIL`）。"""
    if count <= 1:
        return _PRIOR_BASE
    return _PRIOR_BASE + (_POSITION_TAIL - _PRIOR_BASE) * index / (count - 1)


def _normalize(values: list[float]) -> list[float]:
    """把相关性缩到 [0, 1]——MMR 要拿它和 [0, 1] 的余弦相似度做加权。"""
    peak = max(values, default=0.0)
    if peak <= 0:
        return [0.0] * len(values)
    return [value / peak for value in values]


# ---------------------------------------------------------------------------
# MMR：在相关性之外再按"与已选内容不重复"挑
# ---------------------------------------------------------------------------


def _mmr(
    relevance: list[float],
    vectors: list[Counter[str]],
    limit: int,
    mmr_lambda: float,
) -> list[int]:
    """贪心 MMR：每步取 `λ × 归一化相关性 − (1−λ) × 与已选的最大相似度`。

    同分取原文位置靠前者，保证确定性。
    """
    selected = [max(range(len(relevance)), key=lambda index: (relevance[index], -index))]
    candidates = [index for index in range(len(relevance)) if index not in selected]
    while candidates and len(selected) < limit:
        best_index = None
        best_score = 0.0
        for index in candidates:
            similarity = max(_cosine(vectors[index], vectors[chosen]) for chosen in selected)
            score = mmr_lambda * relevance[index] - (1 - mmr_lambda) * similarity
            if best_index is None or score > best_score:
                best_index, best_score = index, score
        selected.append(best_index)
        candidates.remove(best_index)
    return selected


def _cosine(left: Counter[str], right: Counter[str]) -> float:
    """两个词频向量的余弦相似度（都为空或只有一个为空时记 0）。"""
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    dot = sum(freq * right.get(term, 0) for term, freq in left.items())
    if dot == 0:
        return 0.0
    return dot / math.sqrt(_norm(left) * _norm(right))


def _norm(vector: Counter[str]) -> float:
    return sum(freq * freq for freq in vector.values())


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())
