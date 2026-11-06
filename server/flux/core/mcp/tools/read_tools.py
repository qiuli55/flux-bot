"""只读工具：`context.get` / `context.fetch` / `workspace.read` / `workspace.diff`（§3.3）。

这些工具都不写盘、不改状态，因此在令牌能力层面只要 `file.read`；
它们背后分别是打包器、内容寻址原文库、Project File Explorer（只读、含软链防护）
与 Virtual Workspace——本层不重复实现任何文件访问，避免出现第二份"能读用户文件"的代码。

`workspace.tree / read_many / search` 是为减少 Agent 往返而加的探索面：
过去 88% 的 MCP 调用是单文件 workspace.read，一次只拿一个文件，步数被读取次数撑大。
"""

from __future__ import annotations

import asyncio
from pathlib import PurePosixPath
from typing import Any

from flux.core.context.catalog import catalog_line
from flux.core.context.redact import redact
from flux.core.mcp.context_packager import DEFAULT_BUDGET_CHARS, package_context
from flux.core.mcp.tools.base import (
    ToolContext,
    ToolSpec,
    optional_str,
    reject_unknown,
    require_str,
)
from flux.core.virtual_workspace.diff_engine import content_hash
from flux.core.virtual_workspace.path_guard import (
    ensure_not_flux_internal,
    ensure_not_git_internal,
    ensure_not_secret_path,
    is_secret_path,
)
from flux.enums import Capability
from flux.errors import AIOSError, NotFoundError, PermissionDeniedError, ValidationError

CONTEXT_GET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "description": "任务 UUID；带上它才会打包 L1 任务上下文"},
        "project_id": {"type": "string", "description": "项目 UUID；不传则从任务自动推导"},
        "budget": {
            "type": "integer",
            "minimum": 1,
            "description": f"可投喂字符预算，默认 {DEFAULT_BUDGET_CHARS}",
        },
    },
    "additionalProperties": False,
}


async def context_get(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"task_id", "project_id", "budget"})
    # budget 缺省交给打包器按 §6.1 推导，这里只拒绝"传了但不是整数"
    budget = params.get("budget")
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int)):
        raise ValidationError("参数 budget 必须是整数", details={"param": "budget"})
    return await package_context(
        ctx.container,
        task_id=optional_str(params, "task_id"),
        project_id=optional_str(params, "project_id"),
        budget=budget,
    )


#: 单次 fetch 的默认字符预算。远小于打包预算（24_000）：第三档降级只投递 step_id，
#: agent 真需要原文时按需取回；§6.7 要求 range + budget 并存，正是为了防"一不确定
#: 就把整段历史拉回来"——默认给得小，让它被迫分页而不是整段吞。
DEFAULT_FETCH_CHARS = 8_000

CONTEXT_FETCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "step_id": {
            "type": "integer",
            "description": "目录行里的 #编号，例如 context.fetch(step_id=42)",
        },
        "range": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0},
            "minItems": 2,
            "maxItems": 2,
            "description": "字符区间 [start, end)，左闭右开；不传则从 0 开始",
        },
        "budget": {
            "type": "integer",
            "minimum": 1,
            "description": f"本次最多返回的字符数，默认 {DEFAULT_FETCH_CHARS}",
        },
    },
    "required": ["step_id"],
    "additionalProperties": False,
}


def _required_int(params: dict[str, Any], key: str) -> int:
    if key not in params:
        raise ValidationError(f"缺少必填参数：{key}", details={"param": key})
    return _as_int(params[key], key)


def _optional_int(params: dict[str, Any], key: str, *, default: int) -> int:
    if key not in params:
        return default
    return _as_int(params[key], key)


def _as_int(value: Any, key: str) -> int:
    # bool 是 int 的子类，显式排除，避免 True 被当成 1
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"参数 {key} 必须是整数", details={"param": key})
    return value


def _parse_range(params: dict[str, Any]) -> tuple[int, int | None]:
    """解析 [start, end)：缺省 end 表示"到末尾"。"""
    raw = params.get("range")
    if raw is None:
        return 0, None
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValidationError(
            "参数 range 必须是 [start, end] 两元素数组", details={"param": "range"}
        )
    start = _as_int(raw[0], "range")
    end = _as_int(raw[1], "range")
    if start < 0 or end < 0:
        raise ValidationError("参数 range 的两端必须是非负整数", details={"param": "range"})
    if end < start:
        raise ValidationError("参数 range 的 end 不能小于 start", details={"param": "range"})
    return start, end


