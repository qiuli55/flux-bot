"""设计工作室工具：让 Agent 的设计产出走 Luma Studio，而不是手写 HTML。

设计稿的权威存储在 Luma Studio（`settings.design_studio_url`，默认 127.0.0.1:8091）。
工具层只做三件事：把参数转成 Luma 的请求、把上游错误翻译成 Flux 领域错误、
把结果整理成 Agent 能直接读的结构。`design.share` 额外把成品写进当前任务对话，
手机端据此渲染可点开的原生画布卡片。

设计稿 id 规则与 Luma / Flux 代理保持一致：小写字母、数字与连字符。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any

import httpx

from flux.core.mcp.tools.base import (
    ToolContext,
    ToolSpec,
    optional_str,
    reject_unknown,
    require_str,
)
from flux.core.task_engine.repository import MessageDraft
from flux.enums import Capability, TaskMessageRole
from flux.errors import AIOSError, ConflictError, NotFoundError, ValidationError

_DESIGN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

#: 聊天卡片里回带的节点上限：与 Luma `design_summary` 同口径，控制消息体积。
_PREVIEW_NODE_LIMIT = 48


class DesignStudioUnavailable(AIOSError):
    """设计服务不可达或上游故障；Agent 可以稍后重试，不暴露内网地址。"""

    code = "design_studio_unavailable"
    http_status = 503


def _require_design_id(raw: Any) -> str:
    design_id = require_str({"design_id": raw}, "design_id")
    if not _DESIGN_ID_RE.match(design_id):
        raise ValidationError(f"设计稿 id 不合法：{design_id}", details={"param": "design_id"})
    return design_id


def _client(ctx: ToolContext) -> httpx.AsyncClient:
    """短连接客户端：与 Flux 代理同一份超时配置，测试可替换本函数。"""
    settings = ctx.container.settings
    timeout = httpx.Timeout(settings.design_studio_timeout_seconds, connect=3.0)
    return httpx.AsyncClient(
        base_url=settings.design_studio_url.rstrip("/"),
        timeout=timeout,
        # 同 designs 代理：本机地址不走系统代理
        trust_env=False,
    )


def _upstream_error(response: httpx.Response) -> AIOSError:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    message = str(payload.get("error") or "设计服务暂时不可用")
    if response.status_code == 404:
        return NotFoundError(message)
    if response.status_code == 409:
        return ConflictError(
            message,
            code="design_conflict",
            details={"version": payload.get("version")},
        )
    if response.status_code == 429:
        return AIOSError(message, code="rate_limited", http_status=429)
    if response.status_code >= 500:
        return DesignStudioUnavailable(message)
    return ValidationError(message, details={"upstream_status": response.status_code})


async def _request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        response = await client.request(method, path, json=payload)
    except httpx.HTTPError as exc:
        raise DesignStudioUnavailable("设计服务连接失败") from exc
    if response.status_code >= 400:
        raise _upstream_error(response)
    try:
        body = response.json()
    except ValueError as exc:
        raise DesignStudioUnavailable("设计服务返回了非 JSON 响应") from exc
    if not isinstance(body, dict) or not body.get("ok"):
        raise DesignStudioUnavailable("设计服务返回了异常响应")
    return body


def _design_meta(params: dict[str, Any]) -> dict[str, Any]:
    """把可选的元数据字段收成 Luma 接受的形状；未传的键不出现。"""
    meta: dict[str, Any] = {}
    for key in ("name", "project", "project_id", "description", "status", "bg", "file"):
        value = optional_str(params, key)
        if value is not None:
            meta[key] = value
    return meta


def _screens(params: dict[str, Any]) -> list[dict[str, Any]] | None:
    screens = params.get("screens")
    if screens is None:
        return None
    if not isinstance(screens, list) or not screens:
        raise ValidationError("screens 必须是非空数组（1–20 个画板）", details={"param": "screens"})
    if len(screens) > 20:
        raise ValidationError("screens 最多 20 个画板", details={"param": "screens"})
    for screen in screens:
        if not isinstance(screen, dict):
            raise ValidationError("每个画板必须是对象", details={"param": "screens"})
        for key in ("id", "name"):
            if not isinstance(screen.get(key), str) or not screen.get(key, "").strip():
                raise ValidationError(
                    f"画板缺少字段 {key}", details={"param": "screens", "field": key}
                )
        for key in ("w", "h"):
            if not isinstance(screen.get(key), (int, float)):
                raise ValidationError(
                    f"画板缺少尺寸字段 {key}", details={"param": "screens", "field": key}
                )
        if not isinstance(screen.get("nodes", []), list):
            raise ValidationError(
                f"画板 {screen.get('id')} 的 nodes 必须是数组", details={"param": "screens"}
            )
    return screens


def _layer_count(nodes: list[Any]) -> int:
    total = 0
    for node in nodes:
        if not isinstance(node, dict):
            continue
        total += 1
        children = node.get("children")
        if isinstance(children, list):
            total += _layer_count(children)
    return total


def _summary(design: dict[str, Any]) -> dict[str, Any]:
    screens = design.get("screens") if isinstance(design.get("screens"), list) else []
    layers = sum(
        _layer_count(screen.get("nodes") or []) for screen in screens if isinstance(screen, dict)
    )
    return {
        "design_id": design.get("id"),
        "name": design.get("name"),
        "project": design.get("project"),
        "project_id": design.get("project_id"),
        "description": design.get("description"),
        "status": design.get("status"),
        "bg": design.get("bg"),
        "version": design.get("version"),
        "updated_at": design.get("updated_at"),
        "screen_count": len(screens),
        "layer_count": layers,
    }


def _preview(design: dict[str, Any]) -> dict[str, Any] | None:
    """卡片缩略图数据：第一屏的尺寸、底色与前几十个节点。"""
    screens = design.get("screens")
    if not isinstance(screens, list) or not screens:
        return None
    first = screens[0]
    if not isinstance(first, dict):
        return None
    nodes = first.get("nodes")
    return {
        "w": first.get("w") or 390,
        "h": first.get("h") or 844,
        "bg": design.get("bg"),
        "nodes": (nodes if isinstance(nodes, list) else [])[:_PREVIEW_NODE_LIMIT],
    }


async def _project_from_run(ctx: ToolContext) -> str | None:
    run_id = getattr(ctx.identity, "run_id", None)
    if not run_id:
        return None
    run = await ctx.container.run_repo.get(run_id)
    if run is None or run.task_id is None:
        return None
    try:
        task = await ctx.container.task_repo.get(run.task_id)
    except NotFoundError:
        return None
    return str(task.project_id) if task.project_id else None


# ---------------------------------------------------------------- 工具实现


async def design_list(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"project_id"})
    project_id = optional_str(params, "project_id")
    if project_id is None:
        project_id = await _project_from_run(ctx)
    async with _client(ctx) as client:
        body = await _request(client, "GET", "/api/designs")
    designs = body.get("designs") or []
    if not isinstance(designs, list):
        raise DesignStudioUnavailable("设计服务返回了异常列表")
    if project_id is not None:
        designs = [
            item
            for item in designs
            if isinstance(item, dict) and item.get("project_id") == project_id
        ]
    return {"count": len(designs), "project_id": project_id, "designs": designs}


async def design_get(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"design_id"})
    design_id = _require_design_id(params.get("design_id"))
    async with _client(ctx) as client:
        body = await _request(client, "GET", f"/api/designs/{design_id}")
    design = body.get("design")
    if not isinstance(design, dict):
        raise DesignStudioUnavailable("设计服务返回了异常设计稿")
    return {"design": design, "summary": _summary(design)}


async def design_create(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(
        params,
        {"name", "project", "project_id", "description", "status", "bg", "file", "screens"},
    )
    name = require_str(params, "name")
    screens = _screens(params)
    meta = _design_meta(params)
    meta["name"] = name
    async with _client(ctx) as client:
        # Luma 的 POST 只能建空白稿：先建，再整份写入画板，避免留下半成品
        created = await _request(client, "POST", "/api/designs", meta)
        blank = created.get("design")
        if not isinstance(blank, dict) or not blank.get("id"):
            raise DesignStudioUnavailable("设计服务没有返回新建设计稿")
        design_id = str(blank["id"])
        if screens is None:
            return {"created": True, "design": blank, "summary": _summary(blank)}
        saved = await _request(
            client,
            "PUT",
            f"/api/designs/{design_id}",
            {
                "base_version": blank.get("version") or 1,
                "design": {**meta, "screens": screens},
            },
        )
    design = saved.get("design")
    if not isinstance(design, dict):
        raise DesignStudioUnavailable("设计服务返回了异常设计稿")
    return {"created": True, "design": design, "summary": _summary(design)}


async def design_update(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(
        params,
        {
            "design_id",
            "base_version",
            "name",
            "project",
            "project_id",
            "description",
            "status",
            "bg",
            "file",
            "screens",
        },
    )
    design_id = _require_design_id(params.get("design_id"))
    screens = _screens(params)
    meta = _design_meta(params)
    base_version = params.get("base_version")
    if base_version is not None and not isinstance(base_version, int):
        raise ValidationError("base_version 必须是整数", details={"param": "base_version"})
    async with _client(ctx) as client:
        current = await _request(client, "GET", f"/api/designs/{design_id}")
        design = current.get("design")
        if not isinstance(design, dict):
            raise DesignStudioUnavailable("设计服务返回了异常设计稿")
        merged = {
            "name": meta.get("name") or design.get("name"),
            "project": meta.get("project") or design.get("project"),
            "project_id": meta.get("project_id") or design.get("project_id"),
            "description": meta.get("description") or design.get("description"),
            "status": meta.get("status") or design.get("status"),
            "bg": meta.get("bg") or design.get("bg"),
            "file": meta.get("file") or design.get("file"),
            "screens": screens if screens is not None else design.get("screens") or [],
        }
        saved = await _request(
            client,
            "PUT",
            f"/api/designs/{design_id}",
            {
                "base_version": base_version if base_version is not None else design.get("version"),
                "design": merged,
            },
        )
    updated = saved.get("design")
    if not isinstance(updated, dict):
        raise DesignStudioUnavailable("设计服务返回了异常设计稿")
    return {"created": False, "design": updated, "summary": _summary(updated)}


# ---------------------------------------------------------------- AI 局部修改


def _find_node(nodes: list[Any], node_id: str) -> tuple[list[Any], int, dict[str, Any]] | None:
    """在图层树里定位节点，返回 (父数组, 下标, 节点)。"""
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        if node.get("id") == node_id:
            return nodes, index, node
        children = node.get("children")
        if isinstance(children, list):
            hit = _find_node(children, node_id)
            if hit is not None:
                return hit
    return None


def _pick_screen(design: dict[str, Any], params: dict[str, Any]) -> tuple[dict[str, Any], int]:
    screens = design.get("screens")
    if not isinstance(screens, list) or not screens:
        raise ValidationError("设计稿没有任何画板，AI 无从下手")
    screen_id = optional_str(params, "screen_id")
    if screen_id is not None:
        for index, screen in enumerate(screens):
            if isinstance(screen, dict) and screen.get("id") == screen_id:
                return screen, index
        raise ValidationError(f"画板不存在：{screen_id}", details={"param": "screen_id"})
    index = params.get("screen_index", 0)
    if isinstance(index, bool) or not isinstance(index, int):
        raise ValidationError("screen_index 必须是整数", details={"param": "screen_index"})
    if index < 0 or index >= len(screens):
        raise ValidationError(
            f"screen_index 越界：{index}（共 {len(screens)} 块画板）",
            details={"param": "screen_index"},
        )
    screen = screens[index]
    if not isinstance(screen, dict):
        raise DesignStudioUnavailable("设计服务返回了异常画板")
    return screen, index


def _selected_ids(params: dict[str, Any]) -> list[str]:
    raw = params.get("selected_ids")
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 40:
        raise ValidationError(
            "selected_ids 必须是最多 40 个 id 的数组", details={"param": "selected_ids"}
        )
    ids: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(
                "selected_ids 里的每一项都必须是非空字符串",
                details={"param": "selected_ids"},
            )
        ids.append(item.strip())
    return ids


_AI_MODES = {"auto", "node", "multi", "screen"}


async def design_ai(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    """对设计稿做局部 AI 修改：只改当前画板，服务端应用 ops 并落盘。"""
    reject_unknown(
        params,
        {
            "design_id",
            "instruction",
            "screen_index",
            "screen_id",
            "selected_ids",
            "mode",
            "base_version",
        },
    )
    design_id = _require_design_id(params.get("design_id"))
    instruction = require_str(params, "instruction").strip()
    if len(instruction) > 400:
        raise ValidationError("instruction 太长，控制在 400 字内", details={"param": "instruction"})
    selected_ids = _selected_ids(params)
    mode = optional_str(params, "mode") or "auto"
    if mode not in _AI_MODES:
        raise ValidationError(f"mode 只能是 {sorted(_AI_MODES)}", details={"param": "mode"})
    base_version = params.get("base_version")
    if base_version is not None and (
        isinstance(base_version, bool) or not isinstance(base_version, int)
    ):
        raise ValidationError("base_version 必须是整数", details={"param": "base_version"})

    async with _client(ctx) as client:
        current = await _request(client, "GET", f"/api/designs/{design_id}")
        design = current.get("design")
        if not isinstance(design, dict):
            raise DesignStudioUnavailable("设计服务返回了异常设计稿")
        screen, screen_index = _pick_screen(design, params)
        nodes = screen.get("nodes")
        nodes = nodes if isinstance(nodes, list) else []
        selected: list[dict[str, Any]] = []
        missing: list[str] = []
        for node_id in selected_ids:
            hit = _find_node(nodes, node_id)
            if hit is None:
                missing.append(node_id)
            else:
                selected.append(hit[2])
        if missing:
            raise ValidationError(
                f"选中的图层不在该画板：{'、'.join(missing[:5])}",
                details={"param": "selected_ids"},
            )
        if mode == "auto":
            mode = "screen" if not selected_ids else ("multi" if len(selected_ids) > 1 else "node")
        if mode != "screen" and not selected:
            raise ValidationError(
                "mode=node/multi 时必须传 selected_ids", details={"param": "selected_ids"}
            )
        if not nodes:
            raise ValidationError("该画板还没有图层，先用 design.update 搭出版式再让 AI 调")

        try:
            asked = await _request(
                client,
                "POST",
                "/api/ask",
                {
                    "prompt": instruction,
                    "mode": mode,
                    "nodes": selected,
                    # design id 供 Luma 建立多轮修改记录；bg 供其按底色选择深/浅基调
                    "design": design_id,
                    "screen": {
                        "id": screen.get("id"),
                        "name": screen.get("name") or "",
                        "w": screen.get("w") or 390,
                        "h": screen.get("h") or 844,
                        "bg": design.get("bg") or "",
                        "nodes": nodes,
                    },
                },
            )
        except DesignStudioUnavailable as exc:
            # /api/ask 的 502 多半是「AI 连续两次没给出可应用修改」这类可重试失败，
            # 不是设计服务宕机：翻译成 ai_step_failed，让 Agent 看到原因并重试。
            raise AIOSError(str(exc), code="ai_step_failed", http_status=502) from exc
        ops = asked.get("ops")
        if not isinstance(ops, list) or not ops:
            raise ValidationError("AI 没有给出可应用的修改，换个说法或缩小改动范围再试")
        # 提交意图而不是整稿：服务端在最新稿上重放 ops，人同时在改也不会被覆盖
        committed = await _request(
            client,
            "POST",
            f"/api/designs/{design_id}/commits",
            {
                "author": "flux-agent",
                "role": "agent",
                "label": instruction[:120],
                "screen": screen.get("id") or "",
                "base_version": base_version if base_version is not None else design.get("version"),
                "ops": ops,
            },
        )
    updated = committed.get("design")
    if not isinstance(updated, dict):
        raise DesignStudioUnavailable("设计服务返回了异常设计稿")
    applied = committed.get("applied")
    applied = applied if isinstance(applied, int) and not isinstance(applied, bool) else 0
    skipped = committed.get("skipped") if isinstance(committed.get("skipped"), list) else []
    deferred = committed.get("deferred") if isinstance(committed.get("deferred"), list) else []
    if applied == 0:
        raise ValidationError(
            "AI 返回的修改无法应用到当前画板，请重试",
            details={"skipped": skipped[:5], "deferred": deferred[:5]},
        )
    return {
        "changed": True,
        "design_id": design_id,
        "screen_index": screen_index,
        "screen_id": screen.get("id"),
        "version": updated.get("version"),
        "applied": applied,
        "skipped": skipped[:5],
        "deferred": deferred[:5],
        "rebased": bool(committed.get("rebased")),
        "ops": ops,
        "reply": asked.get("reply"),
        "mode": asked.get("mode") or mode,
        "model": asked.get("model"),
        "summary": _summary(updated),
        "design": updated,
    }


async def design_revisions(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"design_id"})
    design_id = _require_design_id(params.get("design_id"))
    async with _client(ctx) as client:
        body = await _request(client, "GET", f"/api/designs/{design_id}/revisions")
    revisions = body.get("revisions") or []
    if not isinstance(revisions, list):
        raise DesignStudioUnavailable("设计服务返回了异常历史列表")
    return {"design_id": design_id, "count": len(revisions), "revisions": revisions}


async def design_revert(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"design_id", "version", "base_version"})
    design_id = _require_design_id(params.get("design_id"))
    version = params.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise ValidationError("version 必须是正整数", details={"param": "version"})
    base_version = params.get("base_version")
    if base_version is not None and (
        isinstance(base_version, bool) or not isinstance(base_version, int)
    ):
        raise ValidationError("base_version 必须是整数", details={"param": "base_version"})
    payload: dict[str, Any] = {"version": version}
    if base_version is not None:
        payload["base_version"] = base_version
    async with _client(ctx) as client:
        restored = await _request(client, "POST", f"/api/designs/{design_id}/revert", payload)
    design = restored.get("design")
    if not isinstance(design, dict):
        raise DesignStudioUnavailable("设计服务返回了异常设计稿")
    return {
        "reverted": True,
        "design": design,
        "summary": _summary(design),
        "version": design.get("version"),
    }


# ---------------------------------------------------------------- 组件 / 模板
#
# 组件库与模板库由 Luma Studio 维护：组件是参数化节点子树，模板是整屏画板。
# 绑定（把蓝图 + 参数展开成节点树）只在服务端做一份，人和 Agent 共用同一结果，
# 所以这里不复制任何占位符逻辑，只负责「取目录 → 绑定 → 生成 ops → 走 commits」。

_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
#: 实例根节点 id 即绑定前缀：服务端按它确定性生成整棵子树的 id。
_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")
_TEMPLATE_MODES = {"new-screen", "replace-screen"}
_COMPONENT_SCOPES = {"instance", "all"}


def _require_asset_id(raw: Any) -> str:
    asset_id = require_str({"asset_id": raw}, "asset_id")
    if not _ASSET_ID_RE.match(asset_id):
        raise ValidationError(f"组件 id 不合法：{asset_id}", details={"param": "asset_id"})
    return asset_id


def _instance_prefix(raw: Any) -> str:
    """实例根 id：Agent 可指定（≤20 字符），否则按 inst-xxxxxx 生成。"""
    if raw is None:
        return "inst-" + uuid.uuid4().hex[:6]
    if not isinstance(raw, str) or not _INSTANCE_ID_RE.match(raw.strip()):
        raise ValidationError(
            "node_id 只允许字母/数字/下划线/连字符，最长 20 字符",
            details={"param": "node_id"},
        )
    return raw.strip()


def _params_object(raw: Any) -> dict[str, Any]:
    """组件参数覆盖项：键是组件声明的参数名，未知键由服务端忽略。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValidationError("params 必须是对象（参数名 → 值）", details={"param": "params"})
    return dict(raw)


