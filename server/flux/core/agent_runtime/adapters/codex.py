"""Codex Adapter（最终方案 §3.3 的具体 Adapter 之一）。

探测规则来自 Codex CLI 0.157.x 的真实行为：
- `codex --version` → `codex-cli 0.157.1`（由版本正则抽出 0.157.1）
- `codex login status` → `Logged in using ChatGPT` / `Not logged in`
- `codex exec <message>` → 非交互跑一条指令
"""

from __future__ import annotations

from flux.core.agent_runtime.adapters.generic import GenericCliAdapter


class CodexAdapter(GenericCliAdapter):
    def __init__(self, **overrides: object) -> None:
        config: dict[str, object] = {
            "name": "codex",
            "executable": "codex",
            "version_args": ("--version",),
            "auth_args": ("login", "status"),
            "auth_ok_markers": ("logged in",),
            "auth_missing_markers": ("not logged in",),
            "run_args": ("exec",),
            "capabilities": ("mcp", "stream"),
        }
        config.update(overrides)
        super().__init__(**config)  # type: ignore[arg-type]
