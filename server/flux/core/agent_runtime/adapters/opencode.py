"""OpenCode Adapter（最终方案 §3.3 / §15 点名的第一个具体 Adapter）。

探测规则来自 OpenCode 1.18.x 的真实 CLI 行为（[2026-10-05 快照]：
`opencode --version` → `1.18.29`；`opencode run <message>` → 非交互跑一条指令）。

认证口径（P2-2 §7.2）：本机 OpenCode 的 provider 已内联在全局配置里，**无需登录**；
`opencode auth list` 显示 `0 credentials` 只是 auth.json 的口径，不代表不可用。
因此 `check_auth` 以**配置解析**为准（找到内联 apiKey / 凭据引用即 ok），
配置里没有才回落到 `auth list`；且绝不把"auth.json 为空"直接判成 missing。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from flux.core.agent_runtime.adapters.generic import GenericCliAdapter

#: 全局配置默认落点（本机实测：~/.config/opencode/opencode.json）
_DEFAULT_CONFIG_PATHS = (
    "~/.config/opencode/opencode.json",
    "~/.config/opencode/config.json",
)


class OpenCodeAdapter(GenericCliAdapter):
    def __init__(self, **overrides: object) -> None:
        config: dict[str, object] = {
            "name": "opencode",
            "executable": "opencode",
            "version_args": ("--version",),
            "auth_args": ("auth", "list"),
            "auth_ok_markers": ("credentials",),
            "auth_missing_markers": ("0 credentials",),
            "run_args": ("run",),
            "capabilities": ("mcp", "stream"),
        }
        config.update(overrides)
        super().__init__(**config)  # type: ignore[arg-type]

    def check_auth(self) -> str:
        if self._config_has_credentials():
            return "ok"
        # 配置里解析不到凭据时，仍以 auth list 为事实；但"auth.json 为空"不等于
        # "不可用"（provider 可能来自配置/环境），故把 missing 降级为 unknown。
        status = super().check_auth()
        return "unknown" if status == "missing" else status

    def _config_has_credentials(self) -> bool:
        for path in self._config_paths():
            data = _load_json(path)
            providers = data.get("provider") if isinstance(data, dict) else None
            if not isinstance(providers, dict):
                continue
            for provider in providers.values():
                options = provider.get("options") if isinstance(provider, dict) else None
                if not isinstance(options, dict):
                    continue
                if any(
                    isinstance(value, str)
                    and value.strip()
                    and any(token in key.lower() for token in ("key", "token"))
                    for key, value in options.items()
                ):
                    return True
        return False

    @staticmethod
    def _config_paths() -> tuple[Path, ...]:
        paths: list[Path] = []
        override = os.environ.get("OPENCODE_CONFIG")
        if override:
            paths.append(Path(override).expanduser())
        paths.extend(Path(p).expanduser() for p in _DEFAULT_CONFIG_PATHS)
        return tuple(paths)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
