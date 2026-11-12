"""MCP 工具注册表（目标架构 §3.3 Phase 1：只读 / 写入 / 运行时 / 记忆 / 终端五组工具）。

这里是工具面的**唯一真源**：`tools/list` 返回什么、调用时要求什么能力，
全部由本模块的 `TOOLS` 决定。不在表里的能力（apply / git.push / secret / shell.exec）
不是"忘了注册"，而是硬禁令（§3.5），因此也没有任何开关能打开它。

`terminal.execute` 是**受控放行**的那一个（Agent Terminal Console §4，2026-10-05 决策）：
它不是"打开任意 shell"，而是必须经 Flux 的 Terminal Session 执行——命令与输出都落成
终端事件供人实时观察并 Stop，且输出只回结构化摘要、不回灌 Agent Context（§9）。

`proposal.preview` 是自检通道：把提案内容应用到临时副本再跑配置的测试命令，真实工作区
不动；它不写真实文件、也不改提案，只回答"这组改动能不能让测试通过"。
"""

from __future__ import annotations

from flux.core.mcp.tools.base import ToolContext, ToolSpec
from flux.core.mcp.tools.delivery_tools import delivery_tools
from flux.core.mcp.tools.design_tools import design_tools
from flux.core.mcp.tools.knowledge_tools import knowledge_tools
from flux.core.mcp.tools.memory_tools import memory_tools
from flux.core.mcp.tools.plan_tools import plan_tools
from flux.core.mcp.tools.preview_tools import preview_tools
from flux.core.mcp.tools.read_tools import read_tools
from flux.core.mcp.tools.routine_tools import routine_tools
from flux.core.mcp.tools.runtime_tools import runtime_tools
from flux.core.mcp.tools.supervisor_tools import supervisor_tools
from flux.core.mcp.tools.terminal_tools import terminal_tools
from flux.core.mcp.tools.write_tools import write_tools

ALL_TOOLS: tuple[ToolSpec, ...] = (
    *read_tools(),
    *write_tools(),
    *runtime_tools(),
    *memory_tools(),
    *terminal_tools(),
    *preview_tools(),
    *design_tools(),
    *delivery_tools(),
    *plan_tools(),
    *supervisor_tools(),
    *knowledge_tools(),
    *routine_tools(),
)

TOOLS: dict[str, ToolSpec] = {tool.name: tool for tool in ALL_TOOLS}

#: 任何 Flux 版本都必须存在的核心工具。测试断言它必须"是面上工具的子集"，
#: 而不是与面上工具精确相等——新增工具（如 flux_context）不该被误杀，
#: 但删掉核心工具仍会被发现（§5：required_tools ⊆ advertised_tools）。
CORE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "context.boot",
        "context.get",
        "flux_context",
        "memory.recall",
        "proposal.create",
        "terminal.execute",
        "workspace.diff",
        "workspace.read",
    }
)

#: 面上一律不暴露的硬禁令（§3.5）。写在这里是为了让测试能断言"确实没有"。
#: terminal.execute 已按 Agent Terminal Console §4 受控放行（见模块 docstring），
#: 但裸 shell 通道仍然关闭。
FORBIDDEN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "workspace.apply",
        "git.push",
        "git.commit",
        "secret.read",
        "shell.exec",
    }
)

__all__ = [
    "ALL_TOOLS",
    "CORE_TOOL_NAMES",
    "FORBIDDEN_TOOL_NAMES",
    "TOOLS",
    "ToolContext",
    "ToolSpec",
]
