"""Project Brain 测试（主规格 §5.6；实施计划 ⑪）。

全部走真实数据库（`projects` / `project_memory` 两张既有表），不 mock 仓储；
扫描走真实临时目录。重点验证两种写入语义（覆盖 / 追加）与上下文拼装。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from flux.container import Container
from flux.core.event.bus import Events
from flux.enums import BrainSection
from flux.errors import NotFoundError, ValidationError


def _project(container: Container, *, name: str = "Flux 演示项目") -> str:
    project = asyncio.run(container.brain.create_project(name=name, repository="qiuli55/flux"))
    return str(project.id)


def _write(container: Container, project_id: str, section: BrainSection, content: str) -> None:
    asyncio.run(container.brain.write(project_id, section=section, content=content))


def _sections(container: Container, project_id: str) -> dict[str, list[dict]]:
    return asyncio.run(container.brain.sections(project_id))


def _python_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "scan-target"
    (root / "app").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    (root / "requirements.txt").write_text("fastapi\npytest\n", encoding="utf-8")
    (root / "app" / "main.py").write_text("app = None\n", encoding="utf-8")
    return root


# --- 项目登记 ---


def test_create_and_list_projects(container: Container) -> None:
    project_id = _project(container, name="甲项目")
    _project(container, name="乙项目")

    fetched = asyncio.run(container.brain.get_project(project_id))

    assert fetched.name == "甲项目"
    assert fetched.repository == "qiuli55/flux"
    assert len(asyncio.run(container.brain.list_projects())) == 2


def test_create_project_rejects_blank_name(container: Container) -> None:
    with pytest.raises(ValidationError) as excinfo:
        asyncio.run(container.brain.create_project(name="   "))
    assert "项目名称不能为空" in excinfo.value.message


def test_get_unknown_project_raises_not_found(container: Container) -> None:
    with pytest.raises(NotFoundError):
        asyncio.run(container.brain.get_project(str(uuid.uuid4())))
    with pytest.raises(NotFoundError):
        asyncio.run(container.brain.get_project("不是 UUID"))


# --- 两种写入语义 ---


def test_current_state_sections_are_overwritten(container: Container) -> None:
    """现状型分区：重复写入只保留最新一份，读取时不需要猜哪条是当前事实。"""
    project_id = _project(container)
    _write(container, project_id, BrainSection.OVERVIEW, "第一版概述")
    _write(container, project_id, BrainSection.OVERVIEW, "第二版概述")

    sections = _sections(container, project_id)

    assert len(sections["overview"]) == 1
    assert sections["overview"][0]["content"] == "第二版概述"
    assert sections["overview"][0]["section"] == "overview"


def test_accumulating_sections_keep_history(container: Container) -> None:
    """累积型分区（决策 / Agent 记录）：只追加，历史不可被覆盖。"""
    project_id = _project(container)
    _write(container, project_id, BrainSection.DECISIONS, "选 FastAPI 而不是 Flask")
    _write(container, project_id, BrainSection.DECISIONS, "SQLite 起步，PostgreSQL 留到 M5")
    _write(container, project_id, BrainSection.AGENT_NOTES, "测试用例必须走真实数据库")

    sections = _sections(container, project_id)

    assert len(sections["decisions"]) == 2
    assert sorted(entry["content"] for entry in sections["decisions"]) == [
        "SQLite 起步，PostgreSQL 留到 M5",
        "选 FastAPI 而不是 Flask",
    ]
    assert len(sections["agent_notes"]) == 1


def test_every_section_present_even_when_empty(container: Container) -> None:
    sections = _sections(container, _project(container))

    assert set(sections) == {section.value for section in BrainSection}
    assert all(entries == [] for entries in sections.values())


def test_write_rejects_blank_content(container: Container) -> None:
    project_id = _project(container)
    with pytest.raises(ValidationError) as excinfo:
        _write(container, project_id, BrainSection.CODING_RULES, "   ")
    assert "记忆内容不能为空" in excinfo.value.message


def test_write_to_unknown_project_raises_not_found(container: Container) -> None:
    with pytest.raises(NotFoundError):
        _write(container, str(uuid.uuid4()), BrainSection.OVERVIEW, "内容")


def test_write_publishes_brain_updated_event(container: Container) -> None:
    project_id = _project(container)
    container.bus.clear()

    _write(container, project_id, BrainSection.CODING_RULES, "全部公开接口都要有错误码")

    events = [event for event, _ in container.bus.history]
    assert Events.BRAIN_UPDATED in events
    payload = next(body for event, body in container.bus.history if event == Events.BRAIN_UPDATED)
    assert payload["section"] == "coding_rules"
    assert payload["replaced"] is True


# --- 扫描 → Brain（⑩ → ⑪）---


def test_scan_records_overview_and_tech_stack(container: Container, tmp_path: Path) -> None:
    project_id = _project(container)
    root = _python_workspace(tmp_path)
    container.bus.clear()

    outcome = asyncio.run(container.brain.scan(project_id, workspace_root=root))

    assert outcome.profile.primary_language == "Python"
    assert [entry["section"] for entry in outcome.to_dict()["recorded"]] == [
        "overview",
        "tech_stack",
    ]
    sections = _sections(container, project_id)
    assert len(sections["overview"]) == 1
    assert "文件数：3" in sections["overview"][0]["content"]
    assert sections["overview"][0]["metadata"]["source"] == "scanner"
    tech_stack = sections["tech_stack"][0]["content"]
    assert "语言：Python" in tech_stack
    assert "框架：FastAPI" in tech_stack
    assert "包管理器：pip" in tech_stack
    assert "测试命令：pytest" in tech_stack
    events = [event for event, _ in container.bus.history]
    assert Events.PROJECT_SCANNED in events
    assert events.count(Events.BRAIN_UPDATED) == 2


def test_scan_can_skip_recording(container: Container, tmp_path: Path) -> None:
    project_id = _project(container)

    outcome = asyncio.run(
        container.brain.scan(project_id, workspace_root=_python_workspace(tmp_path), record=False)
    )

    assert outcome.profile.primary_language == "Python"
    assert outcome.recorded == ()
    assert all(entries == [] for entries in _sections(container, project_id).values())


def test_scan_overwrites_previous_snapshot(container: Container, tmp_path: Path) -> None:
    """重复扫描不会堆出多份 Overview：现状型分区始终只有一份。"""
    project_id = _project(container)
    root = _python_workspace(tmp_path)

    asyncio.run(container.brain.scan(project_id, workspace_root=root))
    asyncio.run(container.brain.scan(project_id, workspace_root=root))

    sections = _sections(container, project_id)
    assert len(sections["overview"]) == 1
    assert len(sections["tech_stack"]) == 1


def test_scan_requires_workspace_root(container: Container) -> None:
    project_id = _project(container)

    with pytest.raises(ValidationError) as excinfo:
        asyncio.run(container.brain.scan(project_id))

    assert "未配置工作区根目录" in excinfo.value.message


def test_scan_unknown_project_raises_not_found(container: Container, tmp_path: Path) -> None:
    with pytest.raises(NotFoundError):
        asyncio.run(
            container.brain.scan(str(uuid.uuid4()), workspace_root=_python_workspace(tmp_path))
        )


# --- 上下文拼装 ---


def test_context_only_includes_filled_sections(container: Container) -> None:
    project_id = _project(container, name="知识库项目")
    _write(container, project_id, BrainSection.ARCHITECTURE, "三层：api / core / models")
    _write(container, project_id, BrainSection.DECISIONS, "先做结构化记忆，不上向量库")

    text = asyncio.run(container.brain.context(project_id))

    assert text.startswith("# 项目：知识库项目")
    assert "仓库：qiuli55/flux" in text
    assert "## Architecture\n三层：api / core / models" in text
    assert "## Decisions\n- 先做结构化记忆，不上向量库" in text
    # 没有内容的分区不出现，避免给 Agent 塞一堆空标题
    assert "## Tech Stack" not in text
    assert "## Coding Rules" not in text


def test_context_can_be_limited_to_selected_sections(container: Container) -> None:
    project_id = _project(container)
    _write(container, project_id, BrainSection.CODING_RULES, "禁止裸 except")
    _write(container, project_id, BrainSection.AGENT_NOTES, "临时记录")

    text = asyncio.run(container.brain.context(project_id, sections=[BrainSection.CODING_RULES]))

    assert "## Coding Rules" in text
    assert "## Agent Notes" not in text


def test_context_caps_accumulated_entries(container: Container) -> None:
    project_id = _project(container)
    for index in range(5):
        _write(container, project_id, BrainSection.AGENT_NOTES, f"第 {index} 条")

    text = asyncio.run(container.brain.context(project_id, max_entries=2))

    assert text.count("- 第") == 2
