"""Bot 侧审批规则工具：`approval_rule.add` / `.list` / `.update` / `.delete`。

Bot 与用户都可以加规则，规则属于 Flux 云用户，对该用户名下**所有 Bot** 生效；
`created_by_bot_id` 只用于前端展示「这条是谁加的」。归属由服务端解析：
run → task → bot / cloud_user_id，工具入参里没有 `cloud_user_id`，Bot 无法把规则写到
别人名下。

写操作的确认口径：

- `add` / `list` 直接执行：新增只会让审批更严格，查看没有副作用；
- `update` / `delete` 必须先挂审批卡、等用户在对话里二次确认（允许 / 拒绝）后才落库；
  拒绝、超时、取消一律不改动规则；没有可用审批通道时 fail-closed 直接报错。

MCP 路径与 Bot 对话轮次共用本模块；对话侧入口是 `run_approval_rule_tool`，
由调用方注入 `confirm` 回调把二次确认接到当前任务的审批卡上。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from flux.core.mcp.tools.base import (
    ToolContext,
    ToolSpec,
    optional_str,
    reject_unknown,
    require_str,
)
from flux.core.task_engine.approvals import (
    APPROVAL_KIND_RULE_CHANGE,
    ApprovalCancelledError,
    ApprovalDeniedError,
    ApprovalTimeoutError,
    raise_approval,
    rule_change_approval_options,
    wait_for_approval,
)
from flux.core.user_scope import normalize_cloud_user_id
from flux.enums import Capability
from flux.errors import BadRequestError, ValidationError

_ADD_FIELDS = {"type", "params", "description", "enabled"}
_UPDATE_FIELDS = {"rule_id", "type", "params", "description", "enabled"}
_DELETE_FIELDS = {"rule_id"}
_UPDATE_KEYS = ("type", "params", "description", "enabled")

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
            "description": "Bot 只能新增启用规则；传 false 会被拒绝（不允许新增即停用的规则）",
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

APPROVAL_RULE_UPDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rule_id": {"type": "string", "description": "要修改的规则 id（先用 list 查）"},
        "type": {"type": "string", "description": "新的规则类型；不传则保持不变"},
        "params": {"type": "object", "description": "新的类型参数；不传则保持不变"},
        "description": {"type": "string", "description": "新的说明；不传则保持不变"},
        "enabled": {"type": "boolean", "description": "启用 / 停用；不传则保持不变"},
    },
    "required": ["rule_id"],
    "additionalProperties": False,
}

APPROVAL_RULE_DELETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rule_id": {"type": "string", "description": "要删除的规则 id（先用 list 查）"},
    },
    "required": ["rule_id"],
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
                "时使用。规则对该用户的所有 Bot 生效。"
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
    {
        "type": "function",
        "function": {
            "name": "approval_rule_update",
            "description": (
                "修改当前用户的一条审批规则（规则对所有 Bot 生效）。调用后系统会挂审批卡，"
                "必须等用户在对话里二次确认「允许」才真正修改；拒绝或超时则保持原样。"
            ),
            "parameters": APPROVAL_RULE_UPDATE_SCHEMA,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "approval_rule_delete",
            "description": (
                "删除当前用户的一条审批规则（规则对所有 Bot 生效）。调用后系统会挂审批卡，"
                "必须等用户在对话里二次确认「允许」才真正删除；拒绝或超时则保持原样。"
            ),
            "parameters": APPROVAL_RULE_DELETE_SCHEMA,
        },
    },
]

APPROVAL_RULE_TOOL_NAMES: frozenset[str] = frozenset(
    tool["function"]["name"] for tool in APPROVAL_RULE_TOOLS
)


async def _bot_context(ctx: ToolContext) -> tuple[str, uuid.UUID, uuid.UUID]:
    """解析本次 Run 的 (cloud_user_id, bot_id, task_id)；不是 Bot 开工的 Run 就拒绝。"""
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
        task_id = uuid.UUID(str(run.task_id))
        task = await ctx.container.task_repo.get(task_id)
    except (TypeError, ValueError) as exc:
        raise BadRequestError("本次 Run 的任务标识非法", details={"run_id": run_id}) from exc
    bot_id = getattr(task, "bot_id", None)
    if bot_id is None:
        raise BadRequestError(
            "当前 Run 不属于任何 Bot，不能添加、修改或查看 Bot 审批规则",
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
    return cloud_user_id, uuid.UUID(str(bot_id)), task_id


def _change_action_label(action: str) -> str:
    return "修改" if action == "update" else "删除"


def _change_title(action: str) -> str:
    return f"Bot 请求{_change_action_label(action)}审批规则 · 需要你确认"


def _describe_changes(changes: dict[str, Any]) -> str:
    parts: list[str] = []
    if "type" in changes:
        parts.append(f"类型 → {changes['type']}")
    if "params" in changes:
        serialized = json.dumps(changes["params"], ensure_ascii=False, sort_keys=True)
        parts.append(f"参数 → {serialized}")
    if "description" in changes:
        parts.append(f"说明 → {changes['description']}")
    if "enabled" in changes:
        parts.append("状态 → 启用" if changes["enabled"] else "状态 → 停用")
    return "；".join(parts) or "（无字段变化）"


def _change_detail(row: Any, action: str, changes: dict[str, Any] | None = None) -> str:
    lines = [
        f"规则：{getattr(row, 'description', '') or '（无说明）'}",
        f"类型：{getattr(row, 'type', '')}",
        f"ID：{getattr(row, 'id', '')}",
        "范围：该用户的所有 Bot",
    ]
    if action == "update" and changes is not None:
        lines.append(f"变更：{_describe_changes(changes)}")
    return "\n".join(lines)


def _change_meta(row: Any, action: str) -> dict[str, Any]:
    return {
        "source": "bot-approval-rule-tool",
        "action": action,
        "rule_id": str(getattr(row, "id", "")),
        "rule_type": str(getattr(row, "type", "")),
    }


def _ensure_confirmed(outcome: str, action: str) -> None:
    label = _change_action_label(action)
    if outcome == "allowed":
        return
    if outcome == "denied":
        raise ApprovalDeniedError(f"用户拒绝{label}该审批规则，规则未改动")
    if outcome == "timeout":
        raise ApprovalTimeoutError(f"{label}审批超时（用户未在时限内响应），规则未改动")
    raise ApprovalCancelledError(f"{label}审批已取消，规则未改动")


async def _confirm_change(
    container: Any,
    *,
    task_id: uuid.UUID,
    run_id: str | None,
    action: str,
    row: Any,
    changes: dict[str, Any] | None = None,
) -> None:
    """MCP 路径的二次确认：挂审批卡并阻塞等待，只有「允许」才继续。"""
    record = await raise_approval(
        container,
        task_id=task_id,
        run_id=run_id,
        kind=APPROVAL_KIND_RULE_CHANGE,
        title=_change_title(action),
        detail=_change_detail(row, action, changes),
        meta=_change_meta(row, action),
        options=rule_change_approval_options(_change_action_label(action)),
    )
    outcome = await wait_for_approval(
        container,
        task_id=task_id,
        decision_id=record["id"],
        run_id=run_id,
    )
    _ensure_confirmed(outcome, action)


def _update_changes(params: dict[str, Any]) -> dict[str, Any]:
    """抽取并校验 update 的变更字段；一个字段都没给就报错。"""
    changes = {key: params[key] for key in _UPDATE_KEYS if key in params}
    if not changes:
        raise ValidationError(
            "至少提供一个要修改的字段：type / params / description / enabled",
            details={"allowed": list(_UPDATE_KEYS)},
        )
    if "type" in changes and (not isinstance(changes["type"], str) or not changes["type"].strip()):
        raise ValidationError("type 必须是非空字符串", details={"param": "type"})
    if "params" in changes and not isinstance(changes["params"], dict):
        raise ValidationError("params 必须是对象", details={"param": "params"})
    if "description" in changes and not isinstance(changes["description"], str):
        raise ValidationError("description 必须是字符串", details={"param": "description"})
    if "enabled" in changes and not isinstance(changes["enabled"], bool):
        raise ValidationError("enabled 必须是布尔值", details={"param": "enabled"})
    return changes


async def approval_rule_add(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    """新增一条 bot 创建的规则；enabled=false 明确拒绝。"""
    reject_unknown(params, _ADD_FIELDS)
    rule_type = require_str(params, "type")
    raw_params = params.get("params")
    if not isinstance(raw_params, dict):
        raise ValidationError("params 必须是对象", details={"param": "params"})
    if params.get("enabled") is False:
        raise ValidationError(
            "Bot 新增规则时不能直接停用；需要停用请先新增再经用户确认后修改",
            details={"field": "enabled", "bot_may_not_loosen": True},
        )
    description = optional_str(params, "description")
    cloud_user_id, bot_id, _task_id = await _bot_context(ctx)
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
    cloud_user_id, _bot_id, _task_id = await _bot_context(ctx)
    rows = await ctx.container.approval_rules.list(cloud_user_id, enabled_only=False)
    rules = [row.to_dict() for row in rows]
    return {"rules": rules, "count": len(rules)}


async def approval_rule_update(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    """修改规则：先挂审批卡等用户确认，允许后才落库。"""
    reject_unknown(params, _UPDATE_FIELDS)
    rule_id = require_str(params, "rule_id")
    changes = _update_changes(params)
    cloud_user_id, _bot_id, task_id = await _bot_context(ctx)
    row = await ctx.container.approval_rules.get(rule_id, cloud_user_id=cloud_user_id)
    await _confirm_change(
        ctx.container,
        task_id=task_id,
        run_id=ctx.identity.run_id,
        action="update",
        row=row,
        changes=changes,
    )
    updated = await ctx.container.approval_rules.update(
        rule_id,
        cloud_user_id=cloud_user_id,
        rule_type=changes.get("type"),
        params=changes.get("params"),
        description=changes.get("description"),
        enabled=changes.get("enabled"),
    )
    return {"rule": updated.to_dict(), "confirmed": True}


async def approval_rule_delete(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    """删除规则：先挂审批卡等用户确认，允许后才删除。"""
    reject_unknown(params, _DELETE_FIELDS)
    rule_id = require_str(params, "rule_id")
    cloud_user_id, _bot_id, task_id = await _bot_context(ctx)
    row = await ctx.container.approval_rules.get(rule_id, cloud_user_id=cloud_user_id)
    await _confirm_change(
        ctx.container,
        task_id=task_id,
        run_id=ctx.identity.run_id,
        action="delete",
        row=row,
    )
    deleted = await ctx.container.approval_rules.delete(rule_id, cloud_user_id=cloud_user_id)
    return {"deleted": True, "rule_id": deleted["id"], "confirmed": True}


async def run_approval_rule_tool(
    service: Any,
    bot: Any,
    name: str,
    arguments: dict[str, Any],
    *,
    cloud_user_id: str | None = None,
    owner_cloud_user_id: str | None = None,
    confirm: Any = None,
) -> Any:
    """Bot 对话轮次入口；规则归属 = 本轮对话的 cloud_user_id（回退主用户）。

    `confirm` 是调用方注入的二次确认：签名
    `async (title, detail, meta) -> "allowed"|"denied"|"timeout"|"cancelled"`。
    update / delete 没有它就直接报错（fail-closed），绝不跳过确认直接落库。
    """
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
                "Bot 新增规则时不能直接停用；需要停用请先新增再经用户确认后修改",
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
    if name in ("approval_rule_update", "approval_rule_delete"):
        action = "update" if name == "approval_rule_update" else "delete"
        allowed_fields = _UPDATE_FIELDS if action == "update" else _DELETE_FIELDS
        reject_unknown(args, allowed_fields)
        if confirm is None:
            raise BadRequestError(
                "修改或删除审批规则必须在对话里二次确认：当前调用没有可用的确认通道",
                details={"tool": name},
            )
        rule_id = require_str(args, "rule_id")
        changes = _update_changes(args) if action == "update" else None
        row = await service.get(rule_id, cloud_user_id=user)
        outcome = await confirm(
            _change_title(action),
            _change_detail(row, action, changes),
            _change_meta(row, action),
        )
        _ensure_confirmed(str(outcome), action)
        if action == "update":
            assert changes is not None
            updated = await service.update(
                rule_id,
                cloud_user_id=user,
                rule_type=changes.get("type"),
                params=changes.get("params"),
                description=changes.get("description"),
                enabled=changes.get("enabled"),
            )
            return {"rule": updated.to_dict(), "confirmed": True}
        deleted = await service.delete(rule_id, cloud_user_id=user)
        return {"deleted": True, "rule_id": deleted["id"], "confirmed": True}
    raise ValidationError(
        f"未知或不允许的审批规则工具：{name}",
        details={"tool": name, "allowed": sorted(APPROVAL_RULE_TOOL_NAMES)},
    )


def approval_rule_tools() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name="approval_rule.add",
            title="给自己加一条审批规则",
            description=(
                "为当前 Bot 所属的 Flux 用户新增审批规则（规则对该用户所有 Bot 生效）。"
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
        ToolSpec(
            name="approval_rule.update",
            title="修改一条审批规则（需用户二次确认）",
            description=(
                "修改当前用户的审批规则（规则对该用户所有 Bot 生效）。"
                "调用后会挂审批卡，只有用户在对话里点「允许」才真正落库；"
                "拒绝、超时、取消都不改动规则。"
            ),
            input_schema=APPROVAL_RULE_UPDATE_SCHEMA,
            capability=Capability.FILE_WRITE,
            handler=approval_rule_update,
        ),
        ToolSpec(
            name="approval_rule.delete",
            title="删除一条审批规则（需用户二次确认）",
            description=(
                "删除当前用户的审批规则（规则对该用户所有 Bot 生效）。"
                "调用后会挂审批卡，只有用户在对话里点「允许」才真正删除；"
                "拒绝、超时、取消都不删除规则。"
            ),
            input_schema=APPROVAL_RULE_DELETE_SCHEMA,
            capability=Capability.FILE_WRITE,
            handler=approval_rule_delete,
        ),
    )


__all__ = [
    "APPROVAL_RULE_ADD_SCHEMA",
    "APPROVAL_RULE_DELETE_SCHEMA",
    "APPROVAL_RULE_LIST_SCHEMA",
    "APPROVAL_RULE_TOOL_NAMES",
    "APPROVAL_RULE_TOOLS",
    "APPROVAL_RULE_UPDATE_SCHEMA",
    "approval_rule_add",
    "approval_rule_delete",
    "approval_rule_list",
    "approval_rule_tools",
    "approval_rule_update",
    "run_approval_rule_tool",
]
