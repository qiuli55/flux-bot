"""运行时上下文工具 `context.boot` / `flux_context`（最终方案 §6 / §7 / §8）。

`context.boot` 是**开工入口**：一次调用同时返回三份上下文，省掉每个 Run 开头
`flux_context` → `context.get` → `memory.recall` 三次串行往返（各 7~10s）。
它与三个旧工具是「聚合 + 原样透出」的关系，不新造语义：

- `runtime`（= `flux_context`）  = 我在哪里、我能做什么（含握手回执与能力清单）
- `context`（= `context.get`）   = 这个项目是什么（项目事实 / 文档 / 代码上下文 / 项目约束）
- `memory`（= `memory.recall`）  = Flux 记住的结论（Environment / User / Project 三层）

三个旧工具全部保留：需要刷新单份上下文、或只缺一份时，仍可单独调用；未来新 Runtime
只要按同一入口调 `context.boot` 即可接入，平台侧无需为某个 Agent 定制开场顺序。

`flux_context` 同时承担 **Flux Agent Handshake v1** 的落点（§6）：MCP `initialize` 完成
协议层握手后，Agent 调一次 `context.boot`（或 `flux_context`）拿到 `protocol` 回执与
能力清单，即视为握手完成。只读，不改任何状态。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from flux.config import Settings
from flux.core.agent_runtime.protocol import (
    FLUX_AGENT_PROTOCOL,
    FLUX_AGENT_PROTOCOL_VERSION,
    MODE_RULES,
    RUNTIME_ENTRY,
    RUNTIME_RULES,
)
from flux.core.mcp.context_packager import DEFAULT_BUDGET_CHARS, package_context
from flux.core.mcp.tools.base import ToolContext, ToolSpec, optional_str, reject_unknown
from flux.core.mcp.tools.memory_tools import memory_recall
from flux.core.permissions.modes import boundary_for, normalize_mode
from flux.core.task_engine.repository import TaskRepository
from flux.enums import Capability, FluxCapability
from flux.errors import AIOSError, ValidationError
from flux.models.agent_run import AgentRun
from flux.models.task import Task
from flux.version import VERSION

#: Personal MVP 的平台模式
PLATFORM_MODE = "personal"

FLUX_CONTEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "run_id": {
            "type": "string",
            "description": "当前 Run 标识；不传则按令牌身份取最新的进行中 Run",
        },
        "task_id": {
            "type": "string",
            "description": "当前任务 UUID；不传则从 Run 推导",
        },
        "project_id": {
            "type": "string",
            "description": "项目 UUID；任务/项目已由 Run 推导时传了也不影响结果",
        },
    },
    "additionalProperties": False,
}


async def flux_context(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    """返回 Agent 当前所处的 Flux Runtime 上下文与可用能力（只读）。"""
    # project_id 由 Run/Task 推导，Agent 按执行指令一起带上时不能因此拒绝整次调用
    reject_unknown(params, {"run_id", "task_id", "project_id"})
    container = ctx.container
    settings = container.settings

    run = await _resolve_run(
        container, agent_id=ctx.identity.agent_id, run_id=optional_str(params, "run_id")
    )
    task = await _resolve_task(container.task_repo, optional_str(params, "task_id"), run)
    mode = normalize_mode(ctx.identity.permission_mode)
    boundary = boundary_for(mode)

    return {
        "protocol": {"name": FLUX_AGENT_PROTOCOL, "version": FLUX_AGENT_PROTOCOL_VERSION},
        "platform": {
            "name": settings.app_name,
            "version": VERSION,
            "mode": PLATFORM_MODE,
        },
        "run": _run_view(run),
        "task": _task_view(task),
        "workspace": _workspace_view(container, settings),
        "agent": {"id": ctx.identity.agent_id, "adapter": None},
        "policy": {
            # Full / 免审核模式允许直写工作区，提案不再是唯一完成路径
            "proposal_required": bool(settings.proposal_required)
            and mode not in ("full", "unattended"),
            "direct_apply": mode in ("full", "unattended"),
        },
        # 本次 Run 的权限模式（Plan / Normal / Full）：白名单与审批闸门都以它为准。
        # 模式在 Run 启动时快照，切换模式从下一轮才生效。
        "permission_mode": {
            "mode": mode,
            **boundary,
        },
        "capabilities": _capabilities(settings),
        # §4.1 契约 rules / entry：与 Runtime Bootstrap（protocol.py）同一份真源——
        # Agent 无论从 prompt 还是从 flux_context 拿到的行为规则必须完全一致。
        "rules": list(MODE_RULES.get(mode, RUNTIME_RULES)),
        "entry": list(RUNTIME_ENTRY),
    }


CONTEXT_BOOT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "description": "任务 UUID；带上它才会打包 L1 任务上下文"},
        "project_id": {"type": "string", "description": "项目 UUID；不传则从任务/当前 Run 推导"},
        "budget": {
            "type": "integer",
            "minimum": 1,
            "description": f"字符预算，默认 {DEFAULT_BUDGET_CHARS}",
        },
    },
    "additionalProperties": False,
}


async def context_boot(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    """开工一次拿齐：运行时身份 + 项目上下文 + 三层记忆（三份并行取回）。

    三份数据互不依赖，并行取回后按固定字段名原样透出；任何一份失败都会让整次调用
    失败（而不是静默返回半份），Agent 可据此退回对应的单项工具分别重试。
    """
    reject_unknown(params, {"task_id", "project_id", "budget"})
    task_id = optional_str(params, "task_id")
    project_id = optional_str(params, "project_id")
    # budget 缺省交给打包器按 §6.1 推导，这里只拒绝"传了但不是整数"
    budget = params.get("budget")
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int)):
        raise ValidationError("参数 budget 必须是整数", details={"param": "budget"})

    # 两个旧工具都用同一份「任务/项目」定位参数；为空的键不传，交给它们各自推导
    derived = {
        key: value for key, value in (("task_id", task_id), ("project_id", project_id)) if value
    }
    runtime, packaged, memory = await asyncio.gather(
        flux_context(ctx, derived),
        package_context(ctx.container, task_id=task_id, project_id=project_id, budget=budget),
        memory_recall(ctx, derived),
    )
    return {
        "runtime": runtime,
        "context": packaged,
        "memory": memory,
        "note": (
            "三份上下文已一次取回；需要刷新某一份时再单独调 flux_context / context.get / "
            "memory.recall。context 是快照，动手前用 workspace.read 核对磁盘当前状态。"
        ),
    }


def _run_view(run: AgentRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "status": run.status,
        "task_id": str(run.task_id) if run.task_id else None,
    }


def _task_view(task: Task | None) -> dict[str, Any] | None:
    if task is None:
        return None
    # Task 模型没有 title 列，任务描述即其"标题"（§7 的 task.title 语义）
    return {"id": str(task.id), "title": task.description, "status": task.status}


def _workspace_view(container: Any, settings: Settings) -> dict[str, Any]:
    path = settings.workspace_root or settings.runtime_workspace
    branch: str | None = None
    # Git 信息是尽力而为：未配置工作区根或它根本不是仓库时，如实返回 null，不报错。
    if settings.workspace_root:
        try:
            branches = container.git_client.branches()
            branch = branches.current
        except AIOSError:
            branch = None
    return {"path": path, "git_branch": branch, "git_revision": None}


def _capabilities(settings: Settings) -> list[str]:
    """声明当前 Flux 真实具备的运行时能力（最终方案 §8）。

    缺失即意味着该能力当前不可用，Agent 应据此降级，而不是假设 Runtime 全能力。
    `rollback` 尚未实现，因此不在清单里——不虚报能力。
    """
    capabilities: list[FluxCapability] = [FluxCapability.CONTEXT, FluxCapability.PROPOSAL]
    if settings.workspace_root:
        capabilities += [FluxCapability.WORKSPACE, FluxCapability.APPLY, FluxCapability.GIT]
    return [str(capability) for capability in capabilities]


async def _resolve_run(container: Any, *, agent_id: str, run_id: str | None) -> AgentRun | None:
    """解析当前 Run。指定 run_id 时只返回归属该令牌身份的 Run（不越权读别人的上下文）。"""
    if run_id:
        run = await container.run_repo.get(run_id)
        if run is None or run.agent_id != agent_id:
            return None
        return run
    runs = [r for r in await container.run_repo.list_non_terminal() if r.agent_id == agent_id]
    if not runs:
        return None
    return max(runs, key=lambda r: (r.created_at, r.id))


async def _resolve_task(
    task_repo: TaskRepository, task_id: str | None, run: AgentRun | None
) -> Task | None:
    key: uuid.UUID | None = None
    if task_id:
        try:
            key = uuid.UUID(task_id)
        except ValueError as exc:
            raise ValidationError(
                f"task_id 不是合法 UUID：{task_id}", details={"task_id": task_id}
            ) from exc
    elif run is not None and run.task_id is not None:
        key = run.task_id
    if key is None:
        return None
    try:
        return await task_repo.get(key)
    except AIOSError:
        # 任务被删或不在本库：如实返回 null，而不是让整个上下文查询失败
        return None


def runtime_tools() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name="context.boot",
            title="开工一次拿齐上下文",
            description=(
                "开工第一步调它：一次返回 runtime（运行时身份/能力/握手回执）、"
                "context（项目事实与任务上下文）、memory（三层长期记忆）三份内容，"
                "替代分别调用 flux_context + context.get + memory.recall 的三次往返。"
                "带上 task_id 才会打包任务上下文；只需刷新其中一份时仍可单独调用对应工具。"
            ),
            input_schema=CONTEXT_BOOT_SCHEMA,
            capability=Capability.FILE_READ,
            handler=context_boot,
        ),
        ToolSpec(
            name="flux_context",
            title="获取 Flux 运行时上下文",
            description=(
                "返回当前 Runtime 身份与能力：platform / run / task / workspace / agent / "
                "policy / capabilities / rules / entry，并回执握手协议（flux-agent v1）。"
                "开工时先调它确认自己在哪里、能做哪些事；"
                "能力缺失时应降级，不要假设 Runtime 全能力。"
            ),
            input_schema=FLUX_CONTEXT_SCHEMA,
            capability=Capability.FILE_READ,
            handler=flux_context,
        ),
    )
