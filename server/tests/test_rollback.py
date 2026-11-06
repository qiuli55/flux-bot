"""正式 Rollback 测试（P1-2 设计 §5.3：T-ROLL-003~009）。

把 `.flux/backups/` 提升为正式能力：单条 / 整批回滚、前置校验、幂等与失败边界，
并保证回滚只恢复 Workspace、不产生任何 git 提交或错误记录。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flux.container import Container
from flux.core.event.bus import Events
from flux.core.virtual_workspace.proposal_parser import CodeChangeSet, FileChange
from flux.enums import VirtualChangeStatus
from flux.errors import ConflictError
from flux.main import create_app
from tests.conftest import python_command

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


def _container(apply_settings, test_command: str | None = None) -> Container:
    updates = {"test_command": test_command or _passing_command()}
    return Container(apply_settings.model_copy(update=updates))


def _apply_one(container: Container, path: str, original: str, proposed: str):
    change_set = CodeChangeSet(
        summary="修改", changes=(FileChange(path=path, content=proposed, op="modify"),)
    )
    change = asyncio.run(
        container.workspace.propose_changes(change_set, original_files={path: original})
    )[0]
    applied = asyncio.run(container.workspace.apply(str(change.id)))
    batch = asyncio.run(container.batch_repo.find_for_change(str(change.id)))
    return applied, batch


# --- T-ROLL-003：单条回滚 ---


def test_single_change_rollback(apply_settings, db_schema: None, workspace_root: Path) -> None:
    target = _write(workspace_root, "auth/login.py", ORIGINAL)
    container = _container(apply_settings)
    applied, batch = _apply_one(container, "auth/login.py", ORIGINAL, PROPOSED)
    assert target.read_text(encoding="utf-8") == PROPOSED

    rolled = asyncio.run(container.workspace.rollback_change(str(applied.id)))

    assert rolled.status == VirtualChangeStatus.ROLLED_BACK.value
    assert target.read_text(encoding="utf-8") == ORIGINAL
    assert asyncio.run(container.batch_repo.get(str(batch.id))).status == "rolled_back"
    assert any(name == Events.APPLY_ROLLED_BACK for name, _ in container.bus.history)


# --- T-ROLL-004：整批回滚最近一次 Apply ---


def test_batch_rollback_restores_all(apply_settings, db_schema: None, workspace_root: Path) -> None:
    _write(workspace_root, "a.py", ORIGINAL)
    _write(workspace_root, "b.py", ORIGINAL)
    container = _container(apply_settings)
    change_set = CodeChangeSet(
        summary="整批",
        changes=(
            FileChange(path="a.py", content=PROPOSED, op="modify"),
            FileChange(path="b.py", content=PROPOSED, op="modify"),
        ),
    )
    changes = asyncio.run(
        container.workspace.propose_changes(
            change_set, original_files={"a.py": ORIGINAL, "b.py": ORIGINAL}
        )
    )
    asyncio.run(container.workspace.apply_many([str(c.id) for c in changes]))

    recent = asyncio.run(container.workspace.list_recent_batches(recent=True, limit=1))
    assert len(recent) == 1
    batch = asyncio.run(container.workspace.rollback_batch(str(recent[0].id)))

    assert batch.status == "rolled_back"
    assert batch.finished_at is not None
    for path in ("a.py", "b.py"):
        assert (workspace_root / path).read_text(encoding="utf-8") == ORIGINAL
    for change in changes:
        assert (
            asyncio.run(container.workspace.get(str(change.id))).status
            == VirtualChangeStatus.ROLLED_BACK.value
        )


# --- T-ROLL-005：create / modify / delete 混合批回滚 ---


def test_mixed_batch_rollback(apply_settings, db_schema: None, workspace_root: Path) -> None:
    _write(workspace_root, "app/mod.py", ORIGINAL)
    _write(workspace_root, "app/gone.py", LEGACY)
    container = _container(apply_settings)
    change_set = CodeChangeSet(
        summary="混合",
        changes=(
            FileChange(path="app/new.py", content=NEW_FILE, op="create"),
            FileChange(path="app/mod.py", content=PROPOSED, op="modify"),
            FileChange(path="app/gone.py", content=None, op="delete"),
        ),
    )
    changes = asyncio.run(
        container.workspace.propose_changes(
            change_set,
            original_files={"app/new.py": None, "app/mod.py": ORIGINAL, "app/gone.py": LEGACY},
        )
    )
    asyncio.run(container.workspace.apply_many([str(c.id) for c in changes]))
    assert not (workspace_root / "app" / "gone.py").exists()

    recent = asyncio.run(container.workspace.list_recent_batches(recent=True, limit=1))
    asyncio.run(container.workspace.rollback_batch(str(recent[0].id)))

    assert not (workspace_root / "app" / "new.py").exists()
    assert (workspace_root / "app" / "mod.py").read_text(encoding="utf-8") == ORIGINAL
    assert (workspace_root / "app" / "gone.py").read_text(encoding="utf-8") == LEGACY


# --- T-ROLL-006：二次回滚 ---


def test_second_batch_rollback_is_conflict(
    apply_settings, db_schema: None, workspace_root: Path
) -> None:
    _write(workspace_root, "auth/login.py", ORIGINAL)
    container = _container(apply_settings)
    _, batch = _apply_one(container, "auth/login.py", ORIGINAL, PROPOSED)
    asyncio.run(container.workspace.rollback_batch(str(batch.id)))

    with pytest.raises(ConflictError):
        asyncio.run(container.workspace.rollback_batch(str(batch.id)))

    assert (workspace_root / "auth" / "login.py").read_text(encoding="utf-8") == ORIGINAL


# --- T-ROLL-007：回滚前文件被外部改动 ---


def test_rollback_refuses_when_file_changed(
    apply_settings, db_schema: None, workspace_root: Path
) -> None:
    target = _write(workspace_root, "auth/login.py", ORIGINAL)
    container = _container(apply_settings)
    applied, _ = _apply_one(container, "auth/login.py", ORIGINAL, PROPOSED)
    # apply 之后用户又改了文件 → 回滚必须拒绝，绝不覆盖
    target.write_text("用户后来改的\n", encoding="utf-8")

    with pytest.raises(ConflictError):
        asyncio.run(container.workspace.rollback_change(str(applied.id)))

    assert target.read_text(encoding="utf-8") == "用户后来改的\n"
    assert (
        asyncio.run(container.workspace.get(str(applied.id))).status
        == VirtualChangeStatus.APPLIED.value
    )


# --- T-ROLL-008：回滚后 git 状态 ---


def test_rollback_leaves_no_git_trace(
    apply_settings, db_schema: None, workspace_root: Path
) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Flux Test",
        "GIT_AUTHOR_EMAIL": "flux-test@example.com",
        "GIT_COMMITTER_NAME": "Flux Test",
        "GIT_COMMITTER_EMAIL": "flux-test@example.com",
    }

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=workspace_root, check=True, capture_output=True, text=True, env=env
        ).stdout

    git("init", "-b", "main")
    _write(workspace_root, "auth/login.py", ORIGINAL)
    git("add", "auth/login.py")
    git("commit", "-m", "chore: 初始化")
    head_before = git("rev-parse", "HEAD")

    container = _container(apply_settings)
    applied, _ = _apply_one(container, "auth/login.py", ORIGINAL, PROPOSED)
    asyncio.run(container.workspace.rollback_change(str(applied.id)))

    # 回滚只恢复工作区，不产生任何提交或回执
    assert git("rev-parse", "HEAD") == head_before
    assert git("status", "--porcelain").strip() == ""


# --- REST 入口 ---


def test_rollback_rest_endpoints(apply_settings, db_schema: None, workspace_root: Path) -> None:
    _write(workspace_root, "auth/login.py", ORIGINAL)
    container = _container(apply_settings)
    applied, batch = _apply_one(container, "auth/login.py", ORIGINAL, PROPOSED)
    asyncio.run(container.dispose())

    app = create_app(apply_settings)
    with TestClient(app) as client:
        recent = client.get("/api/v1/workspace/apply-batches?recent=1").json()
        assert recent["success"] is True
        assert recent["metadata"]["count"] == 1
        assert recent["data"][0]["id"] == str(batch.id)

        rolled = client.post(f"/api/v1/workspace/changes/{applied.id}/rollback").json()
        assert rolled["success"] is True
        assert rolled["data"]["status"] == VirtualChangeStatus.ROLLED_BACK.value

    assert (workspace_root / "auth" / "login.py").read_text(encoding="utf-8") == ORIGINAL


# --- T-ROLL-009：回滚后可再次 accept/apply 同一文件 ---


def test_reapply_after_rollback(apply_settings, db_schema: None, workspace_root: Path) -> None:
    target = _write(workspace_root, "auth/login.py", ORIGINAL)
    container = _container(apply_settings)
    applied, _ = _apply_one(container, "auth/login.py", ORIGINAL, PROPOSED)
    asyncio.run(container.workspace.rollback_change(str(applied.id)))
    assert target.read_text(encoding="utf-8") == ORIGINAL

    # 回滚后重新提交同一文件的提案，闭环正常
    change_set = CodeChangeSet(
        summary="再来一次",
        changes=(FileChange(path="auth/login.py", content=PROPOSED, op="modify"),),
    )
    again = asyncio.run(
        container.workspace.propose_changes(change_set, original_files={"auth/login.py": ORIGINAL})
    )[0]
    asyncio.run(container.workspace.accept(str(again.id)))
    reapplied = asyncio.run(container.workspace.apply(str(again.id)))

    assert reapplied.status == VirtualChangeStatus.APPLIED.value
    assert target.read_text(encoding="utf-8") == PROPOSED
