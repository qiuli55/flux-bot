"""S2 结构抽取测试（FLUX_CONTEXT_MECHANISM §5.3 S2 / §5.2）。

S2 的价值全系于两条性质，二者都是 S5（自校验）能不能做的前提：

1. **抽取是选择不是生成**——产物里除合成行（`# ` 开头）外，每一行都逐字来自原文，
   且记在 `Extraction.kept` 里；S5 拿 `kept` 逐字比对原文即可判定"压没压错"。
2. **没有静默损失**——`len(kept) + dropped_lines == source_lines`。

其余几条盯的是"分流对不对"与"确定性"：六种形态各判各的、同一段文本永远同一份产物。
"""

from __future__ import annotations

from flux.core.context.compress.extract import (
    ContentForm,
    Extraction,
    classify,
    extract,
)


def _assert_sound(extraction: Extraction, text: str) -> None:
    """任何抽取产物都必须满足的两条不变量（S5 的前提）。"""
    original = text.splitlines()
    assert extraction.source_lines == len(original)
    assert len(extraction.kept) + extraction.dropped_lines == extraction.source_lines
    # kept 的每一行都逐字出现在原文行里（S5 只认这批行）
    assert all(line in original for line in extraction.kept)
    # 逐字行都出现在渲染结果里
    assert all(line in extraction.lines for line in extraction.kept)


# ---------------------------------------------------------------------------
# 形态分类
# ---------------------------------------------------------------------------


def test_classify_terminal() -> None:
    text = "$ pytest -q\n1 passed in 0.10s"
    assert classify(text) is ContentForm.TERMINAL


def test_classify_log() -> None:
    text = "\n".join(
        [
            "2024-01-01 10:00:00 INFO 启动",
            "2024-01-01 10:00:01 INFO 就绪",
            "2024-01-01 10:00:02 INFO 退出",
        ]
    )
    assert classify(text) is ContentForm.LOG


def test_classify_diff() -> None:
    text = "diff --git a/x.py b/x.py\nindex 111..222 100644\n@@ -1 +1 @@\n-a\n+b"
    assert classify(text) is ContentForm.DIFF


def test_classify_source() -> None:
    text = "\n".join(
        ["import os", "", "class Beta:", "    def run(self):", "        return 1", "def helper():"]
    )
    assert classify(text) is ContentForm.SOURCE


def test_classify_data_json() -> None:
    assert classify('{"a": 1, "b": [2, 3]}') is ContentForm.DATA


def test_classify_data_table() -> None:
    assert classify("name,score\nalice,90\nbob,80") is ContentForm.DATA


def test_classify_plain() -> None:
    assert classify("这是一段没有任何结构的说明文字，只是普通段落。") is ContentForm.PLAIN


def test_classify_empty_is_plain() -> None:
    assert classify("") is ContentForm.PLAIN


# ---------------------------------------------------------------------------
# 终端
# ---------------------------------------------------------------------------


def test_terminal_keeps_error_exit_result_and_folds_middle() -> None:
    text = "\n".join(
        [
            "$ pytest tests/test_auth.py -q",
            "collecting ...",
            "collected 12 items",
            "........",  # 折叠
            ".......",  # 折叠
            "Traceback (most recent call last):",
            '  File "auth.py", line 12, in login',
            "AssertionError: expected 200",
            "FAILED tests/test_auth.py::test_login",
            "1 failed, 11 passed in 0.42s",
        ]
    )
    result = extract(text, ContentForm.TERMINAL)
    _assert_sound(result, text)
    assert "Traceback (most recent call last):" in result.kept
    # 错误行后面那行定位行（缩进）也跟着留
    assert '  File "auth.py", line 12, in login' in result.kept
    assert "1 failed, 11 passed in 0.42s" in result.kept
    assert "… 省略 2 行输出" in result.lines
    assert result.dropped_lines == 2


def test_terminal_keeps_exit_code_line() -> None:
    text = "\n".join(["$ build", "line a", "line b", "line c", "Exited with code 1"])
    result = extract(text, ContentForm.TERMINAL)
    _assert_sound(result, text)
    assert "Exited with code 1" in result.kept


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------


def test_log_merges_same_template() -> None:
    text = "\n".join(
        [
            "2024-01-01 10:00:00 INFO 请求开始 id=1001",
            "2024-01-01 10:00:01 INFO 请求开始 id=1002",
            "2024-01-01 10:00:02 INFO 请求开始 id=1003",
        ]
    )
    result = extract(text, ContentForm.LOG)
    _assert_sound(result, text)
    # 三条同模板 → 只留首次逐字 + 一个计数标记
    assert result.lines[0] == "2024-01-01 10:00:00 INFO 请求开始 id=1001"
    assert result.lines[1] == "[×3] 相同模板"
    assert result.kept == ("2024-01-01 10:00:00 INFO 请求开始 id=1001",)
    assert result.dropped_lines == 2


