"""内置 CLI Agent Adapter 注册表（最终方案 §3.3 / §20-20）。

交付 Generic CLI Adapter + 三个具体 Adapter（Claude Code / OpenCode / Codex）。
Claude Code 是 Bot 的默认底座，排在首位。
"""

from __future__ import annotations

from flux.core.agent_runtime.adapters.base import CliAgentAdapter
from flux.core.agent_runtime.adapters.claude import ClaudeAdapter
from flux.core.agent_runtime.adapters.codex import CodexAdapter
from flux.core.agent_runtime.adapters.opencode import OpenCodeAdapter


def build_default_adapters() -> tuple[CliAgentAdapter, ...]:
    return (ClaudeAdapter(), OpenCodeAdapter(), CodexAdapter())


__all__ = ["build_default_adapters"]
