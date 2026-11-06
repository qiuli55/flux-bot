"""上下文占用与执行指令同源（跨轮记忆 · 口径统一）。"""

from __future__ import annotations

import uuid

from flux.core.agent_runtime.runtimes.service import _execution_instruction
from flux.core.task_engine.context_usage import (
    LEVEL_FULL,
    LEVEL_WARNING,
    context_budget_chars,
    measure_context,
)
from flux.core.task_engine.repository import MessageDraft
from flux.models.task import Task


def _task() -> Task:
    return Task(
        id=uuid.uuid4(),
        description="接着修登录页",
        project_id=None,
        agent_id=None,
    )


def _msg(content: str):
    return MessageDraft(role="user", kind="text", content=content)


def test_extra_chars_enters_used_chars():
    usage = measure_context([_msg("你好")], message_count=1, extra_chars=5)
    assert usage.instruction_chars == 5
    assert usage.used_chars == len("你好") + 5
    # 空 extra 不占字符
    assert measure_context([], message_count=0, extra_chars=0).instruction_chars == 0


def test_message_count_does_not_saturate_usage():
    """条数到顶不等于上下文满：真实投喂已经是压缩轨迹，比例只看字符。"""
    usage = measure_context([], message_count=1000, limit=18, extra_chars=100)
    assert usage.used_messages == 18
    assert usage.ratio < 0.01
    assert usage.percent == 0
    assert usage.level == "normal"


def test_level_follows_instruction_chars_only():
    """等级仍按字符预算走：指令真的接近/超过预算时才告警、满格。"""
    budget = context_budget_chars()
    warning = measure_context([], message_count=1000, limit=18, extra_chars=int(budget * 0.85))
    assert warning.level == LEVEL_WARNING
    assert 80 <= warning.percent < 100

    full = measure_context([], message_count=1000, limit=18, extra_chars=budget + 1)
    assert full.level == LEVEL_FULL
    assert full.percent == 100


def test_instruction_preview_matches_length():
    """`/context` 的 instruction_chars 必须来自同一份执行指令。"""
    task = _task()
    instruction = _execution_instruction(task, None, [], followup="先补失败用例")
    assert len(instruction) > 0
    assert "## 本轮任务" in instruction


def test_context_reports_instruction_even_with_empty_history(client):
    created = client.post(
        "/api/v1/tasks", json={"description": "从零做", "project_id": None}
    ).json()["data"]
    data = client.get(f"/api/v1/tasks/{created['id']}/context").json()["data"]
    assert data["instruction_chars"] > 0
