"""Claude Code 权限审批桥：`permission_prompt`（`mcp__flux__permission_prompt`）。

正常模式下 Claude Code 以 `--permission-mode default` 跑在无头 `-p` 里：任何需要用户点头
的工具调用都会因为"没人能应答"被**静默拒绝**。本工具就是那个应答方——`cli.py` 在正常模式
且 flux MCP 端点存在时追加 `--permission-prompt-tool mcp__flux__permission_prompt`，
Claude Code 每次需要审批就把请求交给这里。**默认不审批**：只有命中该用户的审批规则
（工具名 / 命令模式）才复用既有审批卡（`raise_approval` / `wait_for_approval`），
否则直接 allow；用户在对话里点「允许 / 拒绝」。

契约（Claude Code 2.1.x，2026-11 实测 `claude-ds` 2.1.322）：

- 入参实测为 ``{"tool_name": str, "input": object, "tool_use_id": str}``。本文档未正式承诺，
  故实现只认这三个键、其余忽略，字段类型不对一律按拒绝处理（fail-closed）。
- 返回值必须是**恰好一个** text content block，text 是 JSON：
  ``{"behavior": "allow", "updatedInput": {...}}`` 或
  ``{"behavior": "deny", "message": "..."}``。不能带 structuredContent——Flux MCP 层的
  `_tool_text` 本来就只渲染一个 text block，故 handler 直接返回 dict 即可。
- 旧版本（v2.1.86–v2.1.206）allow 必须带 `updatedInput`；目标版本 2.1.323 不需要，但仍
  原样回填原始 input 以保持兼容。

三条边界：

- **失败一律拒绝**：拿不到 Run、参数不合法、内部异常、审批超时、Run 已取消，全部 deny；
  绝不在审批链路出错时默认放行。没有规则命中则直接 allow（默认零审批）。
- **审批桥本身不再审批**：`permission_prompt` 由 CLI 直接调用（不经模型工具面，实测不会
  再触发权限询问）；万一模型自己调用它，这里直接拒绝，避免审批套娃。
- **不记录输入值**：日志只记 run / 工具名 / 决定 / 风险规则；审批卡上的预览先按敏感键
  打码、再过 `redact()`，并截断。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from flux.core.approval_rules.evaluator import (
    ACTION_TOOL,
    ApprovalAction,
    evaluate_approval,
    raise_rule_approval,
)
from flux.core.context.redact import redact
from flux.core.mcp.tools.base import ToolContext, ToolSpec
from flux.core.permissions.modes import normalize_mode
from flux.core.permissions.risk import classify_command
from flux.core.task_engine.approvals import APPROVAL_KIND_TOOL_PROMPT
from flux.core.user_scope import normalize_cloud_user_id
from flux.enums import Capability, DshRunStatus, PermissionMode
from flux.logging import get_logger

logger = get_logger(__name__)

#: Flux 侧工具名（`tools/call` 的 name）
PERMISSION_PROMPT_TOOL_NAME = "permission_prompt"
#: Claude Code `--permission-prompt-tool` 的完整名字（server 名固定为 flux）
PERMISSION_PROMPT_TOOL = f"mcp__flux__{PERMISSION_PROMPT_TOOL_NAME}"

#: 工具名展示上限：名字来自客户端，截断后再进审批卡与日志。
_TOOL_NAME_LIMIT = 80
#: 审批卡上展示的入参预览上限（字符）。再长就只截断，不把整份文件内容塞进对话。
_PREVIEW_LIMIT = 500
#: 预览打码前先截断原文的上限：避免对整份大文件跑正则/熵检测。
_RAW_PREVIEW_LIMIT = 4000
#: 敏感键名：值一律替换成占位符（低熵口令正则识别不出来，只能按 key 兜底）。
_SECRET_KEY_RE = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|authorization|cookie|credential)",
    re.IGNORECASE,
)
#: 已进入终态的 Run：不再写审批卡，直接按取消处理。
_FINAL_RUN_STATUSES = frozenset(
    {
        DshRunStatus.COMPLETED.value,
        DshRunStatus.FAILED.value,
        DshRunStatus.TIMEOUT.value,
        DshRunStatus.CANCELLED.value,
        DshRunStatus.INTERRUPTED.value,
    }
)

PERMISSION_PROMPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool_name": {
            "type": "string",
            "description": "发起审批的 Claude Code 工具名，如 Bash / Write / Edit",
        },
        "input": {
            "type": "object",
            "description": "该工具原本要使用的入参；允许时原样回填给 Claude Code",
        },
        "tool_use_id": {
            "type": "string",
            "description": "工具调用 id，仅用于关联，不参与判定",
        },
    },
    # 入参形状是 Claude Code 的实现细节，不承诺稳定：多余字段一律忽略而不是报错，
    # 否则上游加一个字段就会让整条审批桥静默失效。
    "additionalProperties": True,
}


def _deny(message: str) -> dict[str, Any]:
    return {"behavior": "deny", "message": message}


def _scrub_secrets(value: Any, depth: int = 0) -> Any:
    """按敏感键名递归打码；深度受限，避免异常结构拖垮审批。"""
    if depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        scrubbed: dict[str, Any] = {}
        for key, item in value.items():
            if _SECRET_KEY_RE.search(str(key)) and isinstance(item, (str, int, float)):
                scrubbed[str(key)] = "[REDACTED]"
            else:
                scrubbed[str(key)] = _scrub_secrets(item, depth + 1)
        return scrubbed
    if isinstance(value, (list, tuple)):
        return [_scrub_secrets(item, depth + 1) for item in value]
    return value


def _safe_preview(text: str) -> str:
    """先截断原文、再打码、最后截到展示长度——顺序保证密钥不会因截断漏网。"""
    redacted = redact(text[:_RAW_PREVIEW_LIMIT]).text.strip()
    if len(redacted) > _PREVIEW_LIMIT:
        return redacted[:_PREVIEW_LIMIT].rstrip() + "…"
    return redacted


def _bash_command(tool_input: dict[str, Any]) -> str | None:
    command = tool_input.get("command")
    if isinstance(command, str) and command.strip():
        return command
    return None


def _summarize(tool_name: str, tool_input: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """返回 (审批卡 detail, 风险元数据)。detail 不含原始密钥，只含打码后的预览。"""
    command = _bash_command(tool_input) if tool_name.strip().lower() == "bash" else None
    if command is not None:
        preview = _safe_preview(command)
        detail = f"工具：{tool_name}\n命令：{preview}"
        report = classify_command(command)
        detail += f"\n风险：{report.level}（{report.rule}）"
        return detail, {"risk_level": report.level, "risk_rule": report.rule}
    serialized = json.dumps(_scrub_secrets(tool_input), ensure_ascii=False, sort_keys=True)
    return f"工具：{tool_name}\n入参：{_safe_preview(serialized)}", {}


async def permission_prompt(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    """Claude Code 权限询问 → Flux 审批卡 → allow / deny（任何异常都 deny）。"""
    raw_name = params.get("tool_name")
    label = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else "unknown"
    try:
        return await _handle(ctx, params)
    except Exception:  # noqa: BLE001 - 审批桥必须 fail-closed，异常不能变成放行
        logger.warning(
            "claude_permission.deny run=%s tool=%s reason=internal_error",
            ctx.identity.run_id,
            label,
            exc_info=True,
        )
        return _deny("Flux 审批桥内部错误，已按拒绝处理")


async def _handle(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    run_id = ctx.identity.run_id or ""
    raw_name = params.get("tool_name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        logger.info("claude_permission.deny run=%s tool=? reason=bad_arguments", run_id)
        return _deny("缺少合法的 tool_name，已拒绝")
    if raw_name.strip() in (PERMISSION_PROMPT_TOOL, PERMISSION_PROMPT_TOOL_NAME):
        # 模型自己调审批桥：再走一遍审批就是套娃，直接拒绝。
        logger.warning(
            "claude_permission.deny run=%s tool=%s reason=recursive", run_id, PERMISSION_PROMPT_TOOL
        )
        return _deny("审批桥本身不能再发起审批，已拒绝")
    tool_name = raw_name.strip()[:_TOOL_NAME_LIMIT]

    tool_input = params.get("input")
    if not isinstance(tool_input, dict):
        logger.info("claude_permission.deny run=%s tool=%s reason=bad_input", run_id, tool_name)
        return _deny("input 必须是对象，已拒绝")

    mode = normalize_mode(ctx.identity.permission_mode)
    if mode != PermissionMode.NORMAL.value:
        logger.info("claude_permission.deny run=%s tool=%s reason=mode_%s", run_id, tool_name, mode)
        return _deny(f"当前模式（{mode}）不使用逐次审批，已拒绝")

    if not run_id:
        logger.info("claude_permission.deny run=? tool=%s reason=unknown_run", tool_name)
        return _deny("无法定位本次 Run，已拒绝")
    run = await ctx.container.run_repo.get(run_id)
    if run is None or getattr(run, "task_id", None) is None:
        logger.warning(
            "claude_permission.deny run=%s tool=%s reason=unknown_run", run_id, tool_name
        )
        return _deny("找不到本次 Run 关联的任务，已拒绝")
    if str(getattr(run, "agent_id", "")) != str(ctx.identity.agent_id):
        logger.warning(
            "claude_permission.deny run=%s tool=%s reason=run_not_owned", run_id, tool_name
        )
        return _deny("本次 Run 不属于当前 Agent，已拒绝")
    if str(getattr(run, "status", "")) in _FINAL_RUN_STATUSES:
        logger.info(
            "claude_permission.deny run=%s tool=%s reason=run_final status=%s",
            run_id,
            tool_name,
            getattr(run, "status", ""),
        )
        return _deny("本次 Run 已结束，已拒绝")
    try:
        task_id = uuid.UUID(str(run.task_id))
    except (TypeError, ValueError):
        logger.warning(
            "claude_permission.deny run=%s tool=%s reason=bad_task_id", run_id, tool_name
        )
        return _deny("本次 Run 关联的任务标识非法，已拒绝")

    # 默认不审批：只有用户规则命中（工具名 / 命令匹配）时才挂卡。
    # 没有规则就是「用户已允许」，直接回填原始入参，绝不留悬空审批卡。
    cloud_user_id = await _task_cloud_user_id(ctx.container, task_id)
    command = _bash_command(tool_input)
    action = ApprovalAction(
        kind=ACTION_TOOL,
        tool_name=tool_name,
        command=command,
    )
    match = await evaluate_approval(ctx.container, cloud_user_id=cloud_user_id, action=action)
    if match is None:
        logger.info("claude_permission.allow run=%s tool=%s reason=no_rule", run_id, tool_name)
        return {"behavior": "allow", "updatedInput": tool_input}

    detail, risk_meta = _summarize(tool_name, tool_input)
    tool_use_id = params.get("tool_use_id")
    outcome = await raise_rule_approval(
        ctx.container,
        match=match,
        task_id=task_id,
        run_id=run_id,
        kind=APPROVAL_KIND_TOOL_PROMPT,
        title=f"Claude 请求使用 {tool_name} · 需要你确认",
        detail=detail,
        meta={
            "tool_name": tool_name,
            "tool_use_id": tool_use_id if isinstance(tool_use_id, str) else "",
            "source": "claude-permission-prompt",
            "permission_mode": mode,
            **risk_meta,
        },
    )
    if outcome == "allowed":
        logger.info(
            "claude_permission.allow run=%s tool=%s approval_rule=%s",
            run_id,
            tool_name,
            match.rule.id,
        )
        # 旧版 Claude Code 要求 allow 必须带 updatedInput；原样回填原始入参。
        return {"behavior": "allow", "updatedInput": tool_input}

    message = {
        "denied": "用户拒绝",
        "timeout": "审批超时（用户未在时限内响应），已拒绝",
        "cancelled": "本次 Run 已取消，已拒绝",
    }.get(outcome, f"审批结果无法识别（{outcome}），已拒绝")
    logger.info(
        "claude_permission.deny run=%s tool=%s decision=%s approval_rule=%s",
        run_id,
        tool_name,
        outcome,
        match.rule.id,
    )
    return _deny(message)


async def _task_cloud_user_id(container: Any, task_id: uuid.UUID) -> str | None:
    """run → task 的云用户归属；历史 NULL 回退部署主用户。"""
    task_repo = getattr(container, "task_repo", None)
    task = None
    if task_repo is not None:
        try:
            task = await task_repo.get(task_id)
        except Exception:  # noqa: BLE001 - 查不到任务只影响规则归属
            task = None
    user = normalize_cloud_user_id(getattr(task, "cloud_user_id", None))
    if user:
        return user
    settings = getattr(container, "settings", None)
    return normalize_cloud_user_id(getattr(settings, "owner_cloud_user_id", None))


def permission_tools() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name=PERMISSION_PROMPT_TOOL_NAME,
            title="Claude Code 权限审批桥",
            description=(
                "由 Claude Code 通过 --permission-prompt-tool 调用：把一次工具权限询问转成"
                "Flux 审批卡，等用户选择后返回 allow / deny。不是给模型主动调用的工具。"
            ),
            input_schema=PERMISSION_PROMPT_SCHEMA,
            # 本工具只转达用户决定，自己不放行任何操作；用最低的读能力作为调用门槛。
            capability=Capability.FILE_READ,
            handler=permission_prompt,
        ),
    )


__all__ = [
    "PERMISSION_PROMPT_TOOL",
    "PERMISSION_PROMPT_TOOL_NAME",
    "PERMISSION_PROMPT_SCHEMA",
    "permission_prompt",
    "permission_tools",
]
