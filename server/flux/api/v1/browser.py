"""浏览器 MCP 状态（只读）。

`FLUX_CLAUDE_EXTRA_MCP_SERVERS` 里没有 `browser` 条目时直接报告未配置，不发任何
网络请求；配置了才用 2 秒超时探活，结果（成功或失败）缓存 30 秒，避免运维轮询
把 Playwright sidecar 打爆。

响应只含 server 名、类型、URL、可达性、状态码、耗时与错误类别；配置里的
headers（可能含 Authorization 令牌）既不返回也不写日志。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.core.agent_runtime.browser_status import (
    BROWSER_SERVER_NAME,
    browser_mcp_summary,
)
from flux.core.agent_runtime.extra_mcp import parse_extra_mcp_servers

router = APIRouter(prefix="/browser", tags=["browser"])


@router.get("/status", summary="浏览器 MCP 状态（只读，2 秒探活，30 秒缓存）")
async def browser_status(container: Container = Depends(get_container)) -> dict[str, object]:
    servers = parse_extra_mcp_servers(container.settings.claude_extra_mcp_servers)
    server = servers.get(BROWSER_SERVER_NAME)
    probe = container.browser_probe
    if server is None:
        return ok(
            {
                "configured": False,
                "server_name": BROWSER_SERVER_NAME,
                "type": None,
                "url": None,
                "ok": None,
                "reachable": None,
                "status_code": None,
                "latency_ms": None,
                "error": None,
                "last_probe_at": None,
                "cached": False,
                "cache_age_seconds": None,
                "cache_ttl_seconds": probe.cache_ttl_seconds,
                "timeout_seconds": probe.timeout_seconds,
            }
        )
    report = await probe.probe(
        str(server["url"]),
        server.get("headers") if isinstance(server.get("headers"), dict) else None,
    )
    return ok(browser_mcp_summary(report, server=server, probe=probe))


__all__ = ["router"]
