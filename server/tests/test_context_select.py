"""S3 抽取式筛选测试（FLUX_CONTEXT_MECHANISM §5.3 S3）。

S3 与 S2 共享同一条铁律：**抽取不是生成**。所以这里首先盯的是"逐字性"——
`Selection.items` 里每一项都必须原样出现在输入里；其次是确定性（同一批句子 +
同一个查询，永远同一份选择）；再就是公式的三项各自是否真在起作用：

1. BM25 相关性——含查询词的句子该被挑中；
2. 类型先验——同样含查询词，错误行要压过普通行；
3. MMR——两份一模一样的句子只留一份。
"""

from __future__ import annotations

from flux.core.context.compress.select import Selection, select, split_sentences

# ---------------------------------------------------------------------------
# split_sentences：按行 + 按句末标点细切，空行丢弃，逐字保留
# ---------------------------------------------------------------------------


def test_split_sentences_by_line() -> None:
    """一行一句：空行丢弃，其余逐字保留。"""
    units = split_sentences("\n\nfoo\n\nbar\n")
    assert units == ["foo", "bar"]


def test_split_sentences_by_punctuation() -> None:
    """同一行按句末标点再切，拼回去仍是原句。"""
    text = "Hello world! Second one?"
    units = split_sentences(text)
    assert units == ["Hello world!", " Second one?"]
    assert "".join(units) == text


def test_split_sentences_keeps_ascii_period() -> None:
    """ASCII 句点不切——`foo.py`、`1.2.3` 不能被拆散。"""
    assert split_sentences("keep foo.py and 1.2.3 whole") == ["keep foo.py and 1.2.3 whole"]


def test_split_sentences_chinese_punctuation() -> None:
    """中文句末标点同样认。"""
    assert split_sentences("第一句。第二句！") == ["第一句。", "第二句！"]


def test_split_sentences_keeps_indent() -> None:
    """缩进逐字保留——S5 要拿它逐字回验。"""
    assert split_sentences("    indented line") == ["    indented line"]


def test_split_sentences_items_are_substrings() -> None:
    """切出来的每一项都是原文的子串。"""
    text = "alpha beta\nValueError: gone\n第二句。第三句！"
    units = split_sentences(text)
    assert units
    assert all(unit in text for unit in units)


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------


def test_select_empty() -> None:
    """空输入：空结果、零候选。"""
    result = select([], "query", limit=3)
    assert result == Selection((), 0)
    assert result.dropped == 0


def test_select_non_positive_limit() -> None:
    """`limit <= 0`：一项不选，但候选数要如实记下。"""
    result = select(["a", "b"], "query", limit=0)
    assert result.items == ()
    assert result.considered == 2
    assert result.dropped == 2


def test_select_limit_covers_all() -> None:
    """`limit >= 候选数`：全留，且保持原文顺序。"""
    result = select(["second", "first"], "query", limit=5)
    assert result.items == ("second", "first")
    assert result.dropped == 0


def test_select_items_are_verbatim() -> None:
    """选中项逐字来自输入。"""
    sentences = ["alpha hit", "nothing here", "beta hit"]
    result = select(sentences, "alpha", limit=3)
    assert all(item in sentences for item in result.items)


def test_select_is_deterministic() -> None:
    """同一批句子 + 同一个查询，结果恒等（I7 幂等）。"""
    sentences = ["alpha one", "alpha two", "beta three", "gamma four"]
    first = select(sentences, "alpha", limit=2)
    second = select(sentences, "alpha", limit=2)
    assert first == second


# ---------------------------------------------------------------------------
# 公式三项
# ---------------------------------------------------------------------------


def test_select_prefers_query_terms() -> None:
    """BM25：含查询词的句子优先于不含者。"""
    result = select(["beta beta", "alpha gamma"], "alpha", limit=1)
    assert result.items == ("alpha gamma",)


def test_select_error_line_wins() -> None:
    """类型先验：都含查询词时，错误行压过普通行（哪怕它在更靠前的位置）。"""
    sentences = ["ValueError alpha beta delta", "alpha beta gamma delta"]
    result = select(sentences, "alpha", limit=1)
    assert result.items == ("ValueError alpha beta delta",)


def test_select_mmr_drops_duplicate() -> None:
    """MMR：多样性权重大时，两份相同的句子只留一份。"""
    sentences = ["alpha dupe content", "alpha dupe content", "beta different stuff"]
    result = select(sentences, "alpha", limit=2, mmr_lambda=0.3)
    assert result.items == ("alpha dupe content", "beta different stuff")


def test_select_pure_relevance_keeps_duplicate() -> None:
    """MMR：`mmr_lambda=1.0` 时退化为纯相关性，重复项照样入选。"""
    sentences = ["alpha dupe content", "alpha dupe content", "beta different stuff"]
    result = select(sentences, "alpha", limit=2, mmr_lambda=1.0)
    assert result.items == ("alpha dupe content", "alpha dupe content")
