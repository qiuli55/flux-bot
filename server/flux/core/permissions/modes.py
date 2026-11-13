"""对话权限三模式的工具白名单与令牌能力（2026-10-25 定案）。

模式权威来自 `agent_runs.permission_mode`（Run 启动时从任务快照），**不是令牌 scopes**：
Agent 使用长期令牌（能力位是写死的超集），只靠 scopes 区分不了"这一轮是
Plan 还是 Full"。因此 MCP 层先按模式做工具白名单，再按能力位做第二道检查。

Plan 模式只放行只读工具与计划文档工具；Normal / Full 放行全部现有工具，
差别在 terminal.execute 的审批闸门（见 `flux.core.task_engine.approvals`）。
"""

from __future__ import annotations

from flux.enums import Capability, PermissionMode

#: Plan 模式允许的工具：只读 + 计划文档。
#: design.create / design.update / design.ai / design.revert / design.share
#: 与 proposal.* / terminal.execute 一律不在名单里（不靠"Agent 自觉"）。
PLAN_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "context.boot",
        "flux_context",
        "context.get",
        "context.fetch",
        "context.steps",
        "memory.recall",
        "memory.remember",
        "workspace.read",
        "workspace.read_many",
        "workspace.search",
        "workspace.tree",
        "workspace.diff",
        "design.list",
        "design.get",
        "design.revisions",
        "plan.write",
    }
)

#: 模式 → 工具白名单；None 表示"全部现有工具"（Normal / Full）。
MODE_TOOL_WHITELIST: dict[str, frozenset[str] | None] = {
    PermissionMode.PLAN.value: PLAN_ALLOWED_TOOLS,
    PermissionMode.NORMAL.value: None,
    PermissionMode.FULL.value: None,
    PermissionMode.UNATTENDED.value: None,
}

#: 各模式的令牌能力位：Plan 只发 file.read + plan.write（不签 terminal.execute）；
#: Normal / Full 保持原有三项。plan.write 工具本身以 file.read 为门槛，真正边界是模式白名单。
_MODE_SCOPES: dict[str, tuple[Capability, ...]] = {
    PermissionMode.PLAN.value: (Capability.FILE_READ, Capability.PLAN_WRITE),
    PermissionMode.NORMAL.value: (
        Capability.FILE_READ,
        Capability.FILE_WRITE,
        Capability.TERMINAL_EXECUTE,
    ),
    PermissionMode.FULL.value: (
        Capability.FILE_READ,
        Capability.FILE_WRITE,
        Capability.TERMINAL_EXECUTE,
    ),
    PermissionMode.UNATTENDED.value: (
        Capability.FILE_READ,
        Capability.FILE_WRITE,
        Capability.TERMINAL_EXECUTE,
    ),
}

#: flux_context 里给 Agent 自证的边界摘要（三种 runtime 共用）。
MODE_BOUNDARIES: dict[str, dict[str, object]] = {
    PermissionMode.PLAN.value: {
        "label": "Plan · 只读",
        "summary": "只读上下文并写计划文档；不执行 shell、不改代码、不分享设计稿。",
        "can": [
            "workspace.read/search/tree/diff",
            "design.list/get/revisions",
            "plan.write",
            "memory.remember",
        ],
        "cannot": [
            "terminal.execute",
            "proposal.create/preview",
            "design.create/update/ai/revert/share",
        ],
    },
    PermissionMode.NORMAL.value: {
        "label": "正常模式",
        "summary": "正常执行任务；低风险命令自动放行，高风险命令停下来等你允许。",
        "can": ["全部只读工具", "proposal.create/preview", "design.*"],
        "cannot": ["未经允许的高风险命令", "任何模式的受保护对象"],
    },
    PermissionMode.FULL.value: {
        "label": "完全访问",
        "summary": "跳过逐步审批；受保护对象（密钥文件、系统服务）仍需逐次申请。",
        "can": ["全部工具", "直写工作区", "git commit / push"],
        "cannot": ["未经允许的受保护对象"],
    },
    PermissionMode.UNATTENDED.value: {
        "label": "免审核 · 无人值守",
        "summary": "高风险命令与受保护对象都自动放行，不再打断用户；仅用于用户明确离开设备的场景。",
        "can": ["全部工具", "直写工作区", "自动放行全部审批"],
        "cannot": ["凭猜测执行破坏性操作", "外传密钥内容"],
    },
}


def normalize_mode(value: str | None) -> str:
    """未识别/缺失的模式一律按 normal（fail-closed：宁可多问一次审批）。"""
    if value in MODE_TOOL_WHITELIST:
        return str(value)
    return PermissionMode.NORMAL.value


def allowed_tools(mode: str | None) -> frozenset[str] | None:
    """该模式允许的工具名集合；None = 不限制（Normal / Full）。"""
    return MODE_TOOL_WHITELIST.get(normalize_mode(mode))


def tool_allowed(mode: str | None, tool_name: str) -> bool:
    whitelist = allowed_tools(mode)
    return whitelist is None or tool_name in whitelist


def scopes_for_mode(mode: str | None) -> tuple[Capability, ...]:
    return _MODE_SCOPES[normalize_mode(mode)]


def boundary_for(mode: str | None) -> dict[str, object]:
    return MODE_BOUNDARIES[normalize_mode(mode)]


def denied_message(mode: str | None, tool_name: str) -> str:
    """工具被模式拒绝时回给 Agent 的明确文案（写清模式、原因与下一步）。"""
    normalized = normalize_mode(mode)
    if normalized == PermissionMode.PLAN.value:
        return (
            "当前对话为 Plan 模式（只读）。"
            f"{tool_name} 已禁用；如需执行请让用户切到正常或完全访问模式。"
        )
    return f"当前对话模式（{normalized}）不允许调用 {tool_name}。"


__all__ = [
    "MODE_BOUNDARIES",
    "MODE_TOOL_WHITELIST",
    "PLAN_ALLOWED_TOOLS",
    "allowed_tools",
    "boundary_for",
    "denied_message",
    "normalize_mode",
    "scopes_for_mode",
    "tool_allowed",
]