def _size(raw: Any, fallback: float = 0.0) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return fallback
    return float(raw)


def _round(raw: Any, fallback: float = 0.0) -> int:
    return int(round(_size(raw, fallback)))


def _index_param(raw: Any, upper: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValidationError("index 必须是整数", details={"param": "index"})
    return max(0, min(raw, upper))


def _stack_y(nodes: list[Any]) -> int:
    """新组件的默认纵坐标：叠在现有顶层图层下方，至少留 96 的头部空间。"""
    bottom = 96
    for node in nodes:
        if not isinstance(node, dict) or node.get("hidden"):
            continue
        bottom = max(bottom, _round(_size(node.get("y")) + _size(node.get("h")) + 16))
    return bottom


def _profile(asset: dict[str, Any], kind: str) -> dict[str, Any]:
    """目录里的一项 → Agent 可读摘要（不含蓝图节点树，控制上下文体积）。"""
    params: list[dict[str, Any]] = []
    for spec in asset.get("params") or []:
        if isinstance(spec, dict) and isinstance(spec.get("key"), str):
            params.append(
                {
                    "key": spec["key"],
                    "label": spec.get("label") or spec["key"],
                    "type": spec.get("type") or "text",
                    "default": spec.get("default"),
                }
            )
    profile: dict[str, Any] = {
        "asset_id": asset.get("id"),
        "name": asset.get("name"),
        "group": asset.get("group"),
        "description": asset.get("desc"),
        "params": params,
    }
    if kind == "template":
        screen = asset.get("screen") if isinstance(asset.get("screen"), dict) else {}
        profile["w"] = screen.get("w")
        profile["h"] = screen.get("h")
        profile["layer_count"] = _layer_count(screen.get("nodes") or [])
    else:
        node = asset.get("node") if isinstance(asset.get("node"), dict) else {}
        profile["layer_count"] = _layer_count([node]) if node else 0
        variants = [
            {"id": item["id"], "name": item.get("name") or item["id"]}
            for item in asset.get("variants") or []
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        if variants:
            profile["variants"] = variants
            profile["default_variant"] = variants[0]["id"]
    return profile


async def _bind_asset(
    client: httpx.AsyncClient,
    asset_id: str,
    kind: str,
    params: dict[str, Any],
    prefix: str,
    variant: str | None = None,
) -> dict[str, Any]:
    """调用服务端 binder：返回绑定结果、生效参数与生效变体。"""
    payload: dict[str, Any] = {
        "asset_id": asset_id,
        "kind": kind,
        "params": params,
        "prefix": prefix,
    }
    if variant:
        payload["variant"] = variant
    body = await _request(client, "POST", "/api/components/render", payload)
    bound = body.get("node") if kind == "component" else body.get("screen")
    if not isinstance(bound, dict):
        raise DesignStudioUnavailable("设计服务返回了异常组件实例")
    merged = body.get("params") if isinstance(body.get("params"), dict) else params
    return {"bound": bound, "params": merged, "variant": body.get("variant")}


def _find_node_anywhere(
    design: dict[str, Any], node_id: str
) -> tuple[dict[str, Any], tuple[list[Any], int, dict[str, Any]]] | None:
    screens = design.get("screens")
    if not isinstance(screens, list):
        return None
    for screen in screens:
        if not isinstance(screen, dict):
            continue
        hit = _find_node(screen.get("nodes") or [], node_id)
        if hit is not None:
            return screen, hit
    return None


def _custom_params(
    origin: dict[str, Any], fallback: dict[str, Any] | None = None
) -> dict[str, Any]:
    """实例上用户真正改过的参数（与 origin.defaults 不同者）。

    切变体时只携带这些值，其余交给新变体默认，避免旧变体默认覆盖新变体默认。
    老实例没有 defaults 时用组件级默认（fallback）兜底，再不行则全部视为默认。
    """
    params = origin.get("params") if isinstance(origin.get("params"), dict) else {}
    defaults = origin.get("defaults")
    if not isinstance(defaults, dict):
        defaults = fallback if isinstance(fallback, dict) else params
    return {key: value for key, value in params.items() if value != defaults.get(key)}


async def _asset_param_defaults(client: httpx.AsyncClient, asset_id: str) -> dict[str, Any]:
    """组件级参数默认值（用于兼容没有 origin.defaults 的老实例）。"""
    try:
        body = await _request(client, "GET", "/api/components")
    except Exception:
        return {}
    for item in body.get("components") or []:
        if not isinstance(item, dict) or item.get("id") != asset_id:
            continue
        out: dict[str, Any] = {}
        for spec in item.get("params") or []:
            if isinstance(spec, dict) and isinstance(spec.get("key"), str):
                out[spec["key"]] = spec.get("default")
        return out
    return {}


def _instances_of(design: dict[str, Any], asset_id: str) -> list[str]:
    """收集全稿同源实例的根 id（含嵌套图层）。"""
    ids: list[str] = []

    def walk(nodes: list[Any]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            origin = node.get("origin")
            if isinstance(origin, dict) and origin.get("assetId") == asset_id:
                ids.append(str(node.get("id")))
            children = node.get("children")
            if isinstance(children, list):
                walk(children)

    for screen in design.get("screens") or []:
        if isinstance(screen, dict):
            nodes = screen.get("nodes")
            walk(nodes if isinstance(nodes, list) else [])
    return ids


def _collect_nodes(node: Any, out: dict[str, dict[str, Any]], order: list[str]) -> None:
    if not isinstance(node, dict):
        return
    node_id = node.get("id")
    if isinstance(node_id, str):
        out[node_id] = node
        order.append(node_id)
    children = node.get("children")
    if isinstance(children, list):
        for child in children:
            _collect_nodes(child, out, order)


def _locate_child(node: dict[str, Any], node_id: str) -> tuple[str | None, int] | None:
    children = node.get("children")
    if not isinstance(children, list):
        return None
    for index, child in enumerate(children):
        if not isinstance(child, dict):
            continue
        if child.get("id") == node_id:
            return node.get("id"), index
        hit = _locate_child(child, node_id)
        if hit is not None:
            return hit
    return None


def _subtree_ops(
    old_root: dict[str, Any], fresh_root: dict[str, Any], screen_id: str
) -> list[dict[str, Any]]:
    """重建前后对比：删多余节点 → 加新节点 → 对同 id 节点只 set 变化字段。

    id 由 prefix 确定性生成，所以常规调参只会产生少量 set；只有蓝图结构变了才会
    出现 add / remove。
    """
    old_nodes: dict[str, dict[str, Any]] = {}
    old_order: list[str] = []
    _collect_nodes(old_root, old_nodes, old_order)
    new_nodes: dict[str, dict[str, Any]] = {}
    new_order: list[str] = []
    _collect_nodes(fresh_root, new_nodes, new_order)

    ops: list[dict[str, Any]] = []
    for node_id in reversed(old_order):
        if node_id not in new_nodes:
            ops.append({"op": "remove", "screen": screen_id, "target": node_id})
    for node_id in new_order:
        if node_id in old_nodes:
            continue
        parent_id, at = _locate_child(fresh_root, node_id) or (None, 0)
        # 父节点本身是新增的：整棵子树随父节点的 add 一起进去，不重复添加
        if parent_id is not None and parent_id not in old_nodes:
            continue
        ops.append(
            {
                "op": "add",
                "screen": screen_id,
                "target": parent_id or "@screen",
                "index": at,
                "node": new_nodes[node_id],
            }
        )
    for node_id in new_order:
        old = old_nodes.get(node_id)
        if old is None:
            continue
        fresh = new_nodes[node_id]
        patch: dict[str, Any] = {}
        for key, value in fresh.items():
            if key in ("id", "type", "children"):
                continue
            if old.get(key) != value:
                patch[key] = value
        if patch:
            ops.append({"op": "set", "screen": screen_id, "target": node_id, "set": patch})
    return ops


async def _commit(
    client: httpx.AsyncClient,
    design_id: str,
    ops: list[dict[str, Any]],
    *,
    label: str,
    screen_hint: str,
    base_version: Any,
) -> dict[str, Any]:
    """提交意图而不是整稿：服务端在最新稿上重放，人同时在改也不会被覆盖。"""
    if not ops:
        raise ValidationError("没有产生任何改动")
    if len(ops) > 400:
        raise ValidationError(
            f"本次改动产生 {len(ops)} 条操作，超过单次上限 400；请拆成多次调用或缩小范围",
            details={"ops": len(ops)},
        )
    body = await _request(
        client,
        "POST",
        f"/api/designs/{design_id}/commits",
        {
            "author": "flux-agent",
            "role": "agent",
            "label": label[:120],
            "screen": screen_hint,
            "base_version": base_version,
            "ops": ops,
        },
    )
    design = body.get("design")
    if not isinstance(design, dict):
        raise DesignStudioUnavailable("设计服务返回了异常设计稿")
    applied = body.get("applied")
    applied = applied if isinstance(applied, int) and not isinstance(applied, bool) else 0
    skipped = body.get("skipped") if isinstance(body.get("skipped"), list) else []
    deferred = body.get("deferred") if isinstance(body.get("deferred"), list) else []
    if applied == 0:
        if deferred:
            raise ConflictError(
                "设计稿正被人工编辑（相关图层被软锁），稍后重试",
                code="design_locked",
                details={"deferred": deferred[:10], "skipped": skipped[:10]},
            )
        raise ValidationError("改动没有应用到当前设计稿", details={"skipped": skipped[:10]})
    return {
        "design": design,
        "version": design.get("version"),
        "applied": applied,
        "skipped": skipped[:10],
        "deferred": deferred[:10],
        "rebased": bool(body.get("rebased")),
    }


async def _load_design(client: httpx.AsyncClient, design_id: str) -> dict[str, Any]:
    body = await _request(client, "GET", f"/api/designs/{design_id}")
    design = body.get("design")
    if not isinstance(design, dict):
        raise DesignStudioUnavailable("设计服务返回了异常设计稿")
    return design


def _base_version(params: dict[str, Any], design: dict[str, Any]) -> Any:
    raw = params.get("base_version")
    if raw is not None and (isinstance(raw, bool) or not isinstance(raw, int)):
        raise ValidationError("base_version 必须是整数", details={"param": "base_version"})
    return raw if raw is not None else design.get("version")


async def design_component_list(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    """列出组件与模板（只读）：只回摘要，蓝图节点树留在服务端。"""
    reject_unknown(params, {"kind", "group"})
    kind = optional_str(params, "kind")
    if kind is not None and kind not in ("component", "template"):
        raise ValidationError("kind 只能是 component 或 template", details={"param": "kind"})
    group = optional_str(params, "group")
    async with _client(ctx) as client:
        body = await _request(client, "GET", "/api/components")
    result: dict[str, Any] = {
        "version": body.get("version"),
        "updated_at": body.get("updated_at"),
    }
    count = 0
    for key, item_kind in (("components", "component"), ("templates", "template")):
        if kind is not None and kind != item_kind:
            continue
        items = [item for item in body.get(key) or [] if isinstance(item, dict)]
        if group is not None:
            items = [item for item in items if item.get("group") == group]
        result[key] = [_profile(item, item_kind) for item in items]
        count += len(items)
    result["count"] = count
    result["hint"] = (
        "组件用 design.component_insert 插入画板并保留可调参数；"
        "模板用 design.template_apply 新建画板或替换现有画板。"
    )
    return result


async def design_component_insert(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(
        params,
        {
            "design_id",
            "asset_id",
            "screen_id",
            "screen_index",
            "params",
            "node_id",
            "x",
            "y",
            "index",
            "variant",
            "base_version",
        },
    )
    design_id = _require_design_id(params.get("design_id"))
    asset_id = _require_asset_id(params.get("asset_id"))
    overrides = _params_object(params.get("params"))
    variant = optional_str(params, "variant")
    prefix = _instance_prefix(params.get("node_id"))

    async with _client(ctx) as client:
        design = await _load_design(client, design_id)
        screen, _ = _pick_screen(design, params)
        screen_id = str(screen.get("id"))
        nodes = screen.get("nodes") if isinstance(screen.get("nodes"), list) else []

        bound = await _bind_asset(client, asset_id, "component", overrides, prefix, variant)
        node = bound["bound"]

        width = _size(screen.get("w"))
        height = _size(screen.get("h"))
        node_w = _size(node.get("w"))
        node_h = _size(node.get("h"))
        if params.get("x") is not None:
            node["x"] = _round(params.get("x"))
        else:
            node["x"] = max(8, int(round((width - node_w) / 2)))
        if params.get("y") is not None:
            node["y"] = _round(params.get("y"))
        else:
            node["y"] = min(_stack_y(nodes), max(96, int(height - node_h - 24)))
        at = (
            len(nodes)
            if params.get("index") is None
            else _index_param(params.get("index"), len(nodes))
        )

        committed = await _commit(
            client,
            design_id,
            [{"op": "add", "screen": screen_id, "target": "@screen", "index": at, "node": node}],
            label=f"插入组件 {asset_id}",
            screen_hint=screen_id,
            base_version=_base_version(params, design),
        )
    updated = committed["design"]
    return {
        "inserted": True,
        "design_id": design_id,
        "screen_id": screen_id,
        "node_id": node.get("id"),
        "asset_id": asset_id,
        "params": bound["params"],
        "variant": bound["variant"],
        "x": node.get("x"),
        "y": node.get("y"),
        "version": committed["version"],
        "applied": committed["applied"],
        "skipped": committed["skipped"],
        "deferred": committed["deferred"],
        "rebased": committed["rebased"],
        "summary": _summary(updated),
    }


async def design_component_update(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(
        params,
        {"design_id", "node_id", "params", "variant", "scope", "label", "base_version"},
    )
    design_id = _require_design_id(params.get("design_id"))
    node_id = require_str(params, "node_id")
    overrides = _params_object(params.get("params"))
    variant = optional_str(params, "variant")
    if not overrides and variant is None:
        raise ValidationError("params 与 variant 至少要给一个", details={"param": "params"})
    scope = optional_str(params, "scope") or "instance"
    if scope not in _COMPONENT_SCOPES:
        raise ValidationError(
            f"scope 只能是 {sorted(_COMPONENT_SCOPES)}", details={"param": "scope"}
        )
    label = optional_str(params, "label") or f"更新组件实例 {node_id}"

    async with _client(ctx) as client:
        design = await _load_design(client, design_id)
        found = _find_node_anywhere(design, node_id)
        if found is None:
            raise NotFoundError(f"图层不存在：{node_id}", details={"param": "node_id"})
        origin = found[1][2].get("origin")
        if not isinstance(origin, dict) or not origin.get("assetId"):
            raise ValidationError(
                "该图层不是组件实例（没有 origin），无法按参数更新",
                details={"param": "node_id"},
            )
        asset_id = str(origin["assetId"])
        own = origin.get("params") if isinstance(origin.get("params"), dict) else {}
        # 老实例没有 defaults：取组件级默认，避免旧变体默认被当成用户自定义
        legacy_defaults = None
        if not isinstance(origin.get("defaults"), dict):
            legacy_defaults = await _asset_param_defaults(client, asset_id)
        # 只把「用户自定义 + 本次覆盖」传给 binder，剩余默认值由目标变体决定
        merged = {**_custom_params(origin, legacy_defaults), **overrides}
        effective: dict[str, Any] = {**own, **overrides}
        targets = [node_id] if scope == "instance" else _instances_of(design, asset_id)

        ops: list[dict[str, Any]] = []
        updated_ids: list[str] = []
        applied_variant: Any = variant
        for target_id in targets:
            hit = _find_node_anywhere(design, target_id)
            if hit is None:
                continue
            target_screen, (_, _, old_node) = hit
            target_origin = old_node.get("origin")
            if not isinstance(target_origin, dict) or not target_origin.get("assetId"):
                continue
            # scope=all：把源实例的自定义参数铺到所有同源实例，各自保留自己的变体
            target_custom = _custom_params(target_origin, legacy_defaults)
            wanted = (
                {**target_custom, **merged} if scope == "all" else {**target_custom, **overrides}
            )
            target_variant = variant
            if target_variant is None and isinstance(target_origin.get("variant"), str):
                target_variant = target_origin["variant"]
            bound = await _bind_asset(
                client,
                str(target_origin["assetId"]),
                "component",
                wanted,
                target_id,
                target_variant,
            )
            applied_variant = bound["variant"]
            effective = bound["params"]
            fresh = bound["bound"]
            fresh["x"] = _size(old_node.get("x"))
            fresh["y"] = _size(old_node.get("y"))
            if isinstance(old_node.get("name"), str) and old_node["name"]:
                fresh["name"] = old_node["name"]
            for key in ("hidden", "locked", "rotation", "opacity"):
                if key in old_node:
                    fresh[key] = old_node[key]
            ops.extend(_subtree_ops(old_node, fresh, str(target_screen.get("id") or "")))
            updated_ids.append(target_id)

        if not ops:
            return {
                "changed": False,
                "design_id": design_id,
                "node_id": node_id,
                "asset_id": asset_id,
                "params": effective,
                "variant": applied_variant,
                "updated": updated_ids,
                "version": design.get("version"),
                "note": "参数与当前实例一致，没有产生改动",
            }
        committed = await _commit(
            client,
            design_id,
            ops,
            label=label,
            screen_hint=str(found[0].get("id") or ""),
            base_version=_base_version(params, design),
        )
    return {
        "changed": True,
        "design_id": design_id,
        "node_id": node_id,
        "asset_id": asset_id,
        "scope": scope,
        "params": effective,
        "variant": applied_variant,
        "updated": updated_ids,
        "version": committed["version"],
        "applied": committed["applied"],
        "skipped": committed["skipped"],
        "deferred": committed["deferred"],
        "rebased": committed["rebased"],
        "summary": _summary(committed["design"]),
    }


async def design_template_apply(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(
        params,
        {
            "design_id",
            "asset_id",
            "mode",
            "screen_id",
            "screen_index",
            "params",
            "name",
            "index",
            "base_version",
        },
    )
    design_id = _require_design_id(params.get("design_id"))
    asset_id = _require_asset_id(params.get("asset_id"))
    mode = optional_str(params, "mode") or "new-screen"
    if mode not in _TEMPLATE_MODES:
        raise ValidationError(f"mode 只能是 {sorted(_TEMPLATE_MODES)}", details={"param": "mode"})
    overrides = _params_object(params.get("params"))
    name_override = optional_str(params, "name")

    async with _client(ctx) as client:
        design = await _load_design(client, design_id)
        screens = design.get("screens") if isinstance(design.get("screens"), list) else []

        if mode == "new-screen":
            if len(screens) >= 20:
                raise ValidationError(
                    "设计稿已有 20 块画板，不能再新建；可改用 replace-screen 覆盖现有画板",
                    details={"param": "mode"},
                )
            prefix = "tpl-" + uuid.uuid4().hex[:6]
            bound = await _bind_asset(client, asset_id, "template", overrides, prefix)
            fresh = bound["bound"]
            if name_override:
                fresh["name"] = name_override[:60]
            last = screens[-1] if screens and isinstance(screens[-1], dict) else None
            if last is not None:
                fresh["x"] = _round(_size(last.get("x")) + _size(last.get("w")) + 120)
                fresh["y"] = _round(_size(last.get("y")), 40)
            at = (
                len(screens)
                if params.get("index") is None
                else _index_param(params.get("index"), len(screens))
            )
            screen_id = str(fresh.get("id"))
            ops = [{"op": "add-screen", "screen": screen_id, "index": at, "payload": fresh}]
        else:
            screen, _ = _pick_screen(design, params)
            screen_id = str(screen.get("id"))
            # 画板 id 通常超过 20 字符（实例前缀上限），按它派生一个稳定短前缀
            prefix = "tpl-" + hashlib.sha1(screen_id.encode()).hexdigest()[:8]
            bound = await _bind_asset(client, asset_id, "template", overrides, prefix)
            fresh = bound["bound"]
            fresh_name = (name_override or fresh.get("name") or screen.get("name") or "画板")[:60]
            old_nodes = screen.get("nodes") if isinstance(screen.get("nodes"), list) else []
            ops = []
            patch: dict[str, Any] = {}
            if fresh_name and fresh_name != screen.get("name"):
                patch["name"] = fresh_name
            if isinstance(fresh.get("bg"), str) and fresh["bg"] and fresh["bg"] != screen.get("bg"):
                patch["bg"] = fresh["bg"]
            if patch:
                ops.append({"op": "set-screen", "target": screen_id, "set": patch})
            # 先摘掉旧的顶层图层（子节点随父一起删），再按顺序铺新的顶层图层
            for node in reversed([item for item in old_nodes if isinstance(item, dict)]):
                if isinstance(node.get("id"), str):
                    ops.append({"op": "remove", "screen": screen_id, "target": node["id"]})
            for index, node in enumerate(fresh.get("nodes") or []):
                ops.append(
                    {
                        "op": "add",
                        "screen": screen_id,
                        "target": "@screen",
                        "index": index,
                        "node": node,
                    }
                )

        committed = await _commit(
            client,
            design_id,
            ops,
            label=f"{'新建' if mode == 'new-screen' else '替换'}画板：{asset_id}",
            screen_hint=screen_id,
            base_version=_base_version(params, design),
        )
    updated = committed["design"]
    return {
        "applied_template": True,
        "mode": mode,
        "design_id": design_id,
        "screen_id": screen_id,
        "asset_id": asset_id,
        "params": bound["params"],
        "version": committed["version"],
        "applied": committed["applied"],
        "skipped": committed["skipped"],
        "deferred": committed["deferred"],
        "rebased": committed["rebased"],
        "summary": _summary(updated),
    }


async def _task_for_run(ctx: ToolContext) -> uuid.UUID:
    run_id = getattr(ctx.identity, "run_id", None)
    if not run_id:
        raise ValidationError("design.share 必须在 Flux 任务 Run 内调用（当前调用没有关联 Run）")
    run = await ctx.container.run_repo.get(run_id)
    if run is None or run.task_id is None:
        raise ValidationError(
            "design.share 找不到当前 Run 归属的任务，无法写入对话",
            details={"run_id": run_id},
        )
    return run.task_id


async def design_share(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"design_id", "note"})
    design_id = _require_design_id(params.get("design_id"))
    note = optional_str(params, "note")
    task_id = await _task_for_run(ctx)
    async with _client(ctx) as client:
        body = await _request(client, "GET", f"/api/designs/{design_id}")
    design = body.get("design")
    if not isinstance(design, dict):
        raise DesignStudioUnavailable("设计服务返回了异常设计稿")

    summary = _summary(design)
    name = str(design.get("name") or "未命名设计")
    content = note or f"设计稿「{name}」已生成，点开查看原生画布预览"
    payload = {
        **summary,
        "preview": _preview(design),
    }
    message = (
        await ctx.container.task_repo.add_messages(
            task_id,
            [
                MessageDraft(
                    role=TaskMessageRole.ASSISTANT.value,
                    kind="design_card",
                    content=content,
                    payload=payload,
                )
            ],
        )
    )[0]
    return {
        "shared": True,
        "task_id": str(task_id),
        "message_id": str(message.id),
        "design_id": design_id,
        "name": name,
        "note": "设计稿卡片已写入当前任务对话，用户点开后进入原生画布预览。",
    }


# ---------------------------------------------------------------- 注册


def design_tools() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name="design.list",
            title="列出设计稿",
            description=(
                "列出设计工作室里的设计稿（只读）。不传 project_id 时按当前任务所属项目过滤；"
                "设计类任务开工前先看这里，避免重复造稿。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "项目 UUID；缺省从当前 Run 的任务推导",
                    }
                },
                "additionalProperties": False,
            },
            capability=Capability.FILE_READ,
            handler=design_list,
        ),
        ToolSpec(
            name="design.get",
            title="读取设计稿",
            description=(
                "取整份设计稿（画板与图层），供修改前读取现状。返回的 design 里 screens 是完整"
                "节点树，summary 是画板/图层数与版本。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "design_id": {
                        "type": "string",
                        "description": "设计稿 id（小写字母/数字/连字符）",
                    }
                },
                "required": ["design_id"],
                "additionalProperties": False,
            },
            capability=Capability.FILE_READ,
            handler=design_get,
        ),
        ToolSpec(
            name="design.create",
            title="新建设计稿",
            description=(
                "在设计工作室新建设计稿并写入画板。做页面/UI/视觉设计必须走这个工具，不要用"
                "自写 HTML、截图或静态图片代替。screens 里每个画板需要 id、name、w、h，"
                "nodes 是图层数组（text/card/pill/circle/decor 等，坐标与尺寸用数字）。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "设计稿名称"},
                    "project": {"type": "string", "description": "项目显示名，例如「Flux 移动端」"},
                    "project_id": {
                        "type": "string",
                        "description": "关联的 Flux 项目 UUID（可空）",
                    },
                    "description": {"type": "string", "description": "一句话说明，例如「浅色版」"},
                    "bg": {"type": "string", "description": "画布底色，例如 #F4F6F9"},
                    "screens": {
                        "type": "array",
                        "description": "1–20 个画板；省略时只创建一块空白画板",
                        "items": {"type": "object"},
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            capability=Capability.FILE_WRITE,
            handler=design_create,
        ),
        ToolSpec(
            name="design.update",
            title="更新设计稿",
            description=(
                "整份更新设计稿（传 screens 则替换画板，否则只改元数据）。修改已有设计前先"
                "design.get。带 base_version 做乐观锁；不带时按当前最新版本保存。冲突返回"
                "design_conflict 与当前 version：重新 design.get 后再改，不要覆盖别人的改动。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "design_id": {"type": "string", "description": "设计稿 id"},
                    "base_version": {"type": "integer", "description": "基于哪个版本修改（可空）"},
                    "name": {"type": "string", "description": "新名称（可空）"},
                    "project": {"type": "string", "description": "项目显示名（可空）"},
                    "project_id": {"type": "string", "description": "项目 UUID（可空）"},
                    "description": {"type": "string", "description": "说明（可空）"},
                    "status": {
                        "type": "string",
                        "description": "draft / review / approved 等（可空）",
                    },
                    "bg": {"type": "string", "description": "画布底色（可空）"},
                    "screens": {
                        "type": "array",
                        "description": "替换全部画板；省略时保留现有画板",
                        "items": {"type": "object"},
                    },
                },
                "required": ["design_id"],
                "additionalProperties": False,
            },
            capability=Capability.FILE_WRITE,
            handler=design_update,
        ),
        ToolSpec(
            name="design.component_list",
            title="列出组件与模板",
            description=(
                "列出设计工作室的组件库与模板库（只读）：返回每个组件的参数定义与默认值、"
                "模板的尺寸与图层数。搭版式前先看这里，优先用现成组件/模板，不要手搓重复模块。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["component", "template"],
                        "description": "只看组件或只看模板；省略则两类都返回",
                    },
                    "group": {"type": "string", "description": "按分组过滤，例如「操作」「系统」"},
                },
                "additionalProperties": False,
            },
            capability=Capability.FILE_READ,
            handler=design_component_list,
        ),
        ToolSpec(
            name="design.component_insert",
            title="插入组件实例",
            description=(
                "把组件库里的组件按其参数展开成一个实例节点，追加到指定画板顶层。实例带 origin，"
                "后续可用 design.component_update 只改参数或切换变体。组件有 variants 时可用 "
                "variant 指定蓝图（省略用默认变体）。不传 x/y 时自动水平居中并叠在现有图层"
                "下方。返回的 node_id 是实例根图层 id，改参数、选中它都用这个 id。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "design_id": {"type": "string", "description": "设计稿 id"},
                    "asset_id": {
                        "type": "string",
                        "description": "组件 id，例如 button-primary（见 component_list）",
                    },
                    "params": {
                        "type": "object",
                        "description": '组件参数覆盖，例如 {"text": "立即购买", "width": 300}',
                    },
                    "variant": {
                        "type": "string",
                        "description": "变体 id（见 component_list 的 variants）；省略用默认变体",
                    },
                    "screen_id": {"type": "string", "description": "目标画板 id（优先于下标）"},
                    "screen_index": {
                        "type": "integer",
                        "description": "目标画板下标，默认 0",
                    },
                    "node_id": {
                        "type": "string",
                        "description": "自定义实例根 id（≤20 字符）；省略自动生成 inst-xxxxxx",
                    },
                    "x": {"type": "number", "description": "覆盖横坐标（省略自动居中）"},
                    "y": {"type": "number", "description": "覆盖纵坐标（省略自动堆叠）"},
                    "index": {
                        "type": "integer",
                        "description": "在画板顶层图层中的插入位置，默认追加到末尾",
                    },
                    "base_version": {
                        "type": "integer",
                        "description": "基于哪个版本提交（可空，缺省用刚读到的版本）",
                    },
                },
                "required": ["design_id", "asset_id"],
                "additionalProperties": False,
            },
            capability=Capability.FILE_WRITE,
            handler=design_component_insert,
        ),
        ToolSpec(
            name="design.component_update",
            title="调整组件实例参数/变体",
            description=(
                "按参数重建组件实例子树（实例位置、名称、显隐、锁定、旋转、透明度保持不变）。"
                "scope=instance 只改这一个实例；scope=all 把新参数同步到全稿所有同源实例。"
                "params 改参数，variant 切换变体，两者至少给一个；只切变体时可以不带 params。"
                "只能用于带 origin 的组件实例；普通图层要改样式请用 design.ai 或 design.update。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "design_id": {"type": "string", "description": "设计稿 id"},
                    "node_id": {"type": "string", "description": "组件实例的根图层 id"},
                    "params": {
                        "type": "object",
                        "description": '要改的参数，例如 {"text": "已售罄", "fill": "#F87171"}',
                    },
                    "variant": {
                        "type": "string",
                        "description": "要切换到的变体 id，例如 ghost / danger（见列表工具）",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["instance", "all"],
                        "description": "instance 只改当前实例（默认）；all 同步到所有同源实例",
                    },
                    "label": {"type": "string", "description": "提交说明（可空）"},
                    "base_version": {
                        "type": "integer",
                        "description": "基于哪个版本提交（可空，缺省用刚读到的版本）",
                    },
                },
                "required": ["design_id", "node_id"],
                "additionalProperties": False,
            },
            capability=Capability.FILE_WRITE,
            handler=design_component_update,
        ),
        ToolSpec(
            name="design.template_apply",
            title="应用模板到画板",
            description=(
                "把模板库里的整屏模板铺进设计稿。mode=new-screen 追加一块新画板（排在上一个"
                "画板右侧）；mode=replace-screen 用模板内容整体替换指定画板的图层（保留画板 id"
                "与位置）。模板参数同样可在 params 里覆盖，例如页面标题。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "design_id": {"type": "string", "description": "设计稿 id"},
                    "asset_id": {
                        "type": "string",
                        "description": "模板 id，例如 tpl-settings（从 design.component_list 取）",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["new-screen", "replace-screen"],
                        "description": "new-screen 新建画板（默认）；replace-screen 替换现有画板",
                    },
                    "screen_id": {
                        "type": "string",
                        "description": "mode=replace-screen 时要替换的画板 id",
                    },
                    "screen_index": {
                        "type": "integer",
                        "description": "mode=replace-screen 时可改用画板下标，默认 0",
                    },
                    "params": {
                        "type": "object",
                        "description": '模板参数覆盖，例如 {"title": "偏好设置"}',
                    },
                    "name": {"type": "string", "description": "画板名称覆盖（可空）"},
                    "index": {
                        "type": "integer",
                        "description": "mode=new-screen 时新画板的插入位置，默认追加到末尾",
                    },
                    "base_version": {
                        "type": "integer",
                        "description": "基于哪个版本提交（可空，缺省用刚读到的版本）",
                    },
                },
                "required": ["design_id", "asset_id"],
                "additionalProperties": False,
            },
            capability=Capability.FILE_WRITE,
            handler=design_template_apply,
        ),
        ToolSpec(
            name="design.ai",
            title="AI 局部修改设计稿",
            description=(
                "让设计工作室的 AI 只改当前画板的局部（改文案/配色/间距、加删单个模块、调整"
                "层级），服务端把改动落盘并返回新版本。改已有设计优先用它，不要 design.get 后"
                "整份 design.update 覆盖：那样既费 token，也容易冲掉别人的改动。"
                "mode=auto 时按 selected_ids 推断；要改整屏可不传选中项。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "design_id": {"type": "string", "description": "设计稿 id"},
                    "instruction": {
                        "type": "string",
                        "description": "修改要求，具体到对象与效果，例如「把标题改成深色并右移 8」",
                    },
                    "screen_index": {
                        "type": "integer",
                        "description": "目标画板下标，默认 0（与 screen_id 二选一）",
                    },
                    "screen_id": {"type": "string", "description": "目标画板 id（优先于下标）"},
                    "selected_ids": {
                        "type": "array",
                        "description": "要改的图层 id（最多 40 个）；mode=node/multi 时必填",
                        "items": {"type": "string"},
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["auto", "node", "multi", "screen"],
                        "description": (
                            "auto 按选中项推断；node 改一个、multi 改多个、screen 改整屏"
                        ),
                    },
                    "base_version": {
                        "type": "integer",
                        "description": "基于哪个版本修改（可空，缺省用刚读到的版本）",
                    },
                },
                "required": ["design_id", "instruction"],
                "additionalProperties": False,
            },
            capability=Capability.FILE_WRITE,
            handler=design_ai,
        ),
        ToolSpec(
            name="design.revisions",
            title="查看设计稿历史",
            description=(
                "列出设计稿的历史快照（只读）。改坏了、想对比或要回滚时先看这里，"
                "拿到 version 再调 design.revert。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "design_id": {"type": "string", "description": "设计稿 id"},
                },
                "required": ["design_id"],
                "additionalProperties": False,
            },
            capability=Capability.FILE_READ,
            handler=design_revisions,
        ),
        ToolSpec(
            name="design.revert",
            title="回滚设计稿",
            description=(
                "把设计稿恢复到指定历史版本（会生成一个新版本，不删除历史）。"
                "配合 design.revisions 使用；带 base_version 做乐观锁。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "design_id": {"type": "string", "description": "设计稿 id"},
                    "version": {"type": "integer", "description": "要恢复到的历史版本号"},
                    "base_version": {
                        "type": "integer",
                        "description": "当前版本号，用于乐观锁（可空）",
                    },
                },
                "required": ["design_id", "version"],
                "additionalProperties": False,
            },
            capability=Capability.FILE_WRITE,
            handler=design_revert,
        ),
        ToolSpec(
            name="design.share",
            title="分享设计稿到对话",
            description=(
                "把设计稿作为卡片消息写进当前任务对话。设计任务收尾时必须调用一次，"
                "用户会在对话里看到可点开的卡片并进入原生画布预览。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "design_id": {"type": "string", "description": "要分享的设计稿 id"},
                    "note": {"type": "string", "description": "卡片上的一句话说明（可空）"},
                },
                "required": ["design_id"],
                "additionalProperties": False,
            },
            capability=Capability.FILE_WRITE,
            handler=design_share,
        ),
    )
