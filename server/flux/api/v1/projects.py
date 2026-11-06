"""Project / Project Brain API（主规格 §12.12；实施计划 ⑩⑪）。

Project Scanner（⑩）只读不写：遍历工作区目录产出画像。Project Brain（⑪）是
结构化记忆：六种分区，现状型分区覆盖写入、累积型分区只追加。

`POST /projects/{id}/scan` 会把扫描结果自动落进 Brain（Overview + Tech Stack），
`record=false` 时只返回画像、不改任何记忆。整组接口都是纯本地服务端行为，不调模型。
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from flux.api.auth import is_trusted_local
from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.core.project_files.explorer import DEFAULT_TREE_DEPTH, MAX_SEARCH_RESULTS, MAX_TREE_DEPTH
from flux.schemas.api import (
    MemoryWriteRequest,
    ProjectCreateRequest,
    ScanRequest,
    WorkspaceFileCreateRequest,
    WorkspaceFilePathRequest,
    WorkspaceFileRenameRequest,
    WorkspaceFileReplaceRequest,
    WorkspaceFileWriteRequest,
)

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("")
async def list_projects(container: Container = Depends(get_container)) -> dict[str, object]:
    projects = await container.brain.list_projects()
    return ok([project.to_dict() for project in projects], metadata={"count": len(projects)})


@router.post("")
async def create_project(
    payload: ProjectCreateRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    project = await container.brain.create_project(
        name=payload.name, repository=payload.repository, meta=payload.metadata or None
    )
    return ok(project.to_dict())


@router.get("/{project_id}")
async def get_project(
    project_id: uuid.UUID, container: Container = Depends(get_container)
) -> dict[str, object]:
    project = await container.brain.get_project(project_id)
    return ok(project.to_dict())


@router.get("/{project_id}/memory")
async def get_memory(
    project_id: uuid.UUID, container: Container = Depends(get_container)
) -> dict[str, object]:
    """六个分区全部列出（缺给空列表），便于前端直接渲染。"""
    project = await container.brain.get_project(project_id)
    sections = await container.brain.sections(project.id)
    total = sum(len(entries) for entries in sections.values())
    return ok(sections, metadata={"count": total})


@router.post("/{project_id}/memory")
async def write_memory(
    project_id: uuid.UUID,
    payload: MemoryWriteRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """写记忆：现状型分区覆盖当前内容，累积型分区新增一条。"""
    project = await container.brain.get_project(project_id)
    entry = await container.brain.write(
        project.id, section=payload.section, content=payload.content, meta=payload.metadata or None
    )
    return ok(entry.to_dict())


@router.get("/{project_id}/memory/context")
async def get_context(
    project_id: uuid.UUID, container: Container = Depends(get_container)
) -> dict[str, object]:
    """拼出 Agent 执行前注入的项目上下文（Project Brain 的消费入口）。"""
    project = await container.brain.get_project(project_id)
    text = await container.brain.context(project.id)
    return ok({"text": text}, metadata={"chars": len(text)})


@router.post("/{project_id}/scan")
async def scan_project(
    project_id: uuid.UUID,
    payload: ScanRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """扫描工作区（⑩）并默认写入 Brain（⑪）；record=false 只拿画像、不落记忆。"""
    project = await container.brain.get_project(project_id)
    outcome = await container.brain.scan(
        project.id,
        workspace_root=payload.workspace_root,
        record=payload.record,
    )
    return ok(outcome.to_dict(), metadata={"truncated": outcome.profile.truncated})


def _require_desktop_file_operation(
    container: Container,
    x_flux_desktop: str | None,
) -> None:
    if container.settings.env != "local" or x_flux_desktop != "1":
        raise HTTPException(status_code=403, detail="本地文件操作只允许 Flux 桌面端")


def _require_file_save(
    container: Container,
    request: Request,
    x_flux_desktop: str | None,
) -> None:
    """保存文件：本地环境，且必须来自本机直连或 Flux 桌面端。

    Web dashboard 与桌面端都跑在同一台机器上，`is_trusted_local` 已经覆盖了
    「本机直连且无反向代理头」的情况；`X-Flux-Desktop` 额外覆盖桌面端经代理的路径。
    """
    if container.settings.env != "local":
        raise HTTPException(status_code=403, detail="保存文件只允许在本地环境使用")
    if x_flux_desktop == "1" or is_trusted_local(request):
        return
    raise HTTPException(status_code=403, detail="保存文件只允许本机访问或 Flux 桌面端")


@router.post("/{project_id}/files/file")
async def create_project_file(
    project_id: uuid.UUID,
    payload: WorkspaceFileCreateRequest,
    container: Container = Depends(get_container),
    x_flux_desktop: str | None = Header(default=None),
) -> dict[str, object]:
    """Create one human-selected file in the active workspace."""
    _require_desktop_file_operation(container, x_flux_desktop)
    await container.brain.get_project(project_id)
    path = await asyncio.to_thread(
        container.files.create_file,
        path=payload.path,
        content=payload.content,
    )
    return ok({"path": path})


@router.post("/{project_id}/files/directory")
async def create_project_directory(
    project_id: uuid.UUID,
    payload: WorkspaceFilePathRequest,
    container: Container = Depends(get_container),
    x_flux_desktop: str | None = Header(default=None),
) -> dict[str, object]:
    """Create one human-selected directory in the active workspace."""
    _require_desktop_file_operation(container, x_flux_desktop)
    await container.brain.get_project(project_id)
    path = await asyncio.to_thread(container.files.create_directory, path=payload.path)
    return ok({"path": path})


@router.patch("/{project_id}/files")
async def rename_project_path(
    project_id: uuid.UUID,
    payload: WorkspaceFileRenameRequest,
    container: Container = Depends(get_container),
    x_flux_desktop: str | None = Header(default=None),
) -> dict[str, object]:
    """Rename/move one human-selected file or directory within the workspace."""
    _require_desktop_file_operation(container, x_flux_desktop)
    await container.brain.get_project(project_id)
    path = await asyncio.to_thread(
        container.files.rename,
        path=payload.path,
        new_path=payload.new_path,
    )
    return ok({"path": path})


@router.delete("/{project_id}/files")
async def delete_project_path(
    project_id: uuid.UUID,
    payload: WorkspaceFilePathRequest,
    container: Container = Depends(get_container),
    x_flux_desktop: str | None = Header(default=None),
) -> dict[str, object]:
    """Delete one human-selected file or directory in the active workspace."""
    _require_desktop_file_operation(container, x_flux_desktop)
    await container.brain.get_project(project_id)
    path = await asyncio.to_thread(container.files.delete, path=payload.path)
    return ok({"path": path})


@router.get("/{project_id}/search")
async def search_project_files(
    project_id: uuid.UUID,
    q: str = Query(min_length=1, max_length=500, description="工作区文本搜索关键词"),
    path: str | None = Query(default=None, description="限定搜索范围的工作区相对子目录"),
    case_sensitive: bool = Query(default=False),
    regex: bool = Query(default=False),
    max_results: int = Query(default=200, ge=1, le=MAX_SEARCH_RESULTS),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """Search text across the active workspace; binary and oversized files are skipped."""
    await container.brain.get_project(project_id)
    hits = await asyncio.to_thread(
        container.files.search,
        query=q,
        path=path,
        case_sensitive=case_sensitive,
        regex=regex,
        max_results=max_results,
    )
    return ok([hit.to_dict() for hit in hits], metadata={"count": len(hits), "query": q})


@router.post("/{project_id}/search/replace")
async def replace_project_files(
    project_id: uuid.UUID,
    payload: WorkspaceFileReplaceRequest,
    container: Container = Depends(get_container),
    x_flux_desktop: str | None = Header(default=None),
) -> dict[str, object]:
    """Replace matching text in the active workspace as an explicit human operation."""
    _require_desktop_file_operation(container, x_flux_desktop)
    await container.brain.get_project(project_id)
    result = await asyncio.to_thread(
        container.files.replace,
        query=payload.query,
        replacement=payload.replacement,
        path=payload.path,
        case_sensitive=payload.case_sensitive,
        regex=payload.regex,
    )
    return ok(result, metadata={"query": payload.query})


@router.get("/{project_id}/files")
async def list_project_files(
    project_id: uuid.UUID,
    path: str | None = Query(default=None, description="工作区内的相对子目录，缺省为工作区根"),
    depth: int = Query(
        default=DEFAULT_TREE_DEPTH,
        ge=1,
        le=MAX_TREE_DEPTH,
        description=f"向下展开的层数（上限 {MAX_TREE_DEPTH}）",
    ),
    workspace_root: str | None = Query(
        default=None, description="按次覆盖工作区根；缺省用 FLUX_WORKSPACE_ROOT"
    ),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """列出工作区文件树（只读）。

    路径经 `safe_relative_path` 校验、沿途软链一律拒绝；忽略规则复用 Project Scanner；
    条目数触顶时 `truncated=true`，绝不静默丢结果。这里**不**关联待审提案——
    前端用 `/workspace/changes` 自行关联，后端不耦合。
    """
    await container.brain.get_project(project_id)
    tree = await asyncio.to_thread(
        container.files.tree, path=path, depth=depth, workspace_root=workspace_root
    )
    return ok(tree.to_dict(), metadata={"count": len(tree.entries)})


@router.get("/{project_id}/files/content")
async def read_project_file(
    project_id: uuid.UUID,
    path: str = Query(description="工作区内的文件相对路径（必填）"),
    workspace_root: str | None = Query(
        default=None, description="按次覆盖工作区根；缺省用 FLUX_WORKSPACE_ROOT"
    ),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """读单个文件的文本内容（只读，绝不写入或创建文件）。

    越界路径（`../`、绝对路径、根外软链）一律 `validation_error`；二进制（含 NUL）
    与非 UTF-8 文本同样拒绝；超过 256 KiB 只返回前 256 KiB 并置 `truncated=true`，
    `size` 仍是文件真实字节数。
    """
    await container.brain.get_project(project_id)
    content = await asyncio.to_thread(
        container.files.read, path=path, workspace_root=workspace_root
    )
    return ok(content.to_dict(), metadata={"truncated": content.truncated})


@router.put("/{project_id}/files/content")
async def write_project_file(
    project_id: uuid.UUID,
    payload: WorkspaceFileWriteRequest,
    request: Request,
    container: Container = Depends(get_container),
    x_flux_desktop: str | None = Header(default=None),
) -> dict[str, object]:
    """保存编辑器内容：覆写一个已存在的文件（不新建、不重命名、不删）。

    越界路径、`.flux/` 与 `.git/` 内部、二进制/非普通文件、超过 256 KiB 一律拒绝。
    写入是原地截断，保留 inode 与权限位——语言服务器与文件监视器不会把它当成新文件。
    """
    _require_file_save(container, request, x_flux_desktop)
    await container.brain.get_project(project_id)
    content = await asyncio.to_thread(
        container.files.write,
        path=payload.path,
        content=payload.content,
        workspace_root=payload.workspace_root,
    )
    return ok(content.to_dict(), metadata={"saved": True, "size": content.size})
