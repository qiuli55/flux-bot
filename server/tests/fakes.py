"""测试替身与公共夹具。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from flux.connectors.base import Connector, ConnectorManifest
from flux.core.agent_runtime.adapters.base import CliAgentProbe
from flux.enums import AgentInstallStatus, Capability
from flux.errors import PermissionDeniedError


class FakeTerminalConnector(Connector):
    """模拟一个需要 terminal.execute 能力的终端连接器。

    真实终端连接器是 M4 交付物（主规格 §19.1 Issues #042），测试替身只用于验证
    注册表的能力校验、审计日志与事件发布路径。
    """

    manifest = ConnectorManifest(
        name="terminal",
        version="0.1.0",
        type="terminal",
        actions=("run",),
        required_permissions=(Capability.TERMINAL_EXECUTE,),
    )

    def __init__(self) -> None:
        self.initialized = False
        self.closed = False
        self.commands: list[str] = []

    def initialize(self) -> None:
        self.initialized = True

    def validate_permission(self, capability: Capability) -> None:
        if capability is not Capability.TERMINAL_EXECUTE:
            raise PermissionDeniedError(f"终端连接器不支持能力 {capability}")

    async def execute(self, action: str, parameters: dict[str, Any]) -> dict[str, Any]:
        command = str(parameters.get("command", ""))
        self.commands.append(command)
        return {"stdout": f"ran: {command}", "exit_code": 0}

    def health_check(self) -> bool:
        return self.initialized and not self.closed

    def close(self) -> None:
        self.closed = True


# --- CLI Agent 假可执行文件 ---


def write_cli_script(path: Path, body: str) -> str:
    """造一个两平台都能被直接启动的假 CLI。

    POSIX 靠 shebang + 可执行位；Windows 上无扩展名脚本不是有效可执行文件
    （CreateProcess 报 WinError 193），因此写一份 `.py` 脚本 + 一份 `.cmd` 包装，返回
    `.cmd` 路径——被测的 adapter 会经 `platforms.executable_argv` 用 `cmd.exe /c` 启动它。
    """
    if os.name == "nt":
        script = path.with_suffix(".py")
        script.write_text(body, encoding="utf-8", newline="\n")
        wrapper = path.with_suffix(".cmd")
        # 假 CLI 的 JSON 事件里有中文：Windows 的 Python 子进程默认按 locale（cp1252）
        # 解码 argv/stdin，PYTHONUTF8=1 让它与真实 CLI（原生 UTF-8）行为一致。
        wrapper.write_text(
            f'@echo off\r\nset PYTHONUTF8=1\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
            newline="",
        )
        return str(wrapper)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def mark_runtime_ready(container: Any, name: str) -> None:
    """把 installation 直接置为 READY（不真实探测本机 CLI，保持用例确定性）。"""

    async def _upsert() -> None:
        await container.installations._repository.upsert(  # noqa: SLF001 - 测试直接落库
            CliAgentProbe(name=name, adapter=name, status=AgentInstallStatus.READY),
            status=AgentInstallStatus.READY,
        )

    asyncio.run(_upsert())


_FAKE_CLAUDE_TEMPLATE = """#!/usr/bin/env python3
import json, os, sys, time

_LOG = %(log)r
_SENTINEL = %(sentinel)r
_HOLD = %(hold)r
_SESSION = "sess-" + str(os.getpid())

# 每次启动都把 argv 原样落一行：用例据此断言「Agent 收到了什么」
with open(_LOG, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False), flush=True)


emit({"type": "system", "subtype": "init", "session_id": _SESSION})
if _HOLD:
    # 一直不结束，模拟「执行中的 Agent」；夹具收尾时 touch sentinel 放行
    while not os.path.exists(_SENTINEL):
        time.sleep(0.05)
    sys.exit(0)
emit({"type": "stream_event", "event": {"type": "content_block_delta",
      "delta": {"type": "text_delta", "text": "done"}}})
emit({"type": "result", "subtype": "success", "session_id": _SESSION,
      "result": "done", "is_error": False})
"""


def write_fake_claude(
    binary: Path,
    *,
    log_path: Path,
    sentinel: Path,
    hold: bool = False,
) -> str:
    """写一个假 `claude`：记录 argv，吐 init / text_delta / result 三类事件。

    `hold=True` 时进程停在 init 之后不退出，用来测试取消与「执行中」的状态流转。
    """
    body = _FAKE_CLAUDE_TEMPLATE % {
        "log": str(log_path),
        "sentinel": str(sentinel),
        "hold": hold,
    }
    return write_cli_script(binary, body)


def read_cli_argv(log_path: Path) -> list[list[str]]:
    """读回假 CLI 记录的所有 argv（每次进程启动一行 JSON）。"""
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
