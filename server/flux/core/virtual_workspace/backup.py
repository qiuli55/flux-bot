"""原文件备份与回滚（主规格 §7.6 的"备份原文件"一步；实施计划 ⑦）。

备份落在 `<工作区根>/.flux/backups/<change_id>/<file_path>`：与项目同处一棵树下，
内网/离线环境都能用，不引入额外存储依赖。

项目本身是 Git 仓库时，`.flux/` 会写进 `.git/info/exclude`：既不污染用户的 `git status`
（否则每次 Apply 后用户都会看到一堆 Flux 自己的备份），也不去改用户受版本控制的
`.gitignore` —— Flux 不该替用户决定什么该进版本库。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from flux.core.virtual_workspace.path_guard import resolve_within_root
from flux.logging import get_logger

logger = get_logger(__name__)

BACKUP_DIRNAME = ".flux"

#: 备份根相对工作区的路径：apply_batches.backup_root 记的就是它，
#: 崩溃恢复据此推出"每条 change 的备份应该在哪"。
BACKUP_RELATIVE_ROOT = Path(BACKUP_DIRNAME) / "backups"

#: 覆盖旧备份前留档的后缀（`.flux/backups/<change_id>/a.py.prev`）
BACKUP_PREV_SUFFIX = ".prev"


class BackupService:
    def __init__(self, *, workspace_root: Path, dirname: str = BACKUP_DIRNAME) -> None:
        self._root = workspace_root
        self._dirname = dirname

    @property
    def backup_root(self) -> Path:
        return self._root / self._dirname / "backups"

    def backup(self, *, change_id: str, file_path: str, relative_target: Path) -> Path | None:
        """把待改文件复制到备份目录。文件不存在（新建文件）时返回 None。

        待改文件与备份落点都先过 `resolve_within_root`：文件本身、沿途目录或
        `.flux/` 是软链时一律拒绝——绝不把用户文件复制到工作区之外。

        同一 change 重复 Apply（崩溃恢复后的重试）会命中同一备份路径：覆盖前先留一份
        `.prev` 快照，上一轮备份仍可追踪（P0-1 §2.2 的重试语义）。
        """
        source = resolve_within_root(self._root, relative_target)
        if not source.is_file():
            return None
        destination = resolve_within_root(
            self._root, Path(self._dirname) / "backups" / change_id / relative_target
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_git_ignored()
        if destination.is_file():
            previous = destination.with_name(destination.name + BACKUP_PREV_SUFFIX)
            shutil.copy2(destination, previous)
            logger.info("apply.backup 覆盖前留档 change=%s prev=%s", change_id, previous)
        shutil.copy2(source, destination)
        logger.info("apply.backup change=%s file=%s → %s", change_id, file_path, destination)
        return destination

    def restore(self, *, backup_path: Path, relative_target: Path) -> None:
        """用备份覆盖回原文件（回滚）；还原目标同样不许是软链。"""
        target = resolve_within_root(self._root, relative_target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_path, target)
        logger.warning("apply.rollback file=%s ← %s", target, backup_path)

    def _ensure_git_ignored(self) -> None:
        """把备份目录写进 `.git/info/exclude`（本地忽略，不进版本库）。"""
        exclude = self._root / ".git" / "info" / "exclude"
        # `.git` 是文件（worktree / submodule）或不是仓库时直接跳过
        if not exclude.is_file():
            return
        try:
            text = exclude.read_text(encoding="utf-8")
            if any(line.strip() == f"{self._dirname}/" for line in text.splitlines()):
                return
            separator = "" if not text or text.endswith("\n") else "\n"
            exclude.write_text(
                f"{text}{separator}# Flux 改动备份目录（Apply 自动添加，可安全删除）\n"
                f"{self._dirname}/\n",
                encoding="utf-8",
                # git 的 exclude 用 LF 更稳妥（Windows 默认会把 \n 翻成 CRLF）
                newline="\n",
            )
        except OSError as exc:  # 忽略失败：备份本身比"是否被 git 忽略"重要
            logger.warning("写入 .git/info/exclude 失败，备份不受影响：%s", exc)
            return
        logger.info("已把 %s/ 加入 %s", self._dirname, exclude)
