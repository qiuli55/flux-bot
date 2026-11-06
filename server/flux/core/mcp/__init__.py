"""Flux 能力面（目标架构 §3）：MCP Server 及其支撑件。

这个包只做一件事——把 Flux 既有模块（virtual_workspace / project_files /
project_brain / tasks）包装成 agent 可调用的 MCP 工具。它不含 agent loop、
不存对话、不组装 prompt（目标架构 §1 硬约束 1）。
"""
