"""CLI Agent Adapter 包（最终方案 §3.3）。

导出统一接口与内置 Adapter 注册表；Flux Core 只依赖 `CliAgentAdapter` 抽象，
不依赖任何具体 Agent。
"""

from flux.core.agent_runtime.adapters.base import (
    CliAgentAdapter,
    CliAgentProbe,
    CliProcess,
    default_which,
    run_probe,
)
from flux.core.agent_runtime.adapters.codex import CodexAdapter
from flux.core.agent_runtime.adapters.generic import GenericCliAdapter
from flux.core.agent_runtime.adapters.opencode import OpenCodeAdapter
from flux.core.agent_runtime.adapters.registry import build_default_adapters

__all__ = [
    "CliAgentAdapter",
    "CliAgentProbe",
    "CliProcess",
    "CodexAdapter",
    "GenericCliAdapter",
    "OpenCodeAdapter",
    "build_default_adapters",
    "default_which",
    "run_probe",
]
