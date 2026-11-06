"""Git 服务的策略层（实施计划 ⑨）。

`GitClient` 只管执行 git 命令，本模块只回答一个问题：**什么样的改动才允许进 Git 历史**。

实施计划 §13 的闭环顺序是 `Task → Virtual Changes → Approved → Tests Passed → Git Commit`。
在 Flux 里「applied」正好等价于「已人工批准 + 已落盘 + Apply 后测试通过」（§7.6），
所以按 `change_ids` 提交时逐条复验状态即可，不需要另建一套"测试通过"的标记。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from pathlib import Path

from flux.core.event.bus import EventBus, Events
from flux.core.git_integration.client import (
    GitBranches,
    GitClient,
    GitCommit,
    GitDiff,
    GitStatus,
)
from flux.core.virtual_workspace.service import VirtualWorkspaceService
from flux.enums import VirtualChangeStatus
from flux.errors import InvalidTransitionError, ValidationError
from flux.logging import get_logger

logger = get_logger(__name__)


class GitService:
    def __init__(
        self,
        git: GitClient,
        workspace: VirtualWorkspaceService | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self._git = git
        self._workspace = workspace
        self._bus = bus

    # --- 只读 ---

    async def status(self, *, workspace_root: str | Path | None = None) -> GitStatus:
        return await asyncio.to_thread(self._git.status, workspace_root=workspace_root)

    async def diff(
        self,
        paths: Sequence[str] | None = None,
        *,
        staged: bool = False,
        workspace_root: str | Path | None = None,
    ) -> GitDiff:
        return await asyncio.to_thread(
            self._git.diff,
            list(paths) if paths else None,
            staged=staged,
            workspace_root=workspace_root,
        )

    async def branches(self, *, workspace_root: str | Path | None = None) -> GitBranches:
        return await asyncio.to_thread(self._git.branches, workspace_root=workspace_root)

    # --- 写 ---

    async def checkout(
        self,
        target: str,
        *,
        create: bool = False,
        workspace_root: str | Path | None = None,
    ) -> GitBranches:
        return await asyncio.to_thread(
            self._git.checkout, target, create=create, workspace_root=workspace_root
        )

    async def stage(
        self,
        paths: Sequence[str],
        *,
        workspace_root: str | Path | None = None,
    ) -> GitStatus:
        return await asyncio.to_thread(self._git.stage, list(paths), workspace_root=workspace_root)

    async def unstage(
        self,
        paths: Sequence[str],
        *,
        workspace_root: str | Path | None = None,
    ) -> GitStatus:
        return await asyncio.to_thread(
            self._git.unstage, list(paths), workspace_root=workspace_root
        )

    async def commit(
        self,
        message: str,
        *,
        change_ids: Sequence[str | uuid.UUID] | None = None,
        paths: Sequence[str] | None = None,
        workspace_root: str | Path | None = None,
    ) -> GitCommit:
        """提交改动。

        `change_ids` 与 `paths` 都传时以 `change_ids` 解析出的路径为准（二者取并集，
        由前者把关状态）。都不传表示提交用户自己 `git add` 过的暂存区内容。
        """
        resolved = await self._paths_from_change_ids(change_ids)
        target_paths = [*resolved, *(paths or [])]
        commit = await asyncio.to_thread(
            self._git.commit,
            message,
            paths=target_paths or None,
            workspace_root=workspace_root,
        )
        await self._publish(commit, change_ids=change_ids)
        return commit

    async def _paths_from_change_ids(
        self, change_ids: Sequence[str | uuid.UUID] | None
    ) -> list[str]:
        if not change_ids:
            return []
        if self._workspace is None:
            raise ValidationError("未接入 Virtual Workspace，无法按 change_ids 提交")
        paths: list[str] = []
        for change_id in change_ids:
            change = await self._workspace.get(change_id)
            if change.status != VirtualChangeStatus.APPLIED.value:
                raise InvalidTransitionError(
                    f"虚拟改动 {change_id} 当前状态为 {change.status}，"
                    "只有 applied（已批准、已落盘、测试通过）的改动才能提交",
                    details={"change_id": str(change.id), "status": change.status},
                )
            paths.append(change.file_path)
        return paths

    async def _publish(
        self, commit: GitCommit, *, change_ids: Sequence[str | uuid.UUID] | None
    ) -> None:
        if self._bus is None:
            return
        await self._bus.publish(
            Events.GIT_COMMITTED,
            {
                "sha": commit.sha,
                "short_sha": commit.short_sha,
                "files": list(commit.files),
                "change_ids": [str(c) for c in (change_ids or [])],
            },
        )
