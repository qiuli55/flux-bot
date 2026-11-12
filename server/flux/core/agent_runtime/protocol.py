"""Flux Agent 握手协议标识（最终方案 §6）。

第一版不新造与 MCP 平行的协议：`MCP initialize` + `flux_context` 即 Flux Agent
Handshake v1。这里只放协议名与版本，供 `flux_context` 与 CLI Adapter 共用同一份标识。

同时提供 Runtime Bootstrap / Runtime Identity 的**唯一真源**：所有 CLI Agent Adapter
都从这里取，避免多套 `FLUX_*` 注入各写各的（收口方案 §8.2）。
"""

from __future__ import annotations

from flux.version import VERSION

FLUX_AGENT_PROTOCOL = "flux-agent"
FLUX_AGENT_PROTOCOL_VERSION = "1"

#: §4.1 契约 rules 真源：Bootstrap 文案与 `flux_context.rules` 共用，防止两处各写各的。
RUNTIME_RULES: tuple[str, ...] = (
    "Submit every change through proposal.create; Flux handles validation, approval and apply.",
    "Do not assume direct workspace writes are the Flux completion path.",
    "Verify before submitting: call proposal.preview with the same payload to apply your "
    "changes to a throwaway copy and run the project's configured test command; running "
    "terminal.execute instead tests the old on-disk code. Iterate until it passes.",
    "A proposal you have not tested is not a finished change.",
    "For any page, UI or visual design task, use the design studio tools "
    "(design.list / design.get / design.create / design.update / design.ai) to produce the design. "
    "Do not substitute hand-written HTML, screenshots or static images for the design studio.",
    "To revise an existing design, prefer design.ai with a specific instruction (optionally "
    "selected_ids) so only the touched layers change; avoid reading the whole design and "
    "rewriting it with design.update. Use design.revisions / design.revert to inspect or roll "
    "back history when a change goes wrong.",
    "When a design is ready, call design.share so the user gets a tappable card in the chat; "
    "before changing an existing design, read it with design.get first.",
    "When the task produces files the user should take away (reports, images, PDFs, exports, "
    "archives), call file.deliver with workspace-relative paths so they get a file card in "
    "the chat; deliver finished outputs only, not intermediate artifacts or logs.",
    "If the Flux MCP server is unavailable, fall back to the Flux Server CLI: "
    "flux tools call <tool> --params '<json>'.",
)

#: §4.1 契约 entry 真源：Bootstrap 文案与 `flux_context.entry` 共用，防止两处各写各的。
RUNTIME_ENTRY: tuple[str, ...] = (
    "Call context.boot first (pass task_id): one call returns runtime identity, project "
    "context and remembered conventions, replacing separate flux_context / context.get / "
    "memory.recall calls.",
    "Refresh one layer only when you need it: flux_context (identity / capabilities / "
    "handshake), context.get (project facts, read-only), memory.recall (Environment / User / "
    "Project layers).",
)

#: 权限模式段（2026-10-25 三模式）：三种 runtime 共用同一份真源。
#: 模式边界必须先在指令里讲清楚——MCP 白名单只是第二道保险，Agent 不知道边界时会反复撞墙。
PLAN_RULES: tuple[str, ...] = (
    "You are in Plan mode: READ-ONLY. Do not modify code, do not write workspace files and "
    "do not run terminal commands or proposal.preview.",
    "Use workspace.read / workspace.search / workspace.tree / context.get to investigate, then "
    "deliver the plan with plan.write. Design tools are read-only (design.list / design.get / "
    "design.revisions); design.create / design.update / design.ai / design.revert / "
    "design.share are disabled in this mode.",
    "The plan document is written to Flux's own plans directory; never pick your own path.",
)

NORMAL_RULES: tuple[str, ...] = RUNTIME_RULES + (
    "Low-risk read commands run automatically. High-risk commands (deletes, git commit/push, "
    "package installs, network calls, service changes) are sent to the user for approval; wait "
    "for the decision and never attempt to bypass the gate.",
)