async def context_fetch(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    """按 step_id 取回某一步的原文片段（§6.7 / §7.1 第三档的消费方）。

    这是三档降级里"只给引用"那一档的出口：目录行露出 `#编号`，agent 需要细节时
    用本工具按 range + budget 分页取回，而不是要求打包器把整段历史塞进投喂。
    """
    reject_unknown(params, {"step_id", "range", "budget"})
    step_id = _required_int(params, "step_id")
    start, end = _parse_range(params)
    budget = _optional_int(params, "budget", default=DEFAULT_FETCH_CHARS)
    if budget < 1:
        raise ValidationError("参数 budget 必须是正整数", details={"param": "budget"})

    step = await ctx.container.context_steps.get(step_id)
    if not step.result_sha256:
        raise NotFoundError(f"step {step_id} 没有可取的原文（未记录结果）")
    raw = await ctx.container.content_store.get(step.result_sha256)
    text = raw.decode("utf-8", errors="replace")

    total = len(text)
    start = min(start, total)
    stop = total if end is None else min(end, total)
    stop = min(stop, start + budget)
    chunk = text[start:stop]
    truncated = stop < total
    # 回补率观测（§6.7）：真的把原文交回给 agent 才算一次回补，异常路径不计
    ctx.container.context_backfill.note_fetched()
    return {
        "step_id": step.id,
        "catalog_line": catalog_line(step),
        "kind": step.kind,
        "status": step.status,
        "sha256": step.result_sha256,
        "size_bytes": step.result_bytes,
        "total_chars": total,
        "offset": start,
        "returned_chars": len(chunk),
        "truncated": truncated,
        "next_offset": stop if truncated else None,
        "content": chunk,
        "facts": step.facts,
    }


#: context.steps 单次最多返回多少条目录行（step 只增不删，一次全拉会撑爆上下文）
MAX_CONTEXT_STEPS = 200
#: 每条 step 摘要的字符上限：只要够判断"要不要 fetch 原文"
STEP_SUMMARY_CHARS = 240

CONTEXT_STEPS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "run_id": {
            "type": "string",
            "description": "Run id；不传则取当前请求绑定的 Run（X-Flux-Run-Id）",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_CONTEXT_STEPS,
            "description": f"最多返回多少条，默认 50（上限 {MAX_CONTEXT_STEPS}）",
        },
    },
    "additionalProperties": False,
}


async def context_steps(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    """列出某个 Run 的 step 目录：工具、状态、时间、摘要（失败步带 `!` 标记）。

    `context_steps` 过去只写不读——Agent 看不到自己刚走过的路，也接不上前一轮 Run；
    这里补的是"目录入口"，细节仍由 `context.fetch(step_id=...)` 按需取回。
    只允许看本 Agent 名下的 Run：step 正文可能含工作区内容，不能跨 Agent 读。
    """
    reject_unknown(params, {"run_id", "limit"})
    run_id = optional_str(params, "run_id") or ctx.identity.run_id
    if not run_id:
        raise ValidationError(
            "需要 run_id：当前请求没有绑定 Run（缺 X-Flux-Run-Id）", details={"param": "run_id"}
        )
    limit = _optional_int(params, "limit", default=50)
    if limit < 1 or limit > MAX_CONTEXT_STEPS:
        raise ValidationError(
            f"参数 limit 必须在 1..{MAX_CONTEXT_STEPS} 之间", details={"param": "limit"}
        )

    if run_id != ctx.identity.run_id:
        run = await ctx.container.run_repo.get(run_id)
        if run is None:
            raise NotFoundError(f"Run {run_id} 不存在", details={"run_id": run_id})
        if run.agent_id != ctx.identity.agent_id:
            raise PermissionDeniedError(
                "只能查看本 Agent 名下的 Run 步骤", details={"run_id": run_id}
            )

    steps = await ctx.container.context_steps.list_by_run(run_id, limit=limit)
    rows: list[dict[str, Any]] = []
    for step in steps:
        # 密钥路径的历史 step（策略收紧前落库）不回给 Agent；正文摄入时已脱敏，这里再兜底一次
        if step.target_ref and is_secret_path(step.target_ref):
            continue
        summary = redact(step.result_head or "").text.strip().replace("\n", " ")
        rows.append(
            {
                "step_id": step.id,
                "catalog_line": catalog_line(step),
                "kind": step.kind,
                "status": step.status,
                "tool": step.tool,
                "target_ref": step.target_ref,
                "created_at": step.created_at.isoformat() if step.created_at else None,
                "summary": summary[:STEP_SUMMARY_CHARS],
            }
        )
    return {
        "run_id": run_id,
        "count": len(rows),
        "steps": rows,
        "note": "这是目录行；需要某一步的原文时用 context.fetch(step_id=...) 按需取回。",
    }


WORKSPACE_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                '相对工作区根的路径：文件返回内容；目录（含 "."）返回该层条目清单，'
                "例如 todo_service/store.py 或 backend"
            ),
        }
    },
    "required": ["path"],
    "additionalProperties": False,
}


