"""Bot 侧审批规则工具：`approval_rule.add` / `approval_rule.list`。

Bot 可以给自己**加更严格的规则**（例如「执行 rm 前先问我」「支付超过 500 先问我」），
但**不能删除、停用或放宽**：

- 没有 update / delete 工具；
- `enabled=false` 一律拒绝（等同停用）；
- 规则只会触发审批，永远不会直接授予权限，因此「新增」不可能放宽任何东西。

归属由服务端解析：run → task → bot / cloud_user_id，工具入参里没有
`cloud_user_id`，Bot 无法把规则写到别人名下。规则属于云用户，对该用户的所有
Bot 生效；`created_by_bot_id` 只用于前端展示「这条是谁加的」。

MCP 路径与 Bot 对话轮次共用本模块；对话侧入口是 `run_approval_rule_tool`。
"""

from __future__ import annotations

import uuid
from typing import Any

from flux.core.mcp.tools.base import (
    ToolContext,
    ToolSpec,
    optional_str,
    reject_unknown,
    require_str,
)
from flux.core.user_scope import normalize_cloud_user_id
from flux.enums import Capability
from flux.errors import BadRequestError, ValidationError

_ADD_FIELDS = {"type", "params", "description", "enabled"}

APPROVAL_RULE_ADD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "description": (
                "规则类型：payment_amount_over / command_pattern / task_match / "
                "tool_match / outbound_message / custom"
            ),
        },
        "params": {
            "type": "object",
            "description": (
                "类型参数。payment_amount_over: {amount, currency?}；"
                "command_pattern: {pattern, style?=glob|regex}；"
                "tool_match / task_match: {tool_name? / task_kind?}；"
                "outbound_message: {channel?: feishu|wecom|dingtalk}；"
                "custom: {key, value}"
            ),
        },
        "description": {
            "type": "string",
            "description": "给用户看的说明；省略时按类型生成中文默认描述",
        },
        "enabled": {
            "type": "boolean",
            "description": "Bot 只能新增启用规则；传 false 会被拒绝（不允许停用/放宽）",
        },
    },
    "required": ["type", "params"],
    "additionalProperties": False,
}

APPROVAL_RULE_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

#: 对话侧工具名（部分供应商只接受 [A-Za-z0-9_-]）
APPROVAL_RULE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "approval_rule_add",
            "description": (
                "为当前 Bot 所属的 Flux 用户新增一条审批规则。用户说「某类操作前先问我」"
                "时使用。规则对该用户的所有 Bot 生效；Bot 只能新增更严格的规则，"
                "不能删除、停用或放宽。"
            ),
            "parameters": APPROVAL_RULE_ADD_SCHEMA,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "approval_rule_list",
            "description": "列出当前 Bot 所属用户的全部审批规则（含停用项）。",
            "parameters": APPROVAL_RULE_LIST_SCHEMA,
        },
    },
]

APPROVAL_RULE_TOOL_NAMES: frozenset[str] = frozenset(
    tool["function"]["name"] for tool in APPROVAL_RULE_TOOLS
)


async def _bot_context(ctx: ToolContext) -> tuple[str, uuid.UUID]:
    """解析本次 Run 的 (cloud_user_id, bot_id)；不是 Bot 开工的 Run 就拒绝。"""
    run_id = ctx.identity.run_id
    if not run_id:
        raise BadRequestError(
            "该工具只能在 Bot 开工的 Run 里使用：当前调用没有关联的 Run",
            details={"field": "run_id"},
        )
    run = await ctx.container.run_repo.get(run_id)
    if run is None or getattr(run, "task_id", None) is None:
        raise BadRequestError(
            "找不到本次 Run 关联的任务，无法确定规则归属",
            details={"run_id": run_id},
        )
    try:
        task = await ctx.container.task_repo.get(uuid.UUID(str(run.task_id)))
    except (TypeError, ValueError) as exc:
        raise BadRequestError("本次 Run 的任务标识非法", details={"run_id": run_id}) from exc
    bot_id = getattr(task, "bot_id", None)
    if bot_id is None:
        raise BadRequestError(
            "当前 Run 不属于任何 Bot，不能添加或查看 Bot 审批规则",
            details={"run_id": run_id},
        )
    cloud_user_id = normalize_cloud_user_id(getattr(task, "cloud_user_id", None))
    if cloud_user_id is None:
        cloud_user_id = normalize_cloud_user_id(
            getattr(ctx.container.settings, "owner_cloud_user_id", None)
        )
    if cloud_user_id is None:
        raise BadRequestError(
            "无法确定本次 Run 属于哪个 Flux 云用户，已拒绝写入规则",
            details={"run_id": run_id},
        )
    return cloud_user_id, uuid.UUID(str(bot_id))


