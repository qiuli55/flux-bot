"""知识库文本基元与切块器测试。

切块是检索质量的地基：这里只验证「可预期」的部分——标题路径、token 预算、
对话窗口重叠与超长消息兜底，不追求与真实 tokenizer 逐字对齐。
"""

from __future__ import annotations

from flux.core.knowledge.chunker import (
    ConversationMessage,
    chunk_conversation,
    chunk_markdown,
)
from flux.core.knowledge.text import estimate_tokens, snippet, tokenize

# --- text ---


def test_estimate_tokens_counts_cjk_per_char_and_ascii_per_four() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("中文") == 2
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_tokenize_keeps_ascii_words_and_cjk_bigrams() -> None:
    tokens = tokenize("Deploy 部署失败")

    assert "deploy" in tokens
    assert "部署" in tokens
    assert "署失" in tokens
    assert "失败" in tokens
    # 单字兜底仍然保留
    assert "部" in tokens


def test_snippet_windows_around_first_hit() -> None:
    text = "前文" * 100 + "E500 是部署失败的根因" + "后文" * 100

    result = snippet(text, ["e500"], max_chars=60)

    assert "E500" in result
    assert len(result) <= 62
    assert result.startswith("…")
    assert result.endswith("…")


def test_snippet_returns_short_text_unchanged() -> None:
    assert snippet("很短的一句话", ["短"]) == "很短的一句话"


# --- markdown ---


def test_markdown_chunks_carry_heading_path() -> None:
    text = "# 部署手册\n\n总览。\n\n## 失败排查\n\n先看 E500 日志。\n"

    chunks = chunk_markdown(text, max_tokens=200, overlap_tokens=0)

    assert [chunk.title for chunk in chunks] == ["部署手册", "部署手册 / 失败排查"]
    assert chunks[1].content == "先看 E500 日志。"
    assert chunks[1].meta["heading_path"] == ["部署手册", "失败排查"]


def test_markdown_keeps_blank_lines_inside_code_fence() -> None:
    text = "## 示例\n\n```\n第一行\n\n第二行\n```\n"

    chunks = chunk_markdown(text, max_tokens=200, overlap_tokens=0)

    assert len(chunks) == 1
    assert "第一行\n\n第二行" in chunks[0].content


def test_markdown_splits_long_paragraph_within_budget() -> None:
    text = "## 长文\n\n" + "句子。" * 200

    chunks = chunk_markdown(text, max_tokens=40, overlap_tokens=10)

    assert len(chunks) > 1
    # 重叠会让新块带着上一块的尾巴，允许超出 overlap 的预算
    assert all(chunk.token_count <= 50 for chunk in chunks)


def test_markdown_blank_text_returns_no_chunks() -> None:
    assert chunk_markdown("   \n\n", max_tokens=100, overlap_tokens=10) == []


# --- conversation ---


def test_conversation_windows_overlap_one_message() -> None:
    messages = [ConversationMessage(seq=index, role="user", content="ok") for index in range(1, 5)]

    chunks = chunk_conversation(messages, max_tokens=5, overlap_messages=1)

    assert len(chunks) >= 2
    assert chunks[0].meta == {"first_seq": 1, "last_seq": 2}
    # 第二个窗口从上一窗口的最后一条开始，保留问答衔接
    assert chunks[1].meta["first_seq"] == 2


def test_conversation_oversized_message_is_split_independently() -> None:
    messages = [ConversationMessage(seq=7, role="assistant", content="甲" * 60)]

    chunks = chunk_conversation(messages, max_tokens=30)

    assert len(chunks) > 1
    assert all(chunk.meta == {"first_seq": 7, "last_seq": 7} for chunk in chunks)


def test_conversation_empty_input_returns_no_chunks() -> None:
    assert chunk_conversation([], max_tokens=100) == []