def _directory_payload(tree) -> dict[str, Any]:
    """目录读取的统一返回：条目清单足以让 Agent 继续探索，不必再猜。"""
    return {
        "path": tree.path or ".",
        "kind": "dir",
        "entries": [entry.to_dict() for entry in tree.entries],
        "truncated": tree.truncated,
        "note": "这是目录清单；读取具体文件请把 path 指到文件。",
    }


async def workspace_read(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"path"})
    path = require_str(params, "path")
    # Secret 与 Flux 内部数据（备份/锁）是 MCP 面的硬禁令（§3.5 / §7）：直接拒绝，
    # 不因为"只是读一下"而放行
    ensure_not_secret_path(path)
    relative = PurePosixPath(path.replace("\\", "/"))
    ensure_not_flux_internal(relative)
    # workspace_root 不接受外部传入：能读到哪个目录由 Flux 配置决定，不由调用方指定
    if not relative.parts:
        # "." / "./" / "" 都是工作区根；safe_relative_path 会拒绝空 parts，这里直接列根目录
        tree = await asyncio.to_thread(ctx.container.files.tree, depth=1)
        return _directory_payload(tree)
    try:
        content = await asyncio.to_thread(ctx.container.files.read, path=path)
    except ValidationError as read_error:
        # 目录是合法探索目标：读文件失败时按目录再列一层，避免 Agent 在目录上反复试错。
        # 真错误（二进制/超限/缺失）由 tree 再次失败后原样抛出，不吞掉真实原因。
        try:
            tree = await asyncio.to_thread(ctx.container.files.tree, path=path, depth=1)
        except Exception as tree_error:  # noqa: BLE001 - 保留原始读错误
            raise read_error from tree_error
        return _directory_payload(tree)
    return {
        "path": content.path,
        "content": content.content,
        "size": content.size,
        "truncated": content.truncated,
        "sha256": content_hash(content.content),
    }


WORKSPACE_DIFF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "change_id": {
            "type": "string",
            "description": "提案 UUID（从 workspace.changed 事件或提案列表里拿到）",
        }
    },
    "required": ["change_id"],
    "additionalProperties": False,
}


async def workspace_diff(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"change_id"})
    change_id = require_str(params, "change_id")
    change = await ctx.container.workspace.get(change_id)
    # 历史提案可能是在密钥禁令收紧之前落库的，这里按当前策略复核一次：
    # diff 里就是完整文件内容，不能借"看 diff"把密钥读出去
    ensure_not_secret_path(change.file_path)
    ensure_not_flux_internal(PurePosixPath(change.file_path.replace("\\", "/")))
    return {
        "change_id": str(change.id),
        "file_path": change.file_path,
        "status": change.status,
        "diff": change.diff or "",
        "added_lines": change.added_lines,
        "removed_lines": change.removed_lines,
        "hunks": change.hunks,
        "original_hash": change.original_hash,
        "proposed_hash": content_hash(change.proposed_content or ""),
    }


#: 单次 read_many 的路径数上限与逐文件字符预算。10 × 6000 已能覆盖一次"看一组文件"
#: 的典型需求，又不会让一次调用把 60KB 灌进上下文。
MAX_READ_MANY_PATHS = 10
DEFAULT_READ_MANY_CHARS = 6_000
MAX_READ_MANY_TOTAL_CHARS = 30_000
#: workspace.tree 的目标是"给地图"而不是"搬目录"：条目再多也只回前 400 条。
MAX_TREE_AGENT_ENTRIES = 400
#: workspace.search 单次回给 Agent 的命中上限（explorer 自身允许到 1000）。
MAX_AGENT_SEARCH_RESULTS = 100

WORKSPACE_TREE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "相对工作区根的目录，默认根目录"},
        "depth": {"type": "integer", "minimum": 1, "maximum": 4, "description": "展开层数，默认 2"},
    },
    "additionalProperties": False,
}

WORKSPACE_READ_MANY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": MAX_READ_MANY_PATHS,
            "description": "一次要读的相对路径清单（文件或目录）",
        },
        "max_chars": {
            "type": "integer",
            "minimum": 200,
            "description": f"每个文件最多返回的字符数，默认 {DEFAULT_READ_MANY_CHARS}",
        },
    },
    "required": ["paths"],
    "additionalProperties": False,
}

