"""Delete Proposal 测试（P1-1 设计 §4.3：T-DEL-001~006）。

DELETE 与 CREATE / MODIFY 走同一条 Proposal → Diff → Review → Apply 全流程；
这里在真实临时目录上验证删除提案的 diff 概览、落盘与失败回滚。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from flux.container import Container
from flux.core.virtual_workspace.proposal_parser import (
    CodeChangeSet,
    FileChange,
    parse_code_change_set,
)
from flux.enums import ChangeKind, VirtualChangeStatus
from flux.errors import ApplyFailedError, ConflictError, ValidationError
from tests.conftest import python_command, python_script

ORIGINAL = "def login(user):\n    return False\n"
PROPOSED = "def login(user):\n    return check_password(user)\n"
LEGACY = "旧模块内容\n第二行\n"
NEW_FILE = "# 新模块\n"


def _write(root: Path, relative: str, content: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _passing_command() -> str:
    # 跨平台：Windows 的 cmd.exe 不认 shlex.quote 生成的单引号路径
    return python_command("print('ok')")


def _propose_delete(container: Container, path: str, original: str):
    change_set = CodeChangeSet(
        summary="删除废弃模块",
        changes=(FileChange(path=path, content=None, reason="已被新模块替代", op="delete"),),
    )
    return asyncio.run(
        container.workspace.propose_changes(
            change_set, agent_source="developer-agent", original_files={path: original}
        )
    )


# --- 解析器：op 语义 ---


def test_parser_accepts_delete_without_content() -> None:
    raw = json.dumps(
        {"summary": "删除废弃模块", "changes": [{"path": "src/legacy.py", "op": "delete"}]}
    )
    change = parse_code_change_set(raw).changes[0]
    assert change.op == "delete"
    assert change.content is None


def test_parser_rejects_delete_with_content() -> None:
    raw = json.dumps(
        {
            "summary": "x",
            "changes": [{"path": "src/legacy.py", "op": "delete", "content": "x"}],
        }
    )
    with pytest.raises(ValidationError) as excinfo:
        parse_code_change_set(raw)
    assert "禁止携带 content" in excinfo.value.message


def test_parser_rejects_unknown_op() -> None:
    raw = json.dumps(
        {"summary": "x", "changes": [{"path": "a.py", "op": "rename", "content": "x"}]}
    )
    with pytest.raises(ValidationError) as excinfo:
        parse_code_change_set(raw)
    assert ".op 必须是" in excinfo.value.message


def test_parser_rejects_git_internal_paths() -> None:
    raw = json.dumps({"summary": "x", "changes": [{"path": ".git/config", "content": "x"}]})
    with pytest.raises(ValidationError) as excinfo:
        parse_code_change_set(raw)
    assert "Git 元数据目录" in excinfo.value.message


# --- T-DEL-001：提交删除提案（diff / 行数 / 文件未动）---


def test_delete_proposal_diff_and_stats(apply_container: Container, workspace_root: Path) -> None:
    target = _write(workspace_root, "src/legacy.py", LEGACY)

    created = _propose_delete(apply_container, "src/legacy.py", LEGACY)

    assert len(created) == 1
    change = created[0]
    assert change.kind == ChangeKind.DELETE.value
    assert change.proposed_content is None
    assert change.added_lines == 0
    assert change.removed_lines == 2
    assert change.hunks == 1
    assert change.diff and "+++ /dev/null" in change.diff
    # 提交提案不碰磁盘
    assert target.read_text(encoding="utf-8") == LEGACY


# --- T-DEL-002：accept → apply 后文件消失 ---


def test_delete_proposal_applies(apply_settings, db_schema: None, workspace_root: Path) -> None:
    target = _write(workspace_root, "src/legacy.py", LEGACY)
    settings = apply_settings.model_copy(update={"test_command": _passing_command()})
    container = Container(settings)
    change = _propose_delete(container, "src/legacy.py", LEGACY)[0]

    applied = asyncio.run(container.workspace.apply(str(change.id)))

    assert applied.status == VirtualChangeStatus.APPLIED.value
    assert not target.exists()
    assert applied.backup_path and Path(applied.backup_path).is_file()
    batches = asyncio.run(container.batch_repo.list_recent())
    assert batches[0].status == "applied"


def test_delete_proposal_rollback_restores_file(
    apply_settings, db_schema: None, workspace_root: Path
) -> None:
    """T-DEL-003（依赖 §5）：删除后回滚，文件内容恢复。"""
    target = _write(workspace_root, "src/legacy.py", LEGACY)
    container = Container(apply_settings.model_copy(update={"test_command": _passing_command()}))
    change = _propose_delete(container, "src/legacy.py", LEGACY)[0]
    asyncio.run(container.workspace.apply(str(change.id)))
    assert not target.exists()

    rolled = asyncio.run(container.workspace.rollback_change(str(change.id)))

    assert rolled.status == VirtualChangeStatus.ROLLED_BACK.value
    assert target.read_text(encoding="utf-8") == LEGACY


# --- T-DEL-004：删除不存在的文件 ---


def test_delete_missing_target_is_rejected(container: Container) -> None:
    change_set = CodeChangeSet(
        summary="删除不存在的文件",
        changes=(FileChange(path="nope.py", content=None, op="delete"),),
    )
    with pytest.raises(ValidationError) as excinfo:
        asyncio.run(
            container.workspace.propose_changes(change_set, original_files={"nope.py": None})
        )
    assert "删除目标不存在" in excinfo.value.message


# --- T-DEL-005：删除前文件被外部修改 ---


def test_delete_conflicts_when_file_changed_externally(
    apply_container: Container, workspace_root: Path
) -> None:
    target = _write(workspace_root, "src/legacy.py", LEGACY)
    change = _propose_delete(apply_container, "src/legacy.py", LEGACY)[0]
    # 提交提案后用户又改了同一文件 → 拒绝删除，文件保持用户版本
    target.write_text("用户手改的内容\n", encoding="utf-8")

    with pytest.raises(ConflictError):
        asyncio.run(apply_container.workspace.apply(str(change.id)))

    assert target.read_text(encoding="utf-8") == "用户手改的内容\n"
    assert (
        asyncio.run(apply_container.workspace.get(str(change.id))).status
        == VirtualChangeStatus.ACCEPTED.value
    )


# --- T-DEL-006：create + modify + delete 混合批 ---


def _mixed_batch(container: Container):
    change_set = CodeChangeSet(
        summary="混合改动",
        changes=(
            FileChange(path="app/new.py", content=NEW_FILE, op="create"),
            FileChange(path="app/mod.py", content=PROPOSED, op="modify"),
            FileChange(path="app/gone.py", content=None, op="delete"),
        ),
    )
    return asyncio.run(
        container.workspace.propose_changes(
            change_set,
            original_files={"app/new.py": None, "app/mod.py": ORIGINAL, "app/gone.py": LEGACY},
        )
    )


def test_mixed_batch_applies_all(apply_settings, db_schema: None, workspace_root: Path) -> None:
    _write(workspace_root, "app/mod.py", ORIGINAL)
    _write(workspace_root, "app/gone.py", LEGACY)
    container = Container(apply_settings.model_copy(update={"test_command": _passing_command()}))
    changes = _mixed_batch(container)

    applied = asyncio.run(container.workspace.apply_many([str(c.id) for c in changes]))

    assert [c.status for c in applied] == ["applied"] * 3
    assert (workspace_root / "app" / "new.py").read_text(encoding="utf-8") == NEW_FILE
    assert (workspace_root / "app" / "mod.py").read_text(encoding="utf-8") == PROPOSED
    assert not (workspace_root / "app" / "gone.py").exists()


def test_mixed_batch_failure_rolls_back_all(
    apply_settings, db_schema: None, workspace_root: Path
) -> None:
    """混合批测试失败 → 全量回滚：新建被删、修改还原、删除恢复。"""
    _write(workspace_root, "app/mod.py", ORIGINAL)
    _write(workspace_root, "app/gone.py", LEGACY)
    failing = workspace_root / "failing_check.py"
    failing.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    settings = apply_settings.model_copy(update={"test_command": python_script(failing)})
    container = Container(settings)
    changes = _mixed_batch(container)

    with pytest.raises(ApplyFailedError):
        asyncio.run(container.workspace.apply_many([str(c.id) for c in changes]))

    assert not (workspace_root / "app" / "new.py").exists()
    assert (workspace_root / "app" / "mod.py").read_text(encoding="utf-8") == ORIGINAL
    assert (workspace_root / "app" / "gone.py").read_text(encoding="utf-8") == LEGACY
