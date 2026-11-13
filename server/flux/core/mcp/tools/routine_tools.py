"""Bot 自调度工具：`routine.create` / `routine.list` / `routine.delete`。

Bot 在开工时给自己排下一轮活儿：工具只做「提案」，真正落库与调度仍由
`BotService`（`next_run_at` + 常驻轮询）负责——进程重启不丢，和 Bot 主定时同一套机制。

只在 Bot 开工的 Run 里可用：`run → task → bot` 三级解析出 bot_id，普通对话调用会明确报错，
避免把例程挂到不存在的 Bot 上。
"""

from __future__ import annotations

import uuid
from typing import Any

from flux.core.mcp.tools.base import ToolContext, ToolSpec, reject_unknown, require_str
from flux.enums import Capability
from flux.errors import NotFoundError, ValidationError

ROUTINE_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "例程名称，最多 64 字；同名不阻止，靠 id 区分"},
        "instruction": {
            "type": "string",
            "description": "到这个点时让 Bot 做什么，写清楚的作业内容；不要写「继续上一轮」",
        },
        "schedule": {
            "type": "string",
            "description": "5 段 cron（分 时 日 月 周），按 UTC 计算，如 '0 9 * * *' = 每天 09:00",
        },
        "enabled": {"type": "boolean", "description": "是否立即启用，默认 true"},
    },
    "required": ["name", "instruction", "schedule"],
    "additionalProperties": False,
}

ROUTINE_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

ROUTINE_DELETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"routine_id": {"type": "string", "description": "routine.list 返回的 id"}},
    "required": ["routine_id"],
    "additionalProperties": False,
}


async def _require_bot_id(ctx: ToolContext) -> uuid.UUID:
    """从本次 Run 解析所属 Bot：run → task → bot_id。"""
    run_id = ctx.identity.run_id
    if not run_id:
        raise ValidationError(
            "routine.* 只能在 Bot 开工的 Run 内调用：当前请求没有绑定 Run",
            details={"param": "run_id"},
        )
    run = await ctx.container.run_repo.get(run_id)
    if run is None:
        raise NotFoundError(f"Run {run_id} 不存在", details={"run_id": run_id})
    if run.agent_id != ctx.identity.agent_id:
        raise ValidationError(
            "只能为自己的 Bot 管理例程：本次 Run 不属于当前 Agent",
            details={"run_id": run_id},
        )
    if not run.task_id:
        raise ValidationError("本次 Run 没有关联任务，无法定位 Bot", details={"run_id": run_id})
    task = await ctx.container.task_repo.get(run.task_id)
    bot_id = getattr(task, "bot_id", None)
    if not bot_id:
        raise ValidationError(
            "当前任务不是 Bot 开工的任务，不能设定时例程", details={"task_id": str(run.task_id)}
        )
    return bot_id if isinstance(bot_id, uuid.UUID) else uuid.UUID(str(bot_id))


def _routine_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ValidationError(
            f"routine_id 不是合法 UUID：{value}", details={"param": "routine_id"}
        ) from exc


async def routine_create(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"name", "instruction", "schedule", "enabled"})
    name = require_str(params, "name")
    instruction = require_str(params, "instruction")
    schedule = require_str(params, "schedule")
    enabled = params.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValidationError("参数 enabled 必须是布尔值", details={"param": "enabled"})
    bot_id = await _require_bot_id(ctx)
    routine = await ctx.container.bots.create_routine(
        bot_id,
        name=name,
        instruction=instruction,
        schedule=schedule,
        enabled=enabled,
    )
    return routine.to_dict()


async def routine_list(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, set())
    bot_id = await _require_bot_id(ctx)
    routines = await ctx.container.bots.list_routines(bot_id)
    return {"routines": [routine.to_dict() for routine in routines]}


async def routine_delete(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"routine_id"})
    routine_id = _routine_id(require_str(params, "routine_id"))
    bot_id = await _require_bot_id(ctx)
    await ctx.container.bots.delete_routine(bot_id, routine_id)
    return {"deleted": True, "routine_id": str(routine_id)}


def routine_tools() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name="routine.create",
            title="给自己排一个定时例程",
            description=(
                "给当前 Bot 新建一条定时例程：到点后服务端按 instruction 开一轮新任务。"
                "例程落库保存，进程重启不丢；schedule 是 5 段 cron，按 UTC 计算。"
                "只安排确实需要重复执行的事，别把一次性任务写进例程。"
            ),
            input_schema=ROUTINE_CREATE_SCHEMA,
            capability=Capability.FILE_WRITE,
            handler=routine_create,
        ),
        ToolSpec(
            name="routine.list",
            title="查看自己的定时例程",
            description="列出当前 Bot 的全部例程（含 id、cron、下次触发时间与启停状态）。",
            input_schema=ROUTINE_LIST_SCHEMA,
            capability=Capability.FILE_READ,
            handler=routine_list,
        ),
        ToolSpec(
            name="routine.delete",
            title="删除自己的定时例程",
            description="按 routine.list 返回的 id 删除一条例程；不属于当前 Bot 的 id 会被拒绝。",
            input_schema=ROUTINE_DELETE_SCHEMA,
            capability=Capability.FILE_WRITE,
            handler=routine_delete,
        ),
    )
