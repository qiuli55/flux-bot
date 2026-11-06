"""S5 自校验测试（FLUX_CONTEXT_MECHANISM §5.3 S5 / §5.6）。

S5 是压缩链路的最后一道闸，判据只有一条：**产物必须逐字忠实于原文**。这里验证它既
不会漏放（该拦的坏产物必须拦下），也不会误杀（S2 抽取的正常产物必须通过）——

1. 恒通过：`verify(source, extract(source).render())` 在任何形态下都必须过，这是
   §5.6「重建一致率恒 100%」在单段上的形式；
2. 拦得住：改写行、行号错位、sha256 / 用例名 / exit code / 命令行被动手脚，一律判否；
3. 不误杀：`…` / `[×` / `[L` / `# ` 这类 S2 合成的标记行本就不来自原文，跳过。
"""

from __future__ import annotations

import pytest

from flux.core.context.compress import extract, verify

_SHA_A = "3b1f9c0d5e6a7b8c9d0e1f2a3b4c5d6e7f8091a2"
_SHA_B = "3b1f9c0d5e6a7b8c9d0e1f2a3b4c5d6e7f8091ff"

_SAMPLES = {
    "terminal": "$ pytest -q\n1 passed in 0.10s\nexit code 0",
    "log": "\n".join(
        [
            "2024-01-01 10:00:00 INFO 启动",
            "2024-01-01 10:00:01 INFO 就绪",
            "2024-01-01 10:00:02 INFO 退出",
        ]
    ),
    "diff": "diff --git a/x.py b/x.py\nindex 111..222 100644\n@@ -1 +1 @@\n-a\n+b",
    "source": "\n".join(
        [
            "import os",
            "def alpha():",
            "    return 1",
            "class Beta:",
            "    pass",
        ]
    ),
    "data": '{"id": 1, "name": "todo", "done": false}',
    "plain": "\n".join(f"第 {index} 行普通说明文字" for index in range(1, 21)),
}


@pytest.mark.parametrize("form", sorted(_SAMPLES))
def test_extraction_render_always_verifies(form: str) -> None:
    """抽取产物的自校验恒通过，且确实逐行查过（不是空跑）。"""
    text = _SAMPLES[form]
    result = verify(text, extract(text).render())

    assert result.ok is True
    assert result.failures == ()
    assert result.checked > 0


def test_verify_accepts_identical_text() -> None:
    text = "第一行\n第二行\n"
    assert verify(text, text).ok is True


def test_verify_skips_extraction_markers() -> None:
    """标记行与合成行是结构描述，不该被当成"非逐字行"误杀。"""
    text = "\n".join(["alpha", "beta", "gamma", "delta", "epsilon", "zeta"])
    rendered = "\n".join(
        [
            "# 文件大纲：共 6 行，符号 2 个",
            "[L1]",
            "alpha",
            "… 省略 3 行输出",
            "[×2] 相同模板",
            "[L6]",
            "zeta",
        ]
    )
    assert verify(text, rendered).ok is True


def test_verify_catches_a_rewritten_line() -> None:
    text = "def list_todos():\n    return []\n"
    rendered = "def list_todos():\n    return None\n"
    result = verify(text, rendered)

    assert result.ok is False
    assert any("非逐字行" in failure for failure in result.failures)


def test_verify_catches_a_stale_line_number() -> None:
    """`[L{k}]` 不是装饰而是坐标：指向的下一行必须等于原文第 k 行。"""
    text = "\n".join(["alpha", "beta", "gamma"])
    stale = "\n".join(["[L2]", "gamma"])
    out_of_range = "\n".join(["[L9]", "alpha"])

    assert verify(text, stale).ok is False
    assert verify(text, out_of_range).ok is False
    assert any("行号 2 对不上原文" in failure for failure in verify(text, stale).failures)


def test_verify_catches_changed_sha256() -> None:
    text = f"commit {_SHA_A}\n"
    assert verify(text, f"commit {_SHA_B}\n").ok is False


def test_verify_catches_changed_test_name() -> None:
    text = "FAILED tests/test_auth.py::test_login - AssertionError\n"
    rendered = "FAILED tests/test_auth.py::test_logout - AssertionError\n"
    assert verify(text, rendered).ok is False


def test_verify_catches_changed_exit_code() -> None:
    text = "pytest -q\nexit code 0\n"
    assert verify(text, "pytest -q\nexit code 1\n").ok is False


def test_verify_catches_changed_command() -> None:
    text = "$ pytest -q\n1 passed\n"
    assert verify(text, "$ pytest tests/test_auth.py\n1 passed\n").ok is False


def test_verify_reports_what_it_checked() -> None:
    text = "\n".join(["alpha", "beta", "gamma"])
    result = verify(text, text)

    assert result.checked == 3
    assert result.failures == ()
