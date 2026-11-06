"""终端执行工具 `terminal.execute`（Agent Terminal Console §4 / §5 / §9）。

这是 Agent 执行命令的**唯一**通道：命令经 Flux 的 Terminal Session 执行，因此
"AI 执行了哪条命令、输出是什么、退出码多少"都落成终端事件，工程师能在终端窗口
实时看到并随时 Stop / Force Stop（§3.3 / §3.4）。

两条硬约束：

- 输出**只回结构化摘要**（取尾部截断），完整输出留在终端事件里，不把长日志回灌
  Agent Context（§9）——`npm install`、编译、测试日志动辄上千行；
- 命令固定在工作区根目录下执行（与 Apply 同源），Agent 不能自选目录。
"""

from __future__ import annotations

import uuid
from typing import Any

from flux.core.context.redact import redact
from flux.core.mcp.tools.base import (
    ToolContext,
    ToolSpec,
    optional_str,
    reject_unknown,
    require_str,
)
from flux.core.permissions.modes import normalize_mode
from flux.core.permissions.risk import LEVEL_HIGH, LEVEL_PROTECTED, classify_command
from flux.core.task_engine.approvals import (
    APPROVAL_KIND_HIGH_RISK,
    APPROVAL_KIND_PROTECTED,
    ApprovalCancelledError,
    ApprovalDeniedError,
    ApprovalTimeoutError,
    raise_approval,
    wait_for_approval,
)
from flux.enums import Capability, PermissionMode, TerminalEventKind, TerminalSource
from flux.errors import ConflictError
from flux.logging import get_logger

logger = get_logger(__name__)

#: 回给 Agent 的输出摘要上限。取**尾部**：测试汇总行、报错栈尾、编译结论都在末尾。
MAX_SUMMARY_CHARS = 4000

TERMINAL_EXECUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "要执行的命令（在工作区根目录下执行；输出进入终端会话供人观察）",
        },
        "session_id": {
            "type": "string",
            "description": "（可空）复用已有终端会话；留空则新建一个并把 session_id 返回给你",
        },
    },
    "required": ["command"],
    "additionalProperties": False,
}


def _run_uuid(ctx: ToolContext) -> uuid.UUID | None:
    """本次 MCP 调用所属的 Run（来自服务端校验过的 `X-Flux-Run-Id`）；无则 None。

    终端会话落上 run_id 后，Run 终态时才能被定向回收——否则 Agent 派生的命令进程会泄漏。
    """
    raw = ctx.identity.run_id
    return uuid.UUID(raw) if raw else None


