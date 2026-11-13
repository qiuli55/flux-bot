"""Claude Code Adapter（Bot 底座 Agent）。

探测与启动口径按 `@anthropic-ai/claude-code` 的真实 CLI 行为：

- `claude --version` → `2.0.14 (Claude Code)`（版本正则抽出 `2.0.14`）
- `claude --print <prompt> --output-format stream-json --verbose` → 非交互跑一条指令，
  stdout 每行一个 JSON 事件（`system/init`、`assistant`、`user`、`result` 等）
- 续聊：`--resume <session_id>`；MCP：`--mcp-config <file> --strict-mcp-config`
- 鉴权：`ANTHROPIC_API_KEY`（或 `ANTHROPIC_AUTH_TOKEN`）与 `ANTHROPIC_BASE_URL`；
  第三方订阅套餐先经 OpenClaw 登录，再把 `ANTHROPIC_BASE_URL` 指向 OpenClaw 网关，
  Claude Code 只是调用方，不关心网关背后是哪家的套餐。

认证探测不猜测「未登录」：Flux 托管的 Run 每次都会注入密钥（密钥库通道或账号配置），
`auth.json` 是否存在不代表不可用，故取不到确切事实时返回 `unknown`。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from flux.core.agent_runtime.adapters.generic import GenericCliAdapter

#: Claude Code 的账号凭据默认落点（`claude login` 之后生成）
_DEFAULT_CREDENTIALS_PATHS = (
    "~/.claude/.credentials.json",
    "~/.config/claude/.credentials.json",
)


class ClaudeAdapter(GenericCliAdapter):
    def __init__(self, **overrides: object) -> None:
        config: dict[str, object] = {
            # installation 记录名必须与 runtime 标识一致（TaskRunService 按 runtime 查接入状态）
            "name": "claude-code",
            "executable": "claude",
            "version_args": ("--version",),
            # 没有稳定的 `auth status` 子命令：认证由环境变量/凭据文件判定（见 check_auth）
            "auth_args": None,
            "run_args": ("--print",),
            "capabilities": ("mcp", "stream", "resume"),
        }
        config.update(overrides)
        super().__init__(**config)  # type: ignore[arg-type]

    def check_auth(self) -> str:
        """有 API key / 账号凭据即视为可用；无法判定时返回 unknown（绝不误判 missing）。"""
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            if (os.environ.get(name) or "").strip():
                return "ok"
        for path in self._credentials_paths():
            data = _load_json(path)
            if data:
                return "ok"
        return "unknown"

    @staticmethod
    def _credentials_paths() -> tuple[Path, ...]:
        paths: list[Path] = []
        config_dir = (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
        if config_dir:
            paths.append(Path(config_dir).expanduser() / ".credentials.json")
        paths.extend(Path(p).expanduser() for p in _DEFAULT_CREDENTIALS_PATHS)
        return tuple(paths)


def _load_json(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


__all__ = ["ClaudeAdapter"]
