"""Claude Code 扩展 MCP 配置解析（运维扩展点，零第三方依赖）。

环境变量 `FLUX_CLAUDE_EXTRA_MCP_SERVERS` 是一个 JSON 对象字符串，形状与 Claude Code
`mcpServers` 一致。这里做**白名单式**校验后，才由 `CliRuntime` 合并进每轮 Run 的隔离
`mcp.json`：

- 只接受 `http` / `sse` 型远程 server。`stdio` / `command` 型会在容器内拉起任意进程，
  等于把命令执行权交给一份配置字符串，因此一律拒绝；
- server 名必须是 ASCII `[A-Za-z0-9_-]{1,64}`，且不能占用 Flux 保留的 `flux`；
- 日志只记录 server 名与拒绝原因，**绝不记录 headers 的值**（可能含 Bearer 令牌）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from flux.logging import get_logger

logger = get_logger(__name__)

#: Flux 自己注入的 MCP server 名：配置里写同名条目也不会覆盖真实端点。
RESERVED_SERVER_NAME = "flux"

#: server 名白名单：MCP 工具名会拼进 Claude Code 的工具命名空间，必须是安全 ASCII。
_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

#: 只允许远程传输：stdio 会在容器里起进程，属于刻意不开放的攻击面。
_ALLOWED_TYPES = frozenset({"http", "sse"})


def parse_extra_mcp_servers(raw: str) -> dict[str, dict[str, Any]]:
    """解析并校验扩展 MCP 配置；任何非法项都只丢弃该项，不影响其余配置。

    空串 / 纯空白 = 未配置，返回 `{}`。整体不是 JSON 对象（坏 JSON、数组、标量）
    也返回 `{}`。单个条目非法时记录 warning 后丢弃，合法条目照常返回。
    """
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        # 不记录原文：这份 JSON 的 headers 里可能带令牌。
        logger.warning("FLUX_CLAUDE_EXTRA_MCP_SERVERS 不是合法 JSON，已忽略整份配置")
        return {}
    if not isinstance(payload, dict):
        logger.warning(
            "FLUX_CLAUDE_EXTRA_MCP_SERVERS 必须是 JSON 对象，已忽略 type=%s",
            type(payload).__name__,
        )
        return {}
    servers: dict[str, dict[str, Any]] = {}
    for raw_name, spec in payload.items():
        name = str(raw_name)
        parsed = _parse_one(name, spec)
        if parsed is not None:
            servers[name] = parsed
    return servers


def _parse_one(name: str, spec: Any) -> dict[str, Any] | None:
    """校验单个 server 条目；非法时记 warning 并返回 None。"""
    if name == RESERVED_SERVER_NAME:
        logger.warning("扩展 MCP server 名 %r 为 Flux 保留名，已忽略", name)
        return None
    if _NAME_RE.fullmatch(name) is None:
        logger.warning(
            "扩展 MCP server 名不合法，已忽略 name=%r（须匹配 [A-Za-z0-9_-]{1,64}）",
            name,
        )
        return None
    if not isinstance(spec, dict):
        logger.warning("扩展 MCP server 配置必须是对象，已忽略 name=%s", name)
        return None
    kind = spec.get("type")
    if not isinstance(kind, str) or kind.strip().lower() not in _ALLOWED_TYPES:
        # stdio / command 型走到这里：只报类型，不回显 command 等字段。
        logger.warning(
            "扩展 MCP server 类型不受支持，已忽略 name=%s type=%s（只允许 http / sse）",
            name,
            repr(kind) if isinstance(kind, str) else type(kind).__name__,
        )
        return None
    url = spec.get("url")
    if not isinstance(url, str) or not url.strip().startswith(("http://", "https://")):
        logger.warning("扩展 MCP server 的 url 必须是 http(s) 地址，已忽略 name=%s", name)
        return None
    entry: dict[str, Any] = {"type": kind.strip().lower(), "url": url.strip()}
    if "headers" in spec:
        headers = spec.get("headers")
        if not isinstance(headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
        ):
            # 只说形状不对，绝不把 headers 的内容打进日志。
            logger.warning(
                "扩展 MCP server 的 headers 必须是字符串到字符串的对象，已忽略 name=%s",
                name,
            )
            return None
        entry["headers"] = dict(headers)
    return entry