async def approval_rule_add(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    """新增一条 bot 创建的规则；enabled=false 明确拒绝。"""
    reject_unknown(params, _ADD_FIELDS)
    rule_type = require_str(params, "type")
    raw_params = params.get("params")
    if not isinstance(raw_params, dict):
        raise ValidationError("params 必须是对象", details={"param": "params"})
    if params.get("enabled") is False:
        raise ValidationError(
            "Bot 只能新增启用中的规则，不能停用规则",
            details={"field": "enabled", "bot_may_not_loosen": True},
        )
    description = optional_str(params, "description")
    cloud_user_id, bot_id = await _bot_context(ctx)
    row = await ctx.container.approval_rules.create(
        cloud_user_id=cloud_user_id,
        rule_type=rule_type,
        params=raw_params,
        description=description,
        enabled=True,
        created_by_kind="bot",
        created_by_bot_id=bot_id,
    )
    return {"rule": row.to_dict()}


async def approval_rule_list(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    """列出 Bot 所属用户的全部规则；看不到任何其他用户。"""
    reject_unknown(params, set())
    cloud_user_id, _bot_id = await _bot_context(ctx)
    rows = await ctx.container.approval_rules.list(cloud_user_id, enabled_only=False)
    rules = [row.to_dict() for row in rows]
    return {"rules": rules, "count": len(rules)}


async def run_approval_rule_tool(
    service: Any,
    bot: Any,
    name: str,
    arguments: dict[str, Any],
    *,
    cloud_user_id: str | None = None,
    owner_cloud_user_id: str | None = None,
) -> Any:
    """Bot 对话轮次入口；规则归属 = 本轮对话的 cloud_user_id（回退主用户）。"""
    args = arguments or {}
    user = normalize_cloud_user_id(cloud_user_id) or normalize_cloud_user_id(owner_cloud_user_id)
    if user is None:
        raise BadRequestError("无法确定这次对话属于哪个 Flux 云用户，已拒绝写入规则")

    if name == "approval_rule_add":
        reject_unknown(args, _ADD_FIELDS)
        rule_type = require_str(args, "type")
        raw_params = args.get("params")
        if not isinstance(raw_params, dict):
            raise ValidationError("params 必须是对象", details={"param": "params"})
        if args.get("enabled") is False:
            raise ValidationError(
                "Bot 只能新增启用中的规则，不能停用规则",
                details={"field": "enabled", "bot_may_not_loosen": True},
            )
        row = await service.create(
            cloud_user_id=user,
            rule_type=rule_type,
            params=raw_params,
            description=optional_str(args, "description"),
            enabled=True,
            created_by_kind="bot",
            created_by_bot_id=bot.id,
        )
        return {"rule": row.to_dict()}
    if name == "approval_rule_list":
        reject_unknown(args, set())
        rows = await service.list(user, enabled_only=False)
        rules = [row.to_dict() for row in rows]
        return {"rules": rules, "count": len(rules)}
    raise ValidationError(
        f"未知或不允许的审批规则工具：{name}；Bot 只能新增或查看规则",
        details={"tool": name, "allowed": sorted(APPROVAL_RULE_TOOL_NAMES)},
    )


def approval_rule_tools() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name="approval_rule.add",
            title="给自己加一条审批规则",
            description=(
                "为当前 Bot 所属的 Flux 用户新增审批规则（规则对该用户所有 Bot 生效）。"
                "Bot 只能新增更严格的规则，不能删除、停用或放宽；"
                "新增前可先用 approval_rule.list 查看现有规则。"
            ),
            input_schema=APPROVAL_RULE_ADD_SCHEMA,
            capability=Capability.FILE_WRITE,
            handler=approval_rule_add,
        ),
        ToolSpec(
            name="approval_rule.list",
            title="查看当前用户的审批规则",
            description="列出当前 Bot 所属用户的全部审批规则（含停用项），看不到其他用户。",
            input_schema=APPROVAL_RULE_LIST_SCHEMA,
            capability=Capability.FILE_READ,
            handler=approval_rule_list,
        ),
    )


__all__ = [
    "APPROVAL_RULE_ADD_SCHEMA",
    "APPROVAL_RULE_LIST_SCHEMA",
    "APPROVAL_RULE_TOOLS",
    "APPROVAL_RULE_TOOL_NAMES",
    "approval_rule_add",
    "approval_rule_list",
    "approval_rule_tools",
    "run_approval_rule_tool",
]
