"""Flux Server CLI（NEXT_PHASE_OPTIMIZATION §9）。

服务器管理入口：进程内直连 Flux Core，不经 HTTP / MCP。
MCP 或 API 进程出问题时，本入口仍可用——这正是它作为 MCP 兜底通道的意义。
"""

from __future__ import annotations

from flux.cli.main import main

__all__ = ["main"]