WORKSPACE_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "要搜索的文本或正则"},
        "path": {"type": "string", "description": "搜索范围（相对目录），默认整个工作区"},
        "case_sensitive": {"type": "boolean", "description": "是否区分大小写，默认否"},
        "regex": {"type": "boolean", "description": "query 是否按正则解释，默认否（按字面量）"},
        "max_results": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_AGENT_SEARCH_RESULTS,
            "description": "最多返回多少条命中，默认 50",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


def _guarded(relative: PurePosixPath) -> None:
    """探索面统一路径禁令：密钥、`.flux/`、`.git/` 一律不碰。"""
    ensure_not_flux_internal(relative)
    ensure_not_git_internal(relative)


def _optional_bool(params: dict[str, Any], key: str, *, default: bool) -> bool:
    if key not in params:
        return default
    value = params[key]
    if not isinstance(value, bool):
        raise ValidationError(f"参数 {key} 必须是布尔值", details={"param": key})
    return value


async def workspace_tree(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    """给 Agent 一张紧凑目录地图，替代逐层 workspace.read(".") 的往返。"""
    reject_unknown(params, {"path", "depth"})
    path = optional_str(params, "path") or "."
    depth = _optional_int(params, "depth", default=2)
    if depth < 1 or depth > 4:
        raise ValidationError("参数 depth 必须在 1..4 之间", details={"param": "depth"})
    relative = PurePosixPath(path.replace("\\", "/"))
    ensure_not_secret_path(path)
    _guarded(relative)
    target = None if not relative.parts else path
    tree = await asyncio.to_thread(ctx.container.files.tree, path=target, depth=depth)
    entries = [
        {"path": entry.path, "kind": entry.kind, "size": entry.size}
        for entry in tree.entries[:MAX_TREE_AGENT_ENTRIES]
    ]
    return {
        "path": tree.path or ".",
        "entries": entries,
        "truncated": tree.truncated or len(tree.entries) > MAX_TREE_AGENT_ENTRIES,
        "note": "紧凑目录地图；读具体文件请用 workspace.read / read_many。",
    }


def _clip(item: dict[str, Any], limit: int) -> dict[str, Any]:
    """把单条读取结果裁剪到字符预算；目录清单不裁。"""
    content = item.get("content")
    if not isinstance(content, str) or len(content) <= limit:
        return item
    return {
        **item,
        "content": content[:limit],
        "truncated": True,
        "next_hint": f"已截断到 {limit} 字符；需要全文请用 workspace.read 单独读取。",
    }


async def workspace_read_many(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    """一次读多个文件，复用 workspace.read 的守卫与目录回退语义。"""
    reject_unknown(params, {"paths", "max_chars"})
    paths = params.get("paths")
    if not isinstance(paths, list) or not paths or len(paths) > MAX_READ_MANY_PATHS:
        raise ValidationError(
            f"参数 paths 必须是 1..{MAX_READ_MANY_PATHS} 个路径的数组；"
            f"超过 {MAX_READ_MANY_PATHS} 个请拆成多次 workspace.read_many 调用",
            details={"param": "paths", "max_items": MAX_READ_MANY_PATHS},
        )
    max_chars = _optional_int(params, "max_chars", default=DEFAULT_READ_MANY_CHARS)
    if max_chars < 200:
        raise ValidationError("参数 max_chars 不能小于 200", details={"param": "max_chars"})

    items: list[dict[str, Any]] = []
    used = 0
    for raw_path in paths:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValidationError("paths 里的每一项都必须是非空字符串", details={"param": "paths"})
        if used >= MAX_READ_MANY_TOTAL_CHARS:
            items.append({"path": raw_path, "skipped": "总字符预算已用尽"})
            continue
        limit = min(max_chars, MAX_READ_MANY_TOTAL_CHARS - used)
        try:
            item = await workspace_read(ctx, {"path": raw_path})
        except AIOSError as exc:
            # 单条失败不拖垮整批：把原因带回，Agent 可换路径继续
            items.append({"path": raw_path, "error": exc.message})
            continue
        clipped = _clip(item, limit)
        used += len(clipped.get("content") or "")
        items.append(clipped)
    return {
        "items": items,
        "count": len(items),
        "used_chars": used,
        "truncated": any(item.get("truncated") or item.get("skipped") for item in items),
    }


async def workspace_search(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    """在工作区内做文本搜索，让 Agent 先定位再读，而不是逐文件猜。"""
    reject_unknown(params, {"query", "path", "case_sensitive", "regex", "max_results"})
    query = require_str(params, "query")
    path = optional_str(params, "path") or "."
    relative = PurePosixPath(path.replace("\\", "/"))
    ensure_not_secret_path(path)
    _guarded(relative)
    max_results = _optional_int(params, "max_results", default=50)
    if max_results < 1 or max_results > MAX_AGENT_SEARCH_RESULTS:
        raise ValidationError(
            f"参数 max_results 必须在 1..{MAX_AGENT_SEARCH_RESULTS} 之间",
            details={"param": "max_results"},
        )
    hits = await asyncio.to_thread(
        ctx.container.files.search,
        query=query,
        path=None if not relative.parts else path,
        case_sensitive=_optional_bool(params, "case_sensitive", default=False),
        regex=_optional_bool(params, "regex", default=False),
        max_results=max_results,
    )
    # 搜索是独立实现，不走 workspace.read 的守卫；这里补上密钥过滤（fail-closed）
    safe = [hit for hit in hits if not is_secret_path(hit.path)]
    return {
        "query": query,
        "hits": [hit.to_dict() for hit in safe],
        "count": len(safe),
        "filtered_secrets": len(hits) - len(safe),
        "note": "命中的文本已是原文片段，可直接据此决定读哪个文件。",
    }


def read_tools() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name="context.get",
            title="拉取任务上下文",
            description=(
                "按预算拉取 Flux 侧存档的工程上下文（L0 项目 Brain / L1 任务 / L2 近期提案）。"
                "开工第一步应调用它；内容为快照，动手前用 workspace.read 核对磁盘当前状态。"
            ),
            input_schema=CONTEXT_GET_SCHEMA,
            capability=Capability.FILE_READ,
            handler=context_get,
        ),
        ToolSpec(
            name="context.fetch",
            title="取回某步原文",
            description=(
                "按 step_id 取回历史某一步的原文片段（目录行里的 #编号）。"
                "上下文包里的 dropped_refs / 目录行只给引用，需要细节时用本工具按需取回，"
                "用 range 与 budget 分页，避免整段拉回。"
            ),
            input_schema=CONTEXT_FETCH_SCHEMA,
            capability=Capability.FILE_READ,
            handler=context_fetch,
        ),
        ToolSpec(
            name="context.steps",
            title="列出 Run 的步骤目录",
            description=(
                "列出本 Run（或本 Agent 的前序 Run）的 step 目录：工具、状态、时间、摘要。"
                "失败步带 ! 标记。用它回看自己做过什么、接续上一轮；细节再用 context.fetch 取。"
            ),
            input_schema=CONTEXT_STEPS_SCHEMA,
            capability=Capability.FILE_READ,
            handler=context_steps,
        ),
        ToolSpec(
            name="workspace.read",
            title="读取工作区文件",
            description=(
                "读取工作区内某个文件的当前内容及其 sha256（只读，不落盘）。"
                '传入目录（含 "."）时返回该目录的条目清单，便于先看结构再读文件。'
            ),
            input_schema=WORKSPACE_READ_SCHEMA,
            capability=Capability.FILE_READ,
            handler=workspace_read,
        ),
        ToolSpec(
            name="workspace.diff",
            title="查看提案差异",
            description="查看某条提案的 unified diff 与增删行统计（提案尚未落盘，等人审）。",
            input_schema=WORKSPACE_DIFF_SCHEMA,
            capability=Capability.FILE_READ,
            handler=workspace_diff,
        ),
        ToolSpec(
            name="workspace.tree",
            title="查看目录地图",
            description=(
                "一次拿到最多 4 层的紧凑目录地图（路径 + 类型 + 大小），用于替代逐层读取目录。"
                "先看地图、再用 read_many 读一组文件，比逐个 workspace.read 少很多往返。"
            ),
            input_schema=WORKSPACE_TREE_SCHEMA,
            capability=Capability.FILE_READ,
            handler=workspace_tree,
        ),
        ToolSpec(
            name="workspace.read_many",
            title="批量读取文件",
            description=(
                "一次读取最多 10 个文件（或目录），单文件默认裁剪到 6000 字符、总计 30000 字符。"
                "探索阶段优先用它一次拿齐相关文件，避免一个文件一次调用。"
            ),
            input_schema=WORKSPACE_READ_MANY_SCHEMA,
            capability=Capability.FILE_READ,
            handler=workspace_read_many,
        ),
        ToolSpec(
            name="workspace.search",
            title="搜索工作区文本",
            description=(
                "按字面量或正则搜索工作区，返回文件、行号、列号和该行原文（最多 100 条）。"
                "不确定文件路径时先搜再读；密钥类文件会被过滤。"
            ),
            input_schema=WORKSPACE_SEARCH_SCHEMA,
            capability=Capability.FILE_READ,
            handler=workspace_search,
        ),
    )