def test_log_distinct_templates_all_kept() -> None:
    text = "\n".join(
        [
            "2024-01-01 10:00:00 INFO 启动完毕",
            "2024-01-01 10:00:01 ERROR 连接超时",
        ]
    )
    result = extract(text, ContentForm.LOG)
    _assert_sound(result, text)
    assert result.dropped_lines == 0
    assert len(result.kept) == 2


# ---------------------------------------------------------------------------
# 差异
# ---------------------------------------------------------------------------


def test_diff_keeps_changes_and_folds_context() -> None:
    text = "\n".join(
        [
            "diff --git a/x.py b/x.py",
            "index 1234567..89abcde 100644",
            "--- a/x.py",
            "+++ b/x.py",
            "@@ -1,5 +1,6 @@",
            " def foo():",  # 上下文，折叠
            "     return 1",  # 上下文，折叠
            "-    return 2",
            "+    return 3",
            "+    # comment",
        ]
    )
    result = extract(text, ContentForm.DIFF)
    _assert_sound(result, text)
    assert "@@ -1,5 +1,6 @@" in result.kept
    assert "-    return 2" in result.kept
    assert "+    return 3" in result.kept
    assert " def foo():" not in result.kept
    assert "… 省略 2 行上下文" in result.lines
    assert result.dropped_lines == 2


# ---------------------------------------------------------------------------
# 文件内容（大纲）
# ---------------------------------------------------------------------------


def test_source_outline_lists_symbols_with_line_numbers() -> None:
    text = "\n".join(
        [
            "import os",
            "",
            "def alpha():",
            "    return 1",
            "",
            "class Beta:",
            "    def method(self):",
            "        pass",
        ]
    )
    result = extract(text, ContentForm.SOURCE)
    _assert_sound(result, text)
    assert result.lines[0].startswith("# 文件大纲：共 8 行，符号 3 个")
    assert "[L3]" in result.lines
    assert "[L6]" in result.lines
    assert "[L7]" in result.lines
    assert result.kept == ("def alpha():", "class Beta:", "    def method(self):")
    assert result.dropped_lines == 5


def test_source_without_symbols_falls_back_to_window() -> None:
    text = "\n".join(f"line {index}" for index in range(12))
    result = extract(text, ContentForm.SOURCE)
    _assert_sound(result, text)
    assert result.form is ContentForm.SOURCE
    # 认不出符号 → 退回首尾窗口（12 行留 10 行，中间 2 行折叠）
    assert result.dropped_lines == 2


# ---------------------------------------------------------------------------
# 结构化数据
# ---------------------------------------------------------------------------


def test_data_json_summary() -> None:
    text = '{"name": "flux", "count": 3, "items": [1, 2, 3], "meta": {"a": 1}}'
    result = extract(text, ContentForm.DATA)
    _assert_sound(result, text)
    assert "# JSON 对象：4 个键" in result.lines
    assert "#   count: int" in result.lines
    assert "#   items: array(3 项)" in result.lines
    assert "#   meta: object(1 键)" in result.lines


def test_data_table_summary() -> None:
    text = "\n".join(
        [
            "name,score,age",
            "alice,90,30",
            "bob,80,25",
            "carol,70,40",
        ]
    )
    result = extract(text, ContentForm.DATA)
    _assert_sound(result, text)
    assert any(line.startswith("# 表格：4 行 × 3 列") for line in result.lines)
    assert "#   列: name | score | age" in result.lines
    assert any(line.startswith("#   数值列 score: min=70 max=90") for line in result.lines)


# ---------------------------------------------------------------------------
# 兜底 / 边界
# ---------------------------------------------------------------------------


def test_plain_keeps_head_and_tail() -> None:
    text = "\n".join(f"第 {index} 行" for index in range(15))
    result = extract(text)
    _assert_sound(result, text)
    assert result.form is ContentForm.PLAIN
    assert result.kept[0] == "第 0 行"
    assert result.kept[-1] == "第 14 行"
    assert "… 省略 5 行" in result.lines
    assert result.dropped_lines == 5


def test_empty_text() -> None:
    result = extract("")
    assert result.form is ContentForm.PLAIN
    assert result.lines == ()
    assert result.kept == ()
    assert result.dropped_lines == 0
    assert result.source_lines == 0


def test_long_line_is_not_kept_verbatim() -> None:
    """逐字留一行超长内容等于没压：这种内容该走 L3 只留 ref。"""
    long_line = "x" * 500
    text = "\n".join(["head", long_line, "tail"])
    result = extract(text, ContentForm.PLAIN)
    _assert_sound(result, text)
    assert long_line not in result.kept


def test_render_joins_lines() -> None:
    text = "$ run\n1 passed"
    result = extract(text, ContentForm.TERMINAL)
    assert result.render() == "\n".join(result.lines)


def test_extraction_is_deterministic() -> None:
    """同一段文本永远得到同一份产物（I7 幂等 / S5 可复现）。"""
    text = "\n".join(
        [
            "diff --git a/x.py b/x.py",
            "index 1234567..89abcde 100644",
            "@@ -1,5 +1,6 @@",
            " context",
            "-old",
            "+new",
            "+extra",
        ]
    )
    assert extract(text) == extract(text)