async def _approve_if_needed(ctx: ToolContext, command: str) -> None:
    """执行前的审批闸门：受保护对象在 Plan/正常/完全访问模式都问；高风险只在正常模式问。

    Full 模式跳过 high（用户已明确风险自负），但 protected 仍然逐次询问；
    Unattended 模式两者都放行（无人值守，用户明确要求不被打断）；
    Plan 模式在 MCP 白名单层已被拒绝，走不到这里。拒绝/超时/取消都抛错，命令不执行。
    """
    mode = normalize_mode(ctx.identity.permission_mode)
    report = classify_command(command)
    if mode == PermissionMode.UNATTENDED.value:
        # 免审核（无人值守）：任何等级都不落审批卡；保留日志供事后审计
        logger.warning(
            "terminal.unattended_allow mode=%s rule=%s level=%s command=%s",
            mode,
            report.rule,
            report.level,
            redact(command).text[:200],
        )
        return
    if report.level == LEVEL_PROTECTED:
        kind = APPROVAL_KIND_PROTECTED
        title = "受保护操作 · 需要你确认"
    elif report.level == LEVEL_HIGH and mode != PermissionMode.FULL.value:
        kind = APPROVAL_KIND_HIGH_RISK
        title = "高风险命令 · 需要你确认"
    else:
        logger.info("terminal.risk_allow mode=%s rule=%s level=%s", mode, report.rule, report.level)
        return

    task_id = await _task_uuid(ctx)
    if task_id is None:
        # 无 Run 归属就找不到对话去落审批卡：fail-closed，不允许绕过闸门直接执行
        raise ConflictError(
            f"该命令需要用户审批（{report.reason}），但当前调用没有关联的 Run，无法发起审批"
        )

    display_command = redact(command).text
    detail = f"{report.reason}\n\n命令：\n{display_command}"
    record = await raise_approval(
        ctx.container,
        task_id=task_id,
        run_id=ctx.identity.run_id,
        kind=kind,
        title=title,
        detail=detail,
        meta={
            "command": display_command,
            "rule": report.rule,
            "level": report.level,
            "reason": report.reason,
        },
    )
    outcome = await wait_for_approval(
        ctx.container,
        task_id=task_id,
        decision_id=str(record["id"]),
        run_id=ctx.identity.run_id,
    )
    if outcome == "allowed":
        logger.info(
            "terminal.approved mode=%s rule=%s decision=%s", mode, report.rule, record["id"]
        )
        return
    if outcome == "denied":
        raise ApprovalDeniedError(f"用户拒绝了这条命令（{report.rule}），未执行：{display_command}")
    if outcome == "timeout":
        raise ApprovalTimeoutError("审批超时（10 分钟）：命令未执行。需要时请向用户重新申请。")
    raise ApprovalCancelledError("任务已取消或结束，命令未执行")


async def _task_uuid(ctx: ToolContext) -> uuid.UUID | None:
    """本次调用所属的任务（经 Run 反查）；没有 Run 或 Run 不属于任务时返回 None。

    注意：Run 主键是 `uuid4().hex`（无连字符），必须原样拿去查，不能先转 `UUID` 再 `str()`
    ——补上连字符后与存储的 Run id 不一致，会永远查不到 Run 而误判「没有关联 Run」。
    """
    run_id = ctx.identity.run_id
    if not run_id:
        return None
    run = await ctx.container.run_repo.get(run_id)
    if run is None or run.task_id is None:
        return None
    return uuid.UUID(str(run.task_id))


async def terminal_execute(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"command", "session_id"})
    command = require_str(params, "command")
    session_id = optional_str(params, "session_id")
    await _approve_if_needed(ctx, command)
    service = ctx.container.terminal

    if session_id is None:
        session = await service.create_session(run_id=_run_uuid(ctx))
        session_id = str(session.id)
    else:
        session = await service.get_session(session_id)
    # 本次命令产生的事件从这条 seq 之后开始；用它把输出限定在本次命令范围内
    before_seq = session.next_seq - 1

    event = await service.run_command(session_id, command, source=TerminalSource.AI)
    produced = await service.list_events(session_id, after_seq=before_seq)
    output = "".join(
        item.chunk or "" for item in produced if item.kind == TerminalEventKind.OUTPUT.value
    )
    truncated = len(output) > MAX_SUMMARY_CHARS
    return {
        "session_id": session_id,
        "command": command,
        "exit_code": event.exit_code,
        "succeeded": event.exit_code == 0,
        "output_summary": output[-MAX_SUMMARY_CHARS:] if truncated else output,
        "output_truncated": truncated,
        "note": "完整输出只在终端会话事件（terminal.output）里，未回灌 Agent Context",
    }


def terminal_tools() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name="terminal.execute",
            title="执行终端命令",
            description=(
                "在工作区根目录下执行一条命令，并把命令与实时输出写进终端会话（工程师可在"
                "终端窗口观察并随时 Stop）。返回 exit_code 与输出摘要；完整输出不回灌上下文。"
                "注意：提案落盘前磁盘上仍是旧代码，提交前自检请用 proposal.preview。"
            ),
            input_schema=TERMINAL_EXECUTE_SCHEMA,
            capability=Capability.TERMINAL_EXECUTE,
            handler=terminal_execute,
        ),
    )
