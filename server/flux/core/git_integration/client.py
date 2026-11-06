"""Git 客户端（主规格 §17.6；实施计划 ⑨）。

第一版只做五件事：`status` / `diff` / `branch` / `checkout` / `commit`。

四条硬约束：

1. **不用 shell**：一律 `subprocess.run(["git", ...])` 传参数列表，不做字符串拼接，
   文件路径再经 `safe_relative_path` 校验，杜绝命令注入与越出工作区根。
2. **只在工作区根下执行**：与 Apply Engine、Tester 同一个根（§7.6），未配置即拒绝。
3. **只做本地、可回退的操作**：不 push、不 force、不 reset —— 提交权与合并权始终在用户手里。
4. **fail-closed**：git 非零退出 / 超时一律抛 `GitError`，错误详情照搬 git 的原话，
   不猜结果、不吞错。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flux.core.virtual_workspace.apply_engine import resolve_workspace_root, safe_relative_path
from flux.errors import GitError, NotAGitRepositoryError, ValidationError
from flux.logging import get_logger

logger = get_logger(__name__)

# porcelain v1 的默认输出会把非 ASCII 路径转义成 \xxx，中文文件名会变成乱码；
# 全局关掉 quotepath，让路径以原样返回（人工审查要看得懂是哪个文件）。
_GIT_GLOBAL_ARGS = ("-c", "core.quotepath=false")


@dataclass(frozen=True)
class GitFileStatus:
    """`git status --porcelain` 里的一行：XY 两个状态位 + 路径。"""

    path: str
    # 暂存区状态位：M/A/D/R/C/U/?
    index_status: str
    # 工作区状态位
    worktree_status: str
    # 重命名/复制时记录原路径
    original_path: str | None = None

    @property
    def staged(self) -> bool:
        """暂存区里有改动（`git commit` 会带上的部分）。"""
        return self.index_status not in (" ", "?")

    @property
    def untracked(self) -> bool:
        return self.index_status == "?" and self.worktree_status == "?"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "index_status": self.index_status,
            "worktree_status": self.worktree_status,
            "original_path": self.original_path,
            "staged": self.staged,
            "untracked": self.untracked,
        }


@dataclass(frozen=True)
class GitStatus:
    branch: str | None
    detached: bool
    files: tuple[GitFileStatus, ...]

    @property
    def clean(self) -> bool:
        return not self.files

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "detached": self.detached,
            "clean": self.clean,
            "files": [f.to_dict() for f in self.files],
        }


@dataclass(frozen=True)
class GitBranches:
    """本地分支一览。detached 时 current 为 None（HEAD 不在任何分支上）。"""

    current: str | None
    detached: bool
    locals: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"current": self.current, "detached": self.detached, "locals": list(self.locals)}


@dataclass(frozen=True)
class GitDiff:
    staged: bool
    paths: tuple[str, ...]
    text: str

    @property
    def empty(self) -> bool:
        return not self.text.strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "staged": self.staged,
            "paths": list(self.paths),
            "empty": self.empty,
            "text": self.text,
        }


@dataclass(frozen=True)
class GitCommit:
    sha: str
    short_sha: str
    message: str
    files: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha": self.sha,
            "short_sha": self.short_sha,
            "message": self.message,
            "files": list(self.files),
        }


def parse_status(output: str) -> GitStatus:
    """解析 `git status --porcelain=v1 --branch` 的输出。"""
    branch: str | None = None
    detached = False
    files: list[GitFileStatus] = []
    for line in output.splitlines():
        if line.startswith("## "):
            head = line[3:].strip()
            if head.startswith("HEAD (no branch)"):
                detached = True
            elif head.startswith("No commits yet on "):
                # 新建仓库还没有第一次提交，分支名仍然有效
                branch = head[len("No commits yet on ") :].split("...")[0].strip() or None
            else:
                # `main...origin/main [ahead 1]` → 取本地分支名
                branch = head.split("...")[0].split(" ")[0].strip() or None
            continue
        if len(line) < 4:
            continue
        index_status, worktree_status, raw_path = line[0], line[1], line[3:]
        original_path: str | None = None
        path = raw_path.strip()
        if " -> " in path:
            original_path, path = (part.strip() for part in path.split(" -> ", 1))
        files.append(
            GitFileStatus(
                path=path.strip('"'),
                index_status=index_status,
                worktree_status=worktree_status,
                original_path=original_path.strip('"') if original_path else None,
            )
        )
    return GitStatus(branch=branch, detached=detached, files=tuple(files))


def parse_branches(output: str) -> GitBranches:
    """解析 `git branch --format=%(HEAD) %(refname:short)` 的输出。

    detached HEAD 时 git 会列出 `(HEAD detached at <sha>)`，它不是分支，从 locals 里剔除。
    """
    names: list[str] = []
    current: str | None = None
    detached = False
    for line in output.splitlines():
        if not line.strip():
            continue
        marker, name = line[:1], line[1:].strip()
        if name.startswith("(HEAD detached"):
            detached = marker == "*"
            continue
        if marker == "*":
            current = name
        names.append(name)
    return GitBranches(current=current, detached=detached, locals=tuple(names))


class GitClient:
    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._configured_root = Path(workspace_root).expanduser() if workspace_root else None
        self._timeout = timeout_seconds

    @property
    def workspace_root(self) -> Path | None:
        """当前 Git 操作根（桌面端切换工作目录时由 Container 赋值）。"""
        return self._configured_root

    @workspace_root.setter
    def workspace_root(self, value: str | Path | None) -> None:
        self._configured_root = Path(value).expanduser() if value else None

    # --- 只读操作 ---

    def status(self, *, workspace_root: str | Path | None = None) -> GitStatus:
        root = self._root(workspace_root)
        # --untracked-files=all：默认 git 会把新增目录折叠成 `docs/`，前端按变更文件的完整路径
        # 匹配“可提交项”时就匹配不上，导致已落盘的新文件无法在 IDE 里提交。展开后逐文件列出。
        return parse_status(
            self._run("status", "--porcelain=v1", "--branch", "--untracked-files=all", root=root)
        )

    def diff(
        self,
        paths: list[str] | tuple[str, ...] | None = None,
        *,
        staged: bool = False,
        workspace_root: str | Path | None = None,
    ) -> GitDiff:
        root = self._root(workspace_root)
        relative = self._safe_paths(paths)
        args = ["diff"]
        if staged:
            args.append("--staged")
        if relative:
            args += ["--", *relative]
        return GitDiff(staged=staged, paths=tuple(relative), text=self._run(*args, root=root))

    def branches(self, *, workspace_root: str | Path | None = None) -> GitBranches:
        root = self._root(workspace_root)
        return parse_branches(self._run("branch", "--format=%(HEAD) %(refname:short)", root=root))

    # --- 写操作 ---

    def checkout(
        self,
        target: str,
        *,
        create: bool = False,
        workspace_root: str | Path | None = None,
    ) -> GitBranches:
        """切换分支；`create=True` 时新建并切换（`git checkout -b`）。

        工作区有冲突改动时 git 自己会拒绝，我们照原样把错误抛出——不做任何自动 stash。
        """
        root = self._root(workspace_root)
        name = (target or "").strip()
        if not name:
            raise ValidationError("分支名不能为空")
        if name.startswith("-"):
            raise ValidationError(f"非法的分支名：{target}", details={"target": target})
        args = ["checkout", "-b", name] if create else ["checkout", name]
        self._run(*args, root=root)
        return self.branches(workspace_root=root)

    def stage(
        self,
        paths: list[str] | tuple[str, ...],
        *,
        workspace_root: str | Path | None = None,
    ) -> GitStatus:
        """Stage explicit workspace paths without touching any other files."""
        root = self._root(workspace_root)
        relative = self._safe_paths(paths)
        if not relative:
            raise ValidationError("至少指定一个要暂存的文件")
        self._run("add", "--", *relative, root=root)
        return self.status(workspace_root=root)

    def unstage(
        self,
        paths: list[str] | tuple[str, ...],
        *,
        workspace_root: str | Path | None = None,
    ) -> GitStatus:
        """Remove explicit paths from the index while preserving working-tree changes."""
        root = self._root(workspace_root)
        relative = self._safe_paths(paths)
        if not relative:
            raise ValidationError("至少指定一个要取消暂存的文件")
        self._run("restore", "--staged", "--", *relative, root=root)
        return self.status(workspace_root=root)

    def commit(
        self,
        message: str,
        *,
        paths: list[str] | tuple[str, ...] | None = None,
        workspace_root: str | Path | None = None,
    ) -> GitCommit:
        """创建一次提交。

        - 给了 `paths`：先 `git add` 这些文件，再只提交这些文件（其余改动留在工作区）。
        - 没给 `paths`：提交暂存区里已有的内容；暂存区为空时 git 会报"nothing to commit"，
          我们照原样抛出，不静默成功（fail-closed）。
        """
        root = self._root(workspace_root)
        text = (message or "").strip()
        if not text:
            raise ValidationError("提交信息不能为空")
        relative = self._safe_paths(paths)
        if relative:
            self._run("add", "--", *relative, root=root)
            self._run("commit", "-m", text, "--", *relative, root=root)
        else:
            self._run("commit", "-m", text, root=root)
        sha = self._run("rev-parse", "HEAD", root=root).strip()
        changed = self._run("show", "--name-only", "--format=", sha, root=root)
        files = tuple(line.strip() for line in changed.splitlines() if line.strip())
        logger.info("git.commit sha=%s files=%d message=%s", sha[:7], len(files), text)
        return GitCommit(sha=sha, short_sha=sha[:7], message=text, files=files)

    # --- 内部 ---

    def _root(self, override: str | Path | None) -> Path:
        return resolve_workspace_root(override if override is not None else self._configured_root)

    @staticmethod
    def _safe_paths(paths: list[str] | tuple[str, ...] | None) -> list[str]:
        return [str(safe_relative_path(p)) for p in (paths or [])]

    def _run(self, *args: str, root: Path) -> str:
        command = ["git", *_GIT_GLOBAL_ARGS, *args]
        try:
            completed = subprocess.run(  # noqa: S603 - 参数列表由本模块构造，不经 shell
                command,
                cwd=root,
                capture_output=True,
                text=True,
                # git 输出含非 ASCII（中文文件名/提交信息）时按 UTF-8 解码；
                # Windows 默认 locale 解码会抛 UnicodeDecodeError，直接打断 Git 集成。
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitError(
                "未找到 git 命令，无法执行 Git 操作", details={"args": list(args)}
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(
                f"git {args[0]} 超时（{self._timeout}s）",
                details={"args": list(args), "timeout_seconds": self._timeout},
            ) from exc
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            details = {
                "args": list(args),
                "exit_code": completed.returncode,
                "stderr": stderr,
            }
            if "not a git repository" in stderr:
                raise NotAGitRepositoryError(f"{root} 不是 Git 仓库", details=details)
            raise GitError(
                f"git {args[0]} 执行失败（exit={completed.returncode}）：{stderr}",
                details=details,
            )
        return completed.stdout or ""
