"""内置 CLI Agent Adapter 注册表（最终方案 §3.3 / §20-20）。

第一版交付 Generic CLI Adapter + 两个具体 Adapter（OpenCode / Codex），
两者都在本机真实可跑、探测命令已按真实行为核对。
"""

from __future__ import annotations

from flux.core.agent_runtime.adapters.base import CliAgentAdapter
from flux.core.agent_runtime.adapters.codex import CodexAdapter
from flux.core.agent_runtime.adapters.opencode import OpenCodeAdapter


def build_default_adapters() -> tuple[CliAgentAdapter, ...]:
    return (OpenCodeAdapter(), CodexAdapter())


__all__ = ["build_default_adapters"]