FULL_RULES: tuple[str, ...] = (
    "You are in Full-access mode: you may modify the workspace directly and run git commands "
    "without step-by-step approval. Finish the task, then report what you changed.",
    "Protected resources (secret files such as .env / *.pem / SSH keys, and system services "
    "via systemctl / service / reboot) still require explicit per-use user approval. Ask "
    "yourself twice whether you really need them; if so, explain why and wait for the user. "
    "A previous approval never carries over to the next command.",
    "For page, UI or visual design tasks, use the design studio tools (design.create / "
    "design.update / design.ai) and share the result with design.share; revise existing "
    "designs with design.ai instead of rewriting them.",
    "Deliver finished files the user should take away with file.deliver (workspace-relative "
    "paths); skip intermediate artifacts and logs.",
    "If the Flux MCP server is unavailable, fall back to the Flux Server CLI: "
    "flux tools call <tool> --params '<json>'.",
)

UNATTENDED_RULES: tuple[str, ...] = (
    "You are in unattended mode: no one is watching. High-risk commands and protected "
    "resources are auto-approved, so nothing will stop you when you are wrong. Be "
    "conservative: avoid destructive operations, never read or copy secrets unless the task "
    "explicitly requires it, never restart the host, and never push to a shared branch "
    "without a clear reason.",
    "For page, UI or visual design tasks, use the design studio tools (design.create / "
    "design.update / design.ai) and share the result with design.share; revise existing "
    "designs with design.ai instead of rewriting them.",
    "Deliver finished files the user should take away with file.deliver (workspace-relative "
    "paths); skip intermediate artifacts and logs.",
    "When you finish, report every risky action you took and anything you could not verify.",
    "If the Flux MCP server is unavailable, fall back to the Flux Server CLI: "
    "flux tools call <tool> --params '<json>'.",
)

MODE_RULES: dict[str, tuple[str, ...]] = {
    "plan": PLAN_RULES,
    "normal": NORMAL_RULES,
    "full": FULL_RULES,
    "unattended": UNATTENDED_RULES,
}

MODE_HEADINGS: dict[str, str] = {
    "plan": "Plan mode (read-only, plan document only)",
    "normal": "Normal mode (high-risk commands need user approval)",
    "full": "Full-access mode (step-by-step approval skipped; protected resources still ask)",
    "unattended": "Unattended mode (no human approval at all; risk is on the user)",
}


def runtime_bootstrap(mode: str = "normal") -> str:
    """按权限模式生成 Runtime Bootstrap（未识别的模式 fail-closed 按 normal 处理）。"""
    rules = MODE_RULES.get(mode, NORMAL_RULES)
    heading = MODE_HEADINGS.get(mode, MODE_HEADINGS["normal"])
    return (
        "You are running inside Flux. This is your first mandatory context for this run.\n\n"
        "Flux owns the task lifecycle, workspace, proposal validation, approval, apply, "
        "testing and git.\n\n"
        f"Permission mode: {heading}\n\n"
        "Entry:\n"
        + "\n".join(f"- {item}" for item in RUNTIME_ENTRY)
        + "\n\nRules:\n"
        + "\n".join(f"- {rule}" for rule in rules)
    )


#: 兼容既有调用方（默认正常模式）。
RUNTIME_BOOTSTRAP = runtime_bootstrap("normal")


def compose_instruction(instruction: str, mode: str = "normal") -> str:
    """把 Runtime Bootstrap 拼在任务指令之前，作为 Flux 托管启动的 System Context。"""
    return f"{runtime_bootstrap(mode)}\n\n{instruction}"


def build_runtime_env(
    *,
    run_id: str,
    workspace: str,
    task_id: str | None = None,
    agent_id: str | None = None,
    mcp_endpoint: str | None = None,
    permission_mode: str = "normal",
) -> dict[str, str]:
    """Runtime Identity 环境变量（最终方案 §5）。

    这些变量只说明"我在 Flux 里、属于哪个 Run / Task / Workspace"，**不是安全凭证**：
    鉴权仍以令牌 → canonical id 为准，伪造 `FLUX_*` 不会带来任何额外权限。
    """
    env = {
        "FLUX_RUNTIME": "1",
        "FLUX_VERSION": VERSION,
        "FLUX_RUN_ID": run_id,
        "FLUX_WORKSPACE": workspace,
        "FLUX_PERMISSION_MODE": permission_mode,
    }
    if task_id:
        env["FLUX_TASK_ID"] = task_id
    if agent_id:
        env["FLUX_AGENT_ID"] = agent_id
    if mcp_endpoint:
        env["FLUX_MCP_ENDPOINT"] = mcp_endpoint
    return env
