"""对话轨迹装配器（跨轮记忆 · 对话半边）。"""

from __future__ import annotations

from flux.core.task_engine.conversation_trace import build_dialogue_trace


def _msg(role: str, kind: str, content: str, seq: int = 0):
    return type(
        "Msg",
        (),
        {"role": role, "kind": kind, "content": content, "seq": seq},
    )()


def test_empty_messages_returns_empty():
    assert build_dialogue_trace([]) == ""


def test_skips_non_trace_kinds():
    messages = [
        _msg("system", "run_started", "已开始执行", seq=1),
        _msg("user", "text", "把颜色改蓝", seq=2),
    ]
    result = build_dialogue_trace(messages)
    assert "把颜色改蓝" in result
    assert "已开始执行" not in result


def test_orders_by_seq_ascending_and_labels_roles():
    messages = [
        _msg("user", "text", "第一句", seq=1),
        _msg("assistant", "text", "第二句", seq=2),
    ]
    result = build_dialogue_trace(messages)
    assert result.index("[用户] 第一句") < result.index("[助手] 第二句")


def test_excludes_exact_followup_and_keeps_header():
    task_desc = _msg("user", "text", "帮我设计记账工具", seq=1)
    followup = _msg("user", "text", "把主色改成蓝色", seq=2)
    result = build_dialogue_trace([task_desc, followup], exclude_text="把主色改成蓝色")
    assert "帮我设计记账工具" in result
    assert "把主色改成蓝色" not in result
    assert "## 对话轨迹" in result


def test_trims_oldest_when_over_budget():
    messages = [_msg("user", "text", "上" * 500, seq=i) for i in range(1, 6)]
    result = build_dialogue_trace(messages, budget_chars=1200, max_messages=10)
    # 超预算只留最新几条，最旧的被丢
    assert "上" * 500 in result  # 最新一条必然在
    lines = [line for line in result.splitlines() if line.startswith("[用户]")]
    assert len(lines) < 5


def test_clips_single_long_message_to_budget():
    messages = [_msg("user", "text", "长" * 5000, seq=1)]
    result = build_dialogue_trace(messages, budget_chars=200)
    line = next(item for item in result.splitlines() if item.startswith("[用户]"))
    assert len(line) <= 200


def test_confirmation_and_attachment_are_included():
    messages = [
        _msg("assistant", "confirmation", "需求：记账工具", seq=1),
        _msg("user", "attachments", "a.csv、b.csv", seq=2),
    ]
    result = build_dialogue_trace(messages)
    assert "需求确认" in result
    assert "附件" in result
    assert "记账工具" in result


def test_exclude_kinds_drops_sections_with_their_own_block():
    """已单独成段的信息不再进轨迹：同一份内容只付一次 token。"""
    messages = [
        _msg("assistant", "summary", "目标：记账工具", seq=1),
        _msg("assistant", "confirmation", "需求：记账工具", seq=2),
        _msg("user", "attachments", "a.csv", seq=3),
        _msg("user", "text", "把主色改成蓝色", seq=4),
    ]
    result = build_dialogue_trace(
        messages, exclude_kinds=("summary", "confirmation", "attachments")
    )
    assert "把主色改成蓝色" in result
    assert "前情摘要" not in result
    assert "需求确认" not in result
    assert "附件" not in result


def test_exclude_texts_drops_every_injected_user_message():
    messages = [
        _msg("user", "text", "帮我设计记账工具", seq=1),
        _msg("assistant", "text", "方案如下", seq=2),
        _msg("user", "text", "把主色改成蓝色", seq=3),
    ]
    result = build_dialogue_trace(messages, exclude_texts=("帮我设计记账工具", "把主色改成蓝色"))
    assert "方案如下" in result
    assert "帮我设计记账工具" not in result
    assert "把主色改成蓝色" not in result
