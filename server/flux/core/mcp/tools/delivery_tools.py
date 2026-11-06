"""文件下发工具：把工作区里做好的成品送进用户对话。

与 `design.share` 同一套路：不新增数据表，只往当前任务写一条结构化消息
（`kind="file_card"`），文件本身落在 `delivery_store` 的目录里。手机端据此渲染
可切换预览的堆叠卡片；Web 端也能按 payload 里的清单渲染。

只允许下发工作区根之内的普通文件：路径守卫与 `workspace.read` 同一套
（绝对路径 / `..` / 软链逃逸 / 密钥文件 / `.flux` 内部一律拒绝）。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from flux.core.delivery_store import DELIVERY_MAX_FILES, Delivery, create_delivery
from flux.core.mcp.tools.base import ToolContext, ToolSpec, optional_str, reject_unknown
from flux.core.task_engine.repository import MessageDraft
from flux.core.virtual_workspace.apply_engine import (
    resolve_workspace_root,
    safe_relative_path,
)
from flux.core.virtual_workspace.path_guard import (
    ensure_not_flux_internal,
    ensure_not_secret_path,
    resolve_within_root,
)
from flux.enums import Capability, TaskMessageRole
from flux.errors import NotFoundError, ValidationError


async def _task_for_run(ctx: ToolContext) -> uuid.UUID:
    run_id = getattr(ctx.identity, "run_id", None)
    if not run_id:
        raise ValidationError(
            f"{ctx.tool_name} 必须在 Flux 任务 Run 内调用（当前调用没有关联 Run）"
        )
    run = await ctx.container.run_repo.get(run_id)
    if run is None or run.task_id is None:
        raise ValidationError(
            f"{ctx.tool_name} 找不到当前 Run 归属的任务，无法写入对话",
            details={"run_id": run_id},
        )
    return run.task_id


def _sources(ctx: ToolContext, raw_paths: list[Any]) -> list[tuple[str, Path]]:
    """把 Agent 给的相对路径校验成 `(显示名, 绝对路径)`，重复路径只留一份。"""
    root = resolve_workspace_root(ctx.container.files.workspace_root)
    sources: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for raw in raw_paths:
        if not isinstance(raw, str) or not raw.strip():
            raise ValidationError("paths 里每一项都必须是非空字符串", details={"param": "paths"})
        text = raw.strip()
        ensure_not_secret_path(text)
        relative = safe_relative_path(text)
        ensure_not_flux_internal(relative)
        key = relative.as_posix()
        if key in seen:
            continue
        seen.add(key)
        target = resolve_within_root(root, relative)
        if not target.exists():
            raise NotFoundError(
                f"文件不存在：{key}",
                details={"path": key, "hint": "路径必须相对工作区根（FLUX_WORKSPACE_ROOT）"},
            )
        if not target.is_file():
            raise ValidationError(f"路径不是普通文件：{key}", details={"path": key})
        sources.append((relative.name, target))
    if not sources:
        raise ValidationError("没有可下发的文件", details={"param": "paths"})
    return sources


def _content(delivery: Delivery, note: str) -> str:
    if note:
        return note
    if len(delivery.files) == 1:
        return f"已为你准备好文件：{delivery.files[0].name}"
    return f"已为你准备好 {len(delivery.files)} 个文件"


async def file_deliver(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"paths", "note"})
    raw_paths = params.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValidationError(
            "paths 必须是非空的文件路径数组（相对工作区根）",
            details={"param": "paths"},
        )
    if len(raw_paths) > DELIVERY_MAX_FILES:
        raise ValidationError(
            f"一次最多下发 {DELIVERY_MAX_FILES} 个文件",
            details={"count": len(raw_paths), "limit": DELIVERY_MAX_FILES},
        )
    note = optional_str(params, "note") or ""
    task_id = await _task_for_run(ctx)
    sources = _sources(ctx, raw_paths)
    delivery = await asyncio.to_thread(
        create_delivery, ctx.container.settings, sources=sources, note=note
    )
    message = (
        await ctx.container.task_repo.add_messages(
            task_id,
            [
                MessageDraft(
                    role=TaskMessageRole.ASSISTANT.value,
                    kind="file_card",
                    content=_content(delivery, note),
                    payload=delivery.to_dict(),
                )
            ],
        )
    )[0]
    return {
        "delivered": True,
        "task_id": str(task_id),
        "message_id": str(message.id),
        "delivery_id": delivery.id,
        "files": [item.to_dict() for item in delivery.files],
        "note": "文件卡片已写入当前任务对话，用户在手机上可查看预览并切换文件。",
    }


# ---------------------------------------------------------------- 注册


def delivery_tools() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name="file.deliver",
            title="把文件发给用户",
            description=(
                "把已经做好的成品文件交付到当前任务对话：用户在手机端会收到一张文件卡片，"
                "图片 / PDF / 文本 / 音视频可直接预览，其他类型可下载后用系统应用打开。"
                "paths 必须是相对工作区根的路径（不能是绝对路径、不能含 ..、不能是密钥文件），"
                f"一次最多 {DELIVERY_MAX_FILES} 个；单个文件不超过 50MB。"
                "只发用户需要带走的成品（报告、截图、导出数据、压缩包等），"
                "不要把中间产物、日志、依赖目录发过去。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": DELIVERY_MAX_FILES,
                        "description": "要下发的文件路径（相对工作区根），按展示顺序排列",
                    },
                    "note": {
                        "type": "string",
                        "description": "卡片上的一句话说明（可空），例如「报告和原始数据都在这里」",
                    },
                },
                "required": ["paths"],
                "additionalProperties": False,
            },
            capability=Capability.FILE_WRITE,
            handler=file_deliver,
        ),
    )


__all__ = ["delivery_tools", "file_deliver"]
