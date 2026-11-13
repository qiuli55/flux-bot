"""Bot 自调度工具：创建 / 查询 / 更新 / 暂停 / 恢复 / 删除例程。

Bot 在对话里或开工时给自己排活儿：工具只做「提案」，真正落库与调度仍由
`BotService`（`next_run_at` + 常驻轮询）负责——进程重启不丢，和 Bot 主定时同一套机制。

支持三种可组合的触发方式：
- `schedule`：5 段 cron，按 Bot 时区（未设置则 FLUX_TIMEZONE）重复触发；
- `run_at`：ISO 8601 一次性时刻，跑完 `next_run_at` 清空，不再调度；
- `event_triggers`：订阅事件（webhook.* / github.* / email.received 等已知事件）。

MCP 路径只在 Bot 开工的 Run 里可用：`run → task → bot` 三级解析出 bot_id。
Bot 对话轮次走 `ROUTINE_TOOLS` + `run_routine_tool`，bot_id 直接来自当前 Bot，
两套入口调用同一个 `BotService`，不复制业务逻辑。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from flux.core.mcp.tools.base import (
    ToolContext,
    ToolSpec,
    optional_str,
    reject_unknown,
    require_str,
)
from flux.enums import Capability
from flux.errors import NotFoundError, ValidationError

_CREATE_FIELDS = {"name", "instruction", "schedule", "run_at", "event_triggers", "enabled"}
_UPDATE_FIELDS = {
    "routine_id",
    "name",
    "instruction",
    "schedule",
    "run_at",
    "event_triggers",
    "enabled",
}

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
            "description": (
                "5 段 cron（分 时 日 月 周），按 Bot 自己的时区解释，"
                "如 '0 9 * * *' = 每天 09:00；与 run_at 互斥"
            ),
        },
        "run_at": {
            "type": "string",
            "description": (
                "只跑一次的时刻，ISO 8601（如 '2026-11-20T09:00:00+08:00'）；"
                "不带时区时按 Bot 自己的时区解释。与 schedule 互斥"
            ),
        },
        "event_triggers": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "订阅的事件名（如 webhook.deploy、github.push、email.received）；"
                "可与 schedule / run_at 组合，也可单独存在"
            ),
        },
        "enabled": {"type": "boolean", "description": "是否立即启用，默认 true"},
    },
    "required": ["name", "instruction"],
    "additionalProperties": False,
}

ROUTINE_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

ROUTINE_UPDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "routine_id": {"type": "string", "description": "routine.list 返回的 id"},
        "name": {"type": "string", "description": "新名称，最多 64 字"},
        "instruction": {"type": "string", "description": "新的执行指令"},
        "schedule": {
            "type": ["string", "null"],
            "description": (
                "新的 5 段 cron；显式传 null 表示改成非 cron。传了 schedule 就不能同时传 run_at"
            ),
        },
        "run_at": {
            "type": ["string", "null"],
            "description": (
                "改成一次性触发时刻（ISO 8601，按 Bot 时区解释）；会自动清掉 cron。与 schedule 互斥"
            ),
        },
        "event_triggers": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": "新的事件订阅；传 null / [] 表示清空",
        },
        "enabled": {"type": "boolean", "description": "true 恢复、false 暂停"},
    },
    "required": ["routine_id"],
    "additionalProperties": False,
}

ROUTINE_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"routine_id": {"type": "string", "description": "routine.list 返回的 id"}},
    "required": ["routine_id"],
    "additionalProperties": False,
}

ROUTINE_DELETE_SCHEMA = ROUTINE_ID_SCHEMA


def _string_list_arg(params: dict[str, Any], key: str) -> list[str] | None:
    """字符串数组入参：null / 空数组表示清空，其它类型明确报错。"""
    if key not in params or params[key] is None:
        return None
    value = params[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError(f"参数 {key} 必须是字符串数组", details={"param": key})
    cleaned = [item.strip() for item in value if item.strip()]
    return cleaned or None


def _run_at_arg(params: dict[str, Any]) -> str | datetime | None:
    """一次性时刻入参：JSON 里是 ISO 8601 字符串，内部调用也允许直接传 datetime。"""
    if "run_at" not in params or params["run_at"] is None:
        return None
    value = params["run_at"]
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return value.strip() or None
    raise ValidationError("参数 run_at 必须是 ISO 8601 字符串", details={"param": "run_at"})


def _bool_arg(params: dict[str, Any], key: str, *, default: bool | None = None) -> bool | None:
    if key not in params:
        return default
    value = params[key]
    if not isinstance(value, bool):
        raise ValidationError(f"参数 {key} 必须是布尔值", details={"param": key})
    return value


def _create_kwargs(params: dict[str, Any]) -> dict[str, Any]:
    """校验并归一化 create 入参；MCP 与对话 runner 共用同一套规则。"""
    reject_unknown(params, _CREATE_FIELDS)
    name = require_str(params, "name")
    instruction = require_str(params, "instruction")
    schedule = optional_str(params, "schedule")
    run_at = _run_at_arg(params)
    event_triggers = _string_list_arg(params, "event_triggers")
    enabled = _bool_arg(params, "enabled", default=True)
    if schedule is not None and run_at is not None:
        raise ValidationError(
            "schedule 与 run_at 不能同时指定：重复定时用 schedule，只跑一次用 run_at",
            details={"param": "run_at"},
        )
    if schedule is None and run_at is None and not event_triggers:
        raise ValidationError(
            "例程至少需要 schedule、run_at、event_triggers 之一",
            details={"params": ["schedule", "run_at", "event_triggers"]},
        )
    return {
        "name": name,
        "instruction": instruction,
        "schedule": schedule,
        "run_at": run_at,
        "event_triggers": event_triggers,
        "enabled": bool(enabled),
    }


def _update_changes(params: dict[str, Any]) -> dict[str, Any]:
    """校验并归一化 update 入参；只返回调用方真正提供的字段。"""
    reject_unknown(params, _UPDATE_FIELDS)
    changes: dict[str, Any] = {}
    if "name" in params:
        changes["name"] = require_str(params, "name")
    if "instruction" in params:
        changes["instruction"] = optional_str(params, "instruction")
    if "schedule" in params:
        changes["schedule"] = optional_str(params, "schedule")
    if "run_at" in params:
        changes["run_at"] = _run_at_arg(params)
    if "event_triggers" in params:
        changes["event_triggers"] = _string_list_arg(params, "event_triggers")
    if "enabled" in params:
        changes["enabled"] = _bool_arg(params, "enabled")
    if not changes:
        raise ValidationError(
            "routine.update 至少提供一个要更新的字段",
            details={"allowed": sorted(_UPDATE_FIELDS - {"routine_id"})},
        )
    if changes.get("schedule") is not None and changes.get("run_at") is not None:
        raise ValidationError(
            "schedule 与 run_at 不能同时指定：重复定时用 schedule，只跑一次用 run_at",
            details={"param": "run_at"},
        )
    return changes


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


async def _cloud_user_for_run(ctx: ToolContext) -> str | None:
    """本次 Run 代表哪个 Flux 云用户：run → task → tasks.cloud_user_id。

    MCP 请求本身没有登录态；任务行是 Agent 执行期唯一可靠的归属来源。
    解析不到时返回 None，让下游按历史口径回退主用户。
    """
    run_id = ctx.identity.run_id
    if not run_id:
        return None
    run = await ctx.container.run_repo.get(run_id)
    if run is None or run.task_id is None:
        return None
    try:
        task = await ctx.container.task_repo.get(run.task_id)
    except NotFoundError:
        return None
    return getattr(task, "cloud_user_id", None)


def _routine_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ValidationError(
            f"routine_id 不是合法 UUID：{value}", details={"param": "routine_id"}
        ) from exc


async def routine_create(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    kwargs = _create_kwargs(params)
    bot_id = await _require_bot_id(ctx)
    routine = await ctx.container.bots.create_routine(
        bot_id,
        **kwargs,
        cloud_user_id=await _cloud_user_for_run(ctx),
    )
    return routine.to_dict()


async def routine_list(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, set())
    bot_id = await _require_bot_id(ctx)
    routines = await ctx.container.bots.list_routines(bot_id)
    return {"routines": [routine.to_dict() for routine in routines]}


async def routine_update(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, _UPDATE_FIELDS)
    routine_id = _routine_id(require_str(params, "routine_id"))
    changes = _update_changes(params)
    bot_id = await _require_bot_id(ctx)
    routine = await ctx.container.bots.update_routine(bot_id, routine_id, changes)
    return routine.to_dict()


async def routine_pause(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"routine_id"})
    routine_id = _routine_id(require_str(params, "routine_id"))
    bot_id = await _require_bot_id(ctx)
    routine = await ctx.container.bots.update_routine(bot_id, routine_id, {"enabled": False})
    return routine.to_dict()


async def routine_resume(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"routine_id"})
    routine_id = _routine_id(require_str(params, "routine_id"))
    bot_id = await _require_bot_id(ctx)
    routine = await ctx.container.bots.update_routine(bot_id, routine_id, {"enabled": True})
    return routine.to_dict()


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
            title="给自己排一个例程",
            description=(
                "给当前 Bot 新建例程：可以按 cron 重复、按 run_at 只跑一次，或订阅事件触发，"
                "三种方式可组合（schedule 与 run_at 互斥）。时间按 Bot 自己的时区解释，"
                "未设置时跟随 FLUX_TIMEZONE。例程落库保存，进程重启不丢；"
                "到点后服务端按 instruction 开一轮新任务。这是 Bot 给自己排活，"
                "不要让用户去后台页面或 API 手动配置。"
            ),
            input_schema=ROUTINE_CREATE_SCHEMA,
            capability=Capability.FILE_WRITE,
            handler=routine_create,
        ),
        ToolSpec(
            name="routine.list",
            title="查看自己的例程",
            description=(
                "列出当前 Bot 的全部例程（含 id、cron、一次性时刻、事件订阅、"
                "下次触发时间与启停状态）。更新/暂停/恢复/删除前先查一遍，避免重复创建。"
            ),
            input_schema=ROUTINE_LIST_SCHEMA,
            capability=Capability.FILE_READ,
            handler=routine_list,
        ),
        ToolSpec(
            name="routine.update",
            title="更新自己的例程",
            description=(
                "按 routine.list 返回的 id 部分更新例程（名称、指令、schedule、run_at、"
                "event_triggers、enabled）。schedule 传 null 表示改成非 cron；"
                "传 run_at 表示改成一次性并自动清掉 cron。"
            ),
            input_schema=ROUTINE_UPDATE_SCHEMA,
            capability=Capability.FILE_WRITE,
            handler=routine_update,
        ),
        ToolSpec(
            name="routine.pause",
            title="暂停自己的例程",
            description=(
                "按 id 暂停例程：只是停用，不丢原计划；尚未执行的一次性时刻会保留，"
                "之后 routine.resume 可以继续原计划。"
            ),
            input_schema=ROUTINE_ID_SCHEMA,
            capability=Capability.FILE_WRITE,
            handler=routine_pause,
        ),
        ToolSpec(
            name="routine.resume",
            title="恢复自己的例程",
            description=(
                "按 id 恢复例程：cron 从当前时刻重新计时；未执行的一次性例程恢复原时刻。"
                "一次性已经跑完且没有事件订阅时无法凭空再跑，需要先 update 新的 run_at。"
            ),
            input_schema=ROUTINE_ID_SCHEMA,
            capability=Capability.FILE_WRITE,
            handler=routine_resume,
        ),
        ToolSpec(
            name="routine.delete",
            title="删除自己的例程",
            description="按 routine.list 返回的 id 删除一条例程；不属于当前 Bot 的 id 会被拒绝。",
            input_schema=ROUTINE_DELETE_SCHEMA,
            capability=Capability.FILE_WRITE,
            handler=routine_delete,
        ),
    )


# --- Bot 对话轮次里的工具（与 SUBTASK_TOOLS 一样由 conversation 注入）---

#: 对话侧工具名用下划线：部分模型供应商只接受 `[A-Za-z0-9_-]` 的函数名。
ROUTINE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "routine_create",
            "description": (
                "给当前 Bot 新建例程：按 cron 重复、按 run_at 只跑一次，或订阅事件触发，"
                "三种方式可组合（schedule 与 run_at 互斥）。用户用自然语言提出"
                "「每天/每周/到点提醒」「某个时间做一次」「收到某类事件时处理」时，"
                "直接调用本工具，不要让用户去设置页面或 API 手动配置。"
            ),
            "parameters": ROUTINE_CREATE_SCHEMA,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "routine_list",
            "description": "列出当前 Bot 的全部例程（含触发方式、下次触发时间与启停状态）。",
            "parameters": ROUTINE_LIST_SCHEMA,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "routine_update",
            "description": (
                "按 id 部分更新例程；schedule 传 null 表示改成非 cron，"
                "传 run_at 表示改成一次性并自动清掉 cron。"
            ),
            "parameters": ROUTINE_UPDATE_SCHEMA,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "routine_pause",
            "description": "按 id 暂停例程：只停用、不丢原计划。",
            "parameters": ROUTINE_ID_SCHEMA,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "routine_resume",
            "description": "按 id 恢复例程；已跑完的一次性例程需要先 update 新的 run_at。",
            "parameters": ROUTINE_ID_SCHEMA,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "routine_delete",
            "description": "按 id 删除例程。",
            "parameters": ROUTINE_DELETE_SCHEMA,
        },
    },
]

ROUTINE_TOOL_NAMES: frozenset[str] = frozenset(tool["function"]["name"] for tool in ROUTINE_TOOLS)


async def run_routine_tool(
    service: Any,
    bot: Any,
    name: str,
    arguments: dict[str, Any],
    *,
    cloud_user_id: str | None = None,
) -> Any:
    """执行一个对话侧例程工具；调用与 MCP 相同的 `BotService`。

    `cloud_user_id` 是本轮对话的人格：从绑定 IM / 登录 API 带进来，创建例程时
    落库，之后到点开工就按这个人运行。
    """
    args = arguments or {}
    if name == "routine_create":
        routine = await service.create_routine(
            bot.id, **_create_kwargs(args), cloud_user_id=cloud_user_id
        )
        return routine.to_dict()
    if name == "routine_list":
        reject_unknown(args, set())
        routines = await service.list_routines(bot.id)
        return {"routines": [routine.to_dict() for routine in routines]}
    if name == "routine_update":
        reject_unknown(args, _UPDATE_FIELDS)
        routine_id = _routine_id(require_str(args, "routine_id"))
        changes = _update_changes(args)
        routine = await service.update_routine(bot.id, routine_id, changes)
        return routine.to_dict()
    if name in {"routine_pause", "routine_resume"}:
        reject_unknown(args, {"routine_id"})
        routine_id = _routine_id(require_str(args, "routine_id"))
        routine = await service.update_routine(
            bot.id, routine_id, {"enabled": name == "routine_resume"}
        )
        return routine.to_dict()
    if name == "routine_delete":
        reject_unknown(args, {"routine_id"})
        routine_id = _routine_id(require_str(args, "routine_id"))
        await service.delete_routine(bot.id, routine_id)
        return {"deleted": True, "routine_id": str(routine_id)}
    raise ValidationError(
        f"未知的例程工具：{name}", details={"tool": name, "allowed": sorted(ROUTINE_TOOL_NAMES)}
    )


__all__ = [
    "ROUTINE_TOOLS",
    "ROUTINE_TOOL_NAMES",
    "routine_tools",
    "run_routine_tool",
]
