"""符号表字典编码测试（FLUX_CONTEXT_MECHANISM §5.3 S1 / §5.2）。

S1 的价值全系于一个性质：**可逆**。所以这里最主要的一条是 `decode(encode(x)) == x`。
其余几条盯的是"选得对不对"与"确定性"：

- 只编码**反复出现**的结构化长串（出现一次的不叫重复，编码它反而亏）；
- 统计与替换用同一把尺子——`x/src/a.py` 不会被当成 `src/a.py` 替换掉；
- 同一批文本永远得到同一张表（否则产物不可复现，I7 / S5 无从谈起）；
- 原文若已含 `@` + 数字，符号前缀要主动让开，否则解码会误伤原文。
"""

from __future__ import annotations

from flux.core.context.compress.symbols import SymbolTable, build_symbol_table

_PATH = "src/auth/session/token_validator.py"
_CMD = "pytest tests/test_auth.py -x -q"


def test_round_trip_is_lossless() -> None:
    """I2 在压缩层的落点：编码再解码，逐字回到原文。"""
    texts = [f"打开 {_PATH} 看看", f"再打开 {_PATH}，然后跑 {_CMD}", f"{_CMD} 失败了"]
    table = build_symbol_table(texts)
    assert table  # 有重复长串，表不该是空的
    for text in texts:
        encoded = table.encode(text)
        assert encoded != text  # 确实压了
        assert table.decode(encoded) == text


def test_repeated_long_path_is_encoded() -> None:
    table = build_symbol_table([_PATH, _PATH])
    assert table.symbols[_PATH] == "@1"
    assert table.render() == f"@1 = {_PATH}"


def test_single_occurrence_is_not_encoded() -> None:
    """出现一次的长串编码它反而亏（定义开销 > 省下的），交给 S2 / S3。"""
    table = build_symbol_table([f"只出现一次的 {_PATH}"])
    assert not table


def test_short_repeats_are_not_encoded() -> None:
    """短串不在候选范围内。"""
    table = build_symbol_table(["a.py a.py a.py"])
    assert not table


def test_plain_long_words_are_not_encoded() -> None:
    """没有路径 / 点 / 下划线结构的长单词不是"路径 / URL / 模块名"，不收。"""
    word = "supercalifragilisticexpialidocious"
    table = build_symbol_table([word, word])
    assert not table


def test_substring_of_longer_run_is_untouched() -> None:
    """`prefix/src/a.py` 是另一个字符段，不能被 `src/a.py` 的替换波及。"""
    longer = f"prefix/{_PATH}"
    texts = [f"{_PATH} 一次", f"{_PATH} 两次", longer]
    table = build_symbol_table(texts)
    assert _PATH in table.symbols
    assert table.encode(longer) == longer


def test_table_is_deterministic() -> None:
    """同一批文本 → 同一张表（同序号、同顺序），否则压缩产物不可复现。"""
    texts = [f"{_PATH} 与 {_CMD} 一起出现", f"{_PATH} 再来一次", f"{_CMD} 再来一次"]
    first = build_symbol_table(texts)
    second = build_symbol_table(texts)
    assert first == second


def test_sigil_avoids_collision_with_source() -> None:
    """原文已含 `@` + 数字时换前缀，保证解码不把原文里的 `@1` 误展开。"""
    texts = [f"日志里写了 @1 这个记号，还提到 {_PATH}", f"又提到 {_PATH}"]
    table = build_symbol_table(texts)
    assert table  # 有重复长串
    assert table.sigil != "@"
    assert table.decode(table.encode(texts[0])) == texts[0]


def test_empty_corpus_yields_empty_table() -> None:
    table = build_symbol_table([])
    assert table == SymbolTable()
    assert not table
    assert len(table) == 0
    assert table.render() == ""


def test_empty_table_is_identity() -> None:
    """空表下编解码都不该改动文本。"""
    table = build_symbol_table([])
    assert table.encode("原样返回") == "原样返回"
    assert table.decode("原样返回") == "原样返回"
