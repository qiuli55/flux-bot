"""Agent Runtime 实现包（P2-1 / P2-2）。

契约（`base`）在此导出，供 manager 等**低层模块**安全引用（不触发 MCP/容器回环）。
CLI 实现与编排服务（`cli` / `service`）请从各自子模块导入，避免包初始化时的循环依赖。
"""

from flux.core.agent_runtime.runtimes.base import (
    CLI_RUNTIMES,
    DEFAULT_RUNTIME,
    KNOWN_RUNTIMES,
    RUNTIME_CLAUDE,
    RUNTIME_CODEX,
    RUNTIME_OPENCODE,
    AgentRuntime,
    RuntimeEvent,
    RuntimeLaunch,
    RuntimeProcess,
)

__all__ = [
    "CLI_RUNTIMES",
    "DEFAULT_RUNTIME",
    "KNOWN_RUNTIMES",
    "RUNTIME_CLAUDE",
    "RUNTIME_CODEX",
    "RUNTIME_OPENCODE",
    "AgentRuntime",
    "RuntimeEvent",
    "RuntimeLaunch",
    "RuntimeProcess",
]
