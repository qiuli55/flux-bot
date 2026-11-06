"""代码智能（LSP）基础设施。

Web 端 Monaco 与桌面端 AvaloniaEdit 共用同一套后端能力：由本模块按
(工作区, 语言服务器) 常驻子进程，通过 WebSocket 把 LSP 的 JSON-RPC 透传给前端。

对外只有三个入口：

- `registry`：语言服务器清单、探测与安装命令；
- `service.CodeIntelligenceService`：进程生命周期（启动 / 复用 / 空闲回收 / 崩溃清理）；
- `rpc`：`Content-Length` 帧的读写（子进程 stdio 与 WS 文本帧的边界）。
"""

from flux.core.code_intelligence.registry import (
    EXTENSION_LANGUAGES,
    SERVERS,
    LanguageServerSpec,
    describe_servers,
    find_server,
    servers_for_language,
    spec_by_id,
)

__all__ = [
    "EXTENSION_LANGUAGES",
    "SERVERS",
    "LanguageServerSpec",
    "describe_servers",
    "find_server",
    "servers_for_language",
    "spec_by_id",
]
