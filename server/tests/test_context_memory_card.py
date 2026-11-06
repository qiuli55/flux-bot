"""工作记忆卡：生成内容、幂等与最新读取。"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from flux.core.task_engine.memory_repository import TaskMemoryRepository
from flux.core.task_engine.work_memory import MEMORY_CARD_CHARS, build_memory_card


def _task(confirmation: dict | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        description="帮我设计记账工具",
        confirmation=confirmation,
    )


def _run(**extra):
    base = {
        "id": "run-1",
        "instruction": "## 本轮任务\n把主色改成蓝色",
        "status": "failed",
        "finish_reason": "error",
        "error": "provider quota exhausted",
    }
    base.update(extra)
    return SimpleNamespace(**base)


def test_card_contains_goal_files_error_and_next_step():
    card = build_memory_card(
        task=_task({"items": [{"label": "风格", "value": "极简"}]}),
        run=_run(),
        instruction="你是执行 Agent。\n\n## 本轮任务\n把主色改成蓝色\n\n## 执行计划\n- 改 token",
        final_response="还差导出功能没做",
        status="failed",
        trace_rows=[
            {"category": "write", "paths": ["app/page.tsx"]},
            {"category": "command", "paths": []},
            {"category": "read", "paths": ["app/layout.tsx"]},
        ],
    )
    assert "把主色改成蓝色" in card
    assert "app/page.tsx" in card
    assert "provider quota exhausted" in card
    assert "风格：极简" in card
    assert "还差导出功能没做" in card
    assert "不要重做已完成的步骤" in card
    assert len(card) <= MEMORY_CARD_CHARS


def test_card_goal_skips_constraints_in_initial_instruction():
    instruction = (
        "## 执行约束\n- 只通过 Flux MCP 提交提案\n- 高风险命令先审批\n\n"
        "帮我设计记账工具\n\n"
        "## 平台身份（调用 Flux MCP 时带上）\n- task_id：abc"
    )
    card = build_memory_card(task=_task(), run=_run(), instruction=instruction, status="completed")
    assert "本轮目标：帮我设计记账工具" in card
    assert "本轮目标：## 执行约束" not in card


def test_card_redacts_error_secret():
    card = build_memory_card(
        task=_task(),
        run=_run(error="401 invalid key sk-abcdefghijklmnop"),
        status="failed",
    )
    assert "sk-abcdefghijklmnop" not in card


def test_card_is_capped_even_with_huge_response():
    card = build_memory_card(
        task=_task(),
        run=_run(),
        final_response="很长的答复" * 500,
        status="completed",
    )
    assert len(card) <= MEMORY_CARD_CHARS


def test_repository_add_is_idempotent_and_latest_wins(container):
    repo: TaskMemoryRepository = container.task_memory
    task_id = uuid.uuid4()

    first = asyncio.run(repo.add(task_id, "run-1", "第一张"))
    again = asyncio.run(repo.add(task_id, "run-1", "重复写入"))
    assert first.id == again.id
    assert again.content == "第一张"
    assert asyncio.run(repo.exists(task_id, "run-1")) is True

    asyncio.run(repo.add(task_id, "run-2", "第二张"))
    latest = asyncio.run(repo.latest(task_id))
    assert latest is not None and latest.content == "第二张"
    assert len(asyncio.run(repo.list_recent(task_id, limit=5))) == 2
