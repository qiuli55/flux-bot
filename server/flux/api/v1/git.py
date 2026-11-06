"""Git API（主规格 §17.6；实施计划 ⑨）。

只暴露本地、可回退的 Git 操作：status / diff / branches / checkout / commit。
**没有 push、没有 force、没有 reset** —— 提交权与合并权始终在用户手里（§7 原则）。

按 `change_ids` 提交时，服务层会复验每条提案是否已 `applied`（已批准 + 已落盘 +
Apply 后测试通过，§7.6），否则报 `invalid_state_transition`。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.schemas.api import GitCheckoutRequest, GitCommitRequest, GitDiffRequest, GitPathsRequest

router = APIRouter(prefix="/git", tags=["git"])


@router.get("/status")
async def git_status(container: Container = Depends(get_container)) -> dict[str, object]:
    return ok((await container.git.status()).to_dict())


@router.post("/diff")
async def git_diff(
    payload: GitDiffRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    diff = await container.git.diff(payload.paths or None, staged=payload.staged)
    return ok(diff.to_dict())


@router.get("/branches")
async def git_branches(container: Container = Depends(get_container)) -> dict[str, object]:
    return ok((await container.git.branches()).to_dict())


@router.post("/checkout")
async def git_checkout(
    payload: GitCheckoutRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    branches = await container.git.checkout(payload.target, create=payload.create)
    return ok(branches.to_dict())


@router.post("/stage")
async def git_stage(
    payload: GitPathsRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    status = await container.git.stage(payload.paths)
    return ok(status.to_dict())


@router.post("/unstage")
async def git_unstage(
    payload: GitPathsRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    status = await container.git.unstage(payload.paths)
    return ok(status.to_dict())


@router.post("/commit")
async def git_commit(
    payload: GitCommitRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    commit = await container.git.commit(
        payload.message, change_ids=payload.change_ids, paths=payload.paths
    )
    return ok(commit.to_dict())
