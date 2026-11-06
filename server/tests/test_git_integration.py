"""Git 集成测试（主规格 §17.6；实施计划 ⑨）。

没有 mock git：每个用例都在临时目录里建**真实的 Git 仓库**，跑真的 `git` 子进程，
验证 status / diff / branches / checkout / commit 与策略层的状态把关。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from flux.container import Container
from flux.core.git_integration.client import (
    GitClient,
    parse_branches,
    parse_status,
)
from flux.core.git_integration.service import GitService
from flux.enums import VirtualChangeStatus
from flux.errors import GitError, InvalidTransitionError, NotAGitRepositoryError, ValidationError

GIT_ENV = {
    # 让临时仓库不依赖开发机的 git 身份配置
    "GIT_AUTHOR_NAME": "Flux Test",
    "GIT_AUTHOR_EMAIL": "flux-test@example.com",
    "GIT_COMMITTER_NAME": "Flux Test",
    "GIT_COMMITTER_EMAIL": "flux-test@example.com",
}


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **GIT_ENV},
    )
    return completed.stdout


def _repo(path: Path) -> Path:
    """建一个已有一次提交的真实仓库。

    身份写进**仓库本地配置**：产品代码提交时 Flux 不注入 git 身份，
    只依赖开发机全局配置的话，CI runner（无全局身份、新版 git 亦不自动推导）会直接失败。
    """
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Flux Test")
    _git(path, "config", "user.email", "flux-test@example.com")
    (path / "README.md").write_text("# demo\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "chore: 初始化仓库")
    return path


# --- 纯解析（不依赖 git，覆盖 git 各种输出的形态）---


def test_parse_status_reads_branch_and_files() -> None:
    status = parse_status(
        "## main...origin/main [ahead 1]\n"
        " M app/main.py\n"
        "A  app/new.py\n"
        "?? scratch.txt\n"
        "R  old/name.py -> new/name.py\n"
    )

    assert status.branch == "main"
    assert status.detached is False
    assert status.clean is False
    assert [f.path for f in status.files] == [
        "app/main.py",
        "app/new.py",
        "scratch.txt",
        "new/name.py",
    ]
    by_path = {f.path: f for f in status.files}
    assert by_path["app/main.py"].staged is False
    assert by_path["app/main.py"].worktree_status == "M"
    assert by_path["app/new.py"].staged is True
    assert by_path["scratch.txt"].untracked is True
    assert by_path["new/name.py"].original_path == "old/name.py"


def test_parse_status_handles_detached_and_unborn_branch() -> None:
    detached = parse_status("## HEAD (no branch)\n")
    assert detached.branch is None and detached.detached is True

    unborn = parse_status("## No commits yet on main\n?? a.txt\n")
    assert unborn.branch == "main" and unborn.clean is False

    assert parse_status("## main\n").clean is True


def test_parse_branches_marks_current_and_skips_detached_head() -> None:
    branches = parse_branches("* main\n  dev\n")
    assert branches.current == "main"
    assert branches.locals == ("main", "dev")
    assert branches.detached is False

    detached = parse_branches("* (HEAD detached at 1a2b3c4)\n  main\n")
    assert detached.detached is True
    assert detached.current is None
    assert detached.locals == ("main",)


# --- 真实仓库上的只读操作 ---


def test_status_on_real_repository(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    (repo / "README.md").write_text("# demo\n改了一行\n", encoding="utf-8")
    (repo / "fresh.txt").write_text("new\n", encoding="utf-8")

    status = GitClient(workspace_root=repo).status()

    assert status.branch == "main"
    assert status.clean is False
    assert {f.path for f in status.files} == {"README.md", "fresh.txt"}


def test_status_lists_untracked_files_in_new_directories(tmp_path: Path) -> None:
    """新增目录里的未跟踪文件必须逐个列出，而不是折叠成 `docs/`。

    前端 Git 面板用落盘变更的完整文件路径去匹配 git 状态里的路径；若状态只给目录，
    已落盘的新文件就无法被识别为“可提交”，提交按钮会被错误禁用。
    """
    repo = _repo(tmp_path / "repo")
    (repo / "docs").mkdir()
    (repo / "docs" / "note.md").write_text("# note\n", encoding="utf-8")

    status = GitClient(workspace_root=repo).status()

    assert status.clean is False
    assert {f.path for f in status.files} == {"docs/note.md"}


def test_status_on_clean_repository(tmp_path: Path) -> None:
    status = GitClient(workspace_root=_repo(tmp_path / "repo")).status()
    assert status.clean is True and status.branch == "main"


def test_diff_returns_unified_text_and_respects_paths(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    (repo / "b.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "a.txt", "b.txt")
    _git(repo, "commit", "-m", "chore: 加两个文件")
    (repo / "a.txt").write_text("one-changed\n", encoding="utf-8")
    (repo / "b.txt").write_text("two-changed\n", encoding="utf-8")

    client = GitClient(workspace_root=repo)
    everything = client.diff()
    assert "a.txt" in everything.text and "b.txt" in everything.text
    assert everything.staged is False
    assert everything.empty is False

    only_a = client.diff(["a.txt"])
    assert only_a.paths == ("a.txt",)
    assert "a.txt" in only_a.text and "b.txt" not in only_a.text

    _git(repo, "add", "a.txt")
    staged = client.diff(staged=True)
    assert staged.staged is True
    assert "a.txt" in staged.text and "b.txt" not in staged.text


def test_branches_and_checkout_create_then_switch(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    client = GitClient(workspace_root=repo)

    assert client.branches().locals == ("main",)

    created = client.checkout("feature/git-integration", create=True)
    assert created.current == "feature/git-integration"
    assert sorted(created.locals) == ["feature/git-integration", "main"]

    switched = client.checkout("main")
    assert switched.current == "main"


# --- 提交 ---


def test_commit_with_paths_only_commits_those_files(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    (repo / "kept.txt").write_text("只提交我\n", encoding="utf-8")
    (repo / "other.txt").write_text("先不提交\n", encoding="utf-8")

    commit = GitClient(workspace_root=repo).commit("feat: 新增 kept.txt", paths=["kept.txt"])

    assert commit.message == "feat: 新增 kept.txt"
    assert commit.files == ("kept.txt",)
    assert len(commit.sha) == 40 and commit.short_sha == commit.sha[:7]
    # 未指定的文件留在工作区，没有被顺手提交
    assert GitClient(workspace_root=repo).status().files[0].path == "other.txt"


def test_commit_rejects_empty_message_and_escaping_paths(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    client = GitClient(workspace_root=repo)
    (repo / "a.txt").write_text("x\n", encoding="utf-8")

    with pytest.raises(ValidationError) as empty:
        client.commit("   ")
    assert "提交信息不能为空" in empty.value.message

    for bad in ("/etc/passwd", "../outside.txt"):
        with pytest.raises(ValidationError):
            client.commit("feat: x", paths=[bad])


def test_commit_without_changes_fails_closed(tmp_path: Path) -> None:
    """暂存区为空时不静默成功——git 的原话原样抛出。"""
    repo = _repo(tmp_path / "repo")

    with pytest.raises(GitError) as excinfo:
        GitClient(workspace_root=repo).commit("chore: 什么都没有")

    assert excinfo.value.code == "git_failed"
    assert excinfo.value.details["exit_code"] != 0


# --- 环境与配置错误 ---


def test_not_a_repository_has_its_own_error_code(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(NotAGitRepositoryError) as excinfo:
        GitClient(workspace_root=plain).status()

    assert excinfo.value.code == "not_a_git_repository"
    assert excinfo.value.http_status == 409


def test_unconfigured_or_missing_workspace_root_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as unset:
        GitClient().status()
    assert "未配置工作区根目录" in unset.value.message

    with pytest.raises(ValidationError) as missing:
        GitClient(workspace_root=tmp_path / "nope").status()
    assert "不存在或不是目录" in missing.value.message


def test_checkout_rejects_blank_and_option_like_branch_names(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    client = GitClient(workspace_root=repo)

    for bad in ("", "   ", "--force", "-D"):
        with pytest.raises(ValidationError):
            client.checkout(bad)


# --- 策略层：只有 applied 的改动才允许提交 ---


async def _applied_change(container: Container) -> str:
    """造一条已 applied 的提案（直接写库，避开 Apply 的真实落盘）。"""
    change = await container.proposal_repo.create(
        file_path="app/main.py",
        original_content="v1\n",
        proposed_content="v2\n",
        original_hash="hash",
        diff="",
        added_lines=1,
        removed_lines=1,
        hunks=1,
        status=VirtualChangeStatus.APPLIED,
    )
    return str(change.id)


async def test_service_commits_only_applied_changes(
    apply_container: Container, workspace_root: Path
) -> None:
    _repo(workspace_root)
    (workspace_root / "app").mkdir(exist_ok=True)
    (workspace_root / "app" / "main.py").write_text("v2\n", encoding="utf-8")
    change_id = await _applied_change(apply_container)

    commit = await apply_container.git.commit("feat: 落盘后的改动", change_ids=[change_id])

    assert commit.files == ("app/main.py",)
    events = [event for event, _ in apply_container.bus.history]
    assert "git.committed" in events


async def test_service_rejects_pending_change(apply_container: Container) -> None:
    pending = await apply_container.proposal_repo.create(
        file_path="app/main.py",
        original_content="v1\n",
        proposed_content="v2\n",
        original_hash="hash",
        diff="",
        added_lines=1,
        removed_lines=1,
        hunks=1,
        status=VirtualChangeStatus.PENDING,
    )

    with pytest.raises(InvalidTransitionError) as excinfo:
        await apply_container.git.commit("feat: 还没批准", change_ids=[str(pending.id)])

    assert "只有 applied" in excinfo.value.message
    assert excinfo.value.details["status"] == "pending"


async def test_service_requires_workspace_service_for_change_ids() -> None:
    service = GitService(GitClient(), workspace=None)

    with pytest.raises(ValidationError) as excinfo:
        await service.commit("feat: x", change_ids=["0f0f0f0f-0000-0000-0000-000000000000"])

    assert "未接入 Virtual Workspace" in excinfo.value.message


def test_stage_and_unstage(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("hello\n", encoding="utf-8")
    git = GitClient(workspace_root=tmp_path)
    git._run("init", "-q", root=tmp_path)  # noqa: SLF001
    git._run("config", "user.email", "flux@example.com", root=tmp_path)  # noqa: SLF001
    git._run("config", "user.name", "Flux Test", root=tmp_path)  # noqa: SLF001
    git._run("add", "--", "file.txt", root=tmp_path)  # noqa: SLF001
    git._run("commit", "-m", "initial", root=tmp_path)  # noqa: SLF001
    (tmp_path / "file.txt").write_text("changed\n", encoding="utf-8")

    staged = git.stage(["file.txt"])
    assert staged.files[0].staged is True

    unstaged = git.unstage(["file.txt"])
    assert unstaged.files[0].staged is False
