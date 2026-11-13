"""P2-1 / P2-2：Flux 主动拉起外部 CLI Agent 的确定性测试。

用**假 CLI 脚本**（打印 JSON 行后 exit 0 / 非 0 / 忽略 SIGTERM）覆盖
completed / failed / cancel 三条路径，真实经过 `CliRuntime` → `CliAdapter` →
`platforms.popen_kwargs` 进程组隔离 → 现有 `RunSupervisor` 的完整链路，
不启动真实 Codex / OpenCode，也不调用真实模型。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from flux.config import Settings
from flux.core.agent_runtime import platforms
from flux.core.agent_runtime.adapters import OpenCodeAdapter
from flux.core.agent_runtime.adapters.base import CliAgentProbe
from flux.core.agent_runtime.manager import AgentSpec
from flux.core.agent_runtime.runtimes.base import (
    DEFAULT_RUNTIME,
    RUNTIME_CLAUDE,
    RUNTIME_CODEX,
    RUNTIME_OPENCODE,
)
from flux.core.agent_runtime.runtimes.cli import (
    CliRuntime,
    _accumulate_usage,
    _codex_config,
    _normalize_codex_event,
    _normalize_opencode_event,
    _opencode_config,
    _read_user_mcp_servers,
    _RunUsage,
    _upstream_failure,
)
from flux.enums import AgentInstallStatus, AgentRole
from flux.errors import ConfigurationError, ValidationError
from flux.main import create_app

PREFIX = "/api/v1"

_COMPLETED_SCRIPT = """#!/usr/bin/env python3
import json, sys

def send(payload):
    print(json.dumps(payload), flush=True)

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    rid = message.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "thread/start":
        send({"jsonrpc": "2.0", "id": rid, "result": {"thread": {"id": "t1"}}})
    elif method == "turn/start":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
        send({"jsonrpc": "2.0", "method": "item/started", "params": {"item": {
            "id": "c1", "type": "commandExecution", "command": "ls", "status": "inProgress"}}})
        send({"jsonrpc": "2.0", "method": "item/completed", "params": {"item": {
            "id": "c1", "type": "commandExecution", "command": "ls",
            "status": "completed", "aggregatedOutput": "ok"}}})
        for chunk in ["working", " ", "done"]:
            send({"jsonrpc": "2.0", "method": "item/agentMessage/delta",
                  "params": {"itemId": "a1", "delta": chunk}})
        send({"jsonrpc": "2.0", "method": "item/completed", "params": {"item": {
            "id": "a1", "type": "agentMessage", "text": "working done"}}})
        send({"jsonrpc": "2.0", "method": "turn/completed",
              "params": {"turn": {"id": "u1", "status": "completed"}}})
        break
sys.exit(0)
"""

_FAILED_SCRIPT = """#!/usr/bin/env python3
import sys
sys.exit(2)
"""

_FAKE_OPENCODE_SERVER_TEMPLATE = '''#!/usr/bin/env python3
"""仿 opencode serve：健康检查 / 建会话 / SSE 增量 / 异步回合，覆盖 server 模式链路。

回合协议与 1.18.29 对齐：`POST /session/{id}/prompt_async` 立即 204，随后 SSE 推增量并
以 `session.idle` 收口；`GET /session/status` 是兜底，`GET /session/{id}/message` 拉终态。
"""
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = "__MODE__"
# 进程复用用例靠这个文件数「真实起了几个 serve」——每轮冷启动一次就会多一行
SPAWN_LOG = os.environ.get("FAKE_OPENCODE_SPAWN_LOG")
if SPAWN_LOG:
    with open(SPAWN_LOG, "a") as fh:
        fh.write(str(os.getpid()) + "\\n")
STARTED = threading.Event()
FINISHED = threading.Event()
FULL = "done"
TOKENS = {"input": 1000, "output": 80, "reasoning": 0, "cache": {"read": 400, "write": 20}}

if MODE == "cancel":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

if MODE == "quota":
    print(
        'level=ERROR run=abc message="stream error" providerID=opencode '
        'modelID=deepseek-v4.1-flash-free session.id=s1 small=false agent=build '
        'mode=primary error.error="AI_APICallError: Free usage limit reached. '
        'Subscribe to OpenCode Go to keep going"',
        file=sys.stderr,
        flush=True,
    )
    while True:
        time.sleep(0.1)

if MODE == "title":
    print(
        'level=ERROR run=abc message="stream error" providerID=opencode modelID=gpt-nano '
        'small=true agent=title error.error="AI_APICallError: Insufficient account funds"',
        file=sys.stderr,
        flush=True,
    )


def build():
    events = [
        {
            "type": "message.updated",
            "properties": {
                "sessionID": "ses_fake",
                "info": {"id": "msg_a", "role": "assistant", "sessionID": "ses_fake"},
            },
        },
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_fake",
                "part": {"id": "prt_t", "type": "text", "messageID": "msg_a", "text": ""},
            },
        },
    ]
    if MODE == "stream":
        chunks = [
            "流式输出",
            "已经接通",
            "，这是第二段正文内容",
            "。后面还有第三段和第四段，",
            "用来验证增量是逐段推送的。",
        ]
    else:
        chunks = ["done"]
    full = "".join(chunks)
    for chunk in chunks:
        events.append(
            {
                "type": "message.part.delta",
                "properties": {
                    "sessionID": "ses_fake",
                    "messageID": "msg_a",
                    "partID": "prt_t",
                    "field": "text",
                    "delta": chunk,
                },
            }
        )
    if MODE == "usage":
        events.append(
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "ses_fake",
                    "part": {
                        "id": "prt_1",
                        "type": "tool",
                        "messageID": "msg_a",
                        "tool": "workspace.read",
                        "callID": "call_1",
                        "state": {
                            "status": "completed",
                            "input": {"path": "a.py"},
                            "output": "ok",
                        },
                    },
                },
            }
        )
        events.append(
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "ses_fake",
                    "part": {
                        "id": "prt_2",
                        "type": "tool",
                        "messageID": "msg_a",
                        "tool": "workspace.search",
                        "callID": "call_2",
                        "state": {"status": "error", "input": {"q": "x"}, "output": ""},
                    },
                },
            }
        )
    events.append(
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_fake",
                "part": {
                    "id": "prt_s",
                    "type": "step-finish",
                    "messageID": "msg_a",
                    "tokens": TOKENS,
                    "cost": 0.0,
                },
            },
        }
    )
    events.append({"type": "session.idle", "properties": {"sessionID": "ses_fake"}})
    return events, full


EVENTS, FULL = build()


def sse_line(payload):
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\\n\\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        route = self.path.split("?")[0]
        if route == "/global/health":
            self._json(200, {"healthy": True})
            return
        if route == "/session/status":
            # 不在返回表里 = idle（opencode 语义）；cancel 模式永远 busy
            if MODE == "cancel":
                self._json(200, {"ses_fake": {"type": "busy"}})
            elif FINISHED.is_set():
                self._json(200, {})
            else:
                self._json(200, {"ses_fake": {"type": "busy"}})
            return
        if route.startswith("/session/") and route.endswith("/message"):
            # 终态消息：本轮 assistant 的 parts 是权威答复
            self._json(
                200,
                [
                    {
                        "info": {
                            "id": "msg_a",
                            "role": "assistant",
                            "sessionID": "ses_fake",
                            "time": {"created": int(time.time() * 1000)},
                        },
                        "parts": [{"type": "text", "text": FULL}],
                    }
                ],
            )
            return
        if route != "/event":
            self._json(404, {"message": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(sse_line({"type": "server.connected", "properties": {}}).encode())
        self.wfile.flush()
        STARTED.wait(30)
        if MODE == "cancel":
            while True:
                self.wfile.write(b": ping\\n\\n")
                self.wfile.flush()
                time.sleep(1)
        for item in EVENTS:
            self.wfile.write(sse_line(item).encode())
            self.wfile.flush()
            time.sleep(0.01)
        FINISHED.set()
        time.sleep(1)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path == "/session":
            self._json(200, {"id": "ses_fake", "title": "fake"})
            return
        if self.path.endswith("/prompt_async"):
            # 异步提交：立即 204，实际回合由 SSE 推送并在 session.idle 收口
            STARTED.set()
            self.send_response(204)
            self.end_headers()
            return
        if self.path.endswith("/abort"):
            self._json(200, True)
            return
        self._json(404, {"message": "not found"})


ThreadingHTTPServer.daemon_threads = True
PORT = 0
for index, value in enumerate(sys.argv):
    if value == "--port":
        PORT = int(sys.argv[index + 1])
ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
'''


def _fake_opencode_server(mode: str) -> str:
    return _FAKE_OPENCODE_SERVER_TEMPLATE.replace("__MODE__", mode)


_CANCEL_SCRIPT = """#!/usr/bin/env python3
import json, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)

def send(payload):
    print(json.dumps(payload), flush=True)

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    rid = message.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "thread/start":
        send({"jsonrpc": "2.0", "id": rid, "result": {"thread": {"id": "t1"}}})
    elif method == "turn/start":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
        while True:
            time.sleep(0.1)
"""

#: 仿真实 codex app-server：stdout 是 JSON-RPC，stderr 是日志噪声（Info/提示）
_NOISY_CODEX_SCRIPT = """#!/usr/bin/env python3
import json, sys
print("2026-10-06T00:00:00Z  INFO codex_otel: noisy log line", file=sys.stderr, flush=True)

def send(payload):
    print(json.dumps(payload), flush=True)

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    rid = message.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "thread/start":
        send({"jsonrpc": "2.0", "id": rid, "result": {"thread": {"id": "t1"}}})
    elif method == "turn/start":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
        send({"jsonrpc": "2.0", "method": "item/started", "params": {"item": {
            "id": "m1", "type": "mcpToolCall", "tool": "context_get", "arguments": {}}}})
        send({"jsonrpc": "2.0", "method": "item/completed", "params": {"item": {
            "id": "m1", "type": "mcpToolCall", "tool": "context_get",
            "arguments": {}, "status": "completed", "result": {"ok": True}}}})
        # 不带增量，直接给完成态：验证「整段兜底也只推一次」
        send({"jsonrpc": "2.0", "method": "item/completed", "params": {"item": {
            "id": "i3", "type": "agentMessage", "text": "报告：只读完成，未改动任何文件。"}}})
        send({"jsonrpc": "2.0", "method": "turn/completed",
              "params": {"turn": {"id": "u1", "status": "completed"}}})
        break
sys.exit(0)
"""


#: 仿「可跨轮复用」的 codex app-server：常驻读 stdin，每轮 turn/start 回一段增量正文
_POOLED_CODEX_SCRIPT = """#!/usr/bin/env python3
import json, os, sys
SPAWN_LOG = os.environ.get("FAKE_CODEX_SPAWN_LOG")
if SPAWN_LOG:
    with open(SPAWN_LOG, "a") as fh:
        fh.write(str(os.getpid()) + "\\n")
print("2026-10-06T00:00:00Z  INFO codex_otel: noisy log line", file=sys.stderr, flush=True)
turns = {"n": 0}

def send(payload):
    print(json.dumps(payload), flush=True)

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    rid = message.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method in ("thread/start", "thread/resume"):
        send({"jsonrpc": "2.0", "id": rid, "result": {"thread": {"id": "t-pool"}}})
    elif method == "turn/start":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
        turns["n"] += 1
        send({"jsonrpc": "2.0", "method": "item/agentMessage/delta",
              "params": {"itemId": "a%d" % turns["n"], "delta": "第%d轮答复" % turns["n"]}})
        send({"jsonrpc": "2.0", "method": "turn/completed",
              "params": {"turn": {"id": "u%d" % turns["n"], "status": "completed"}}})
"""


def _write_script(path: Path, body: str) -> str:
    """造一个两平台都能被直接启动的假 CLI。

    POSIX 靠 shebang + 可执行位；Windows 上无扩展名脚本不是有效可执行文件
    （CreateProcess 报 WinError 193），因此写一份 `.py` 脚本 + 一份 `.cmd` 包装，返回
    `.cmd` 路径——被测的 adapter 会经 `platforms.executable_argv` 用 `cmd.exe /c` 启动它。
    """
    if os.name == "nt":
        script = path.with_suffix(".py")
        script.write_text(body, encoding="utf-8", newline="\n")
        wrapper = path.with_suffix(".cmd")
        # Flux 侧按 UTF-8 写 JSON-RPC（含中文指令）；Windows 的 Python 子进程默认按
        # locale（cp1252）读 stdin，会因 0x9d 之类未定义字节直接 UnicodeDecodeError。
        # PYTHONUTF8=1 让假 CLI 与真实 codex（Rust 原生 UTF-8）行为一致。
        wrapper.write_text(
            f'@echo off\r\nset PYTHONUTF8=1\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
            newline="",
        )
        return str(wrapper)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _cli_settings(settings: Settings, tmp_path: Path) -> Settings:
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    return settings.model_copy(
        update={
            "runtime_home": str(tmp_path / "runtime-home"),
            "runtime_workspace": str(tmp_path / "runtime-ws"),
            "workspace_root": str(root),
            "mcp_enabled": True,
            "mcp_url": "http://127.0.0.1:8000/mcp",
        }
    )


@contextmanager
def _cli_app(
    settings: Settings,
    tmp_path: Path,
    binary: str,
    *,
    env_name: str = "FLUX_CODEX_CLI_BINARY",
) -> Iterator[TestClient]:
    app = create_app(_cli_settings(settings, tmp_path))
    previous = os.environ.get(env_name)
    os.environ[env_name] = binary
    try:
        with TestClient(app) as client:
            yield client
    finally:
        if previous is None:
            os.environ.pop(env_name, None)
        else:
            os.environ[env_name] = previous


def _mark_ready(container: object, name: str) -> None:
    """把 installation 直接置为 READY（不真实探测本机 CLI，保持用例确定性）。"""

    async def _upsert() -> None:
        await container.installations._repository.upsert(  # noqa: SLF001 - 测试直接落库
            CliAgentProbe(name=name, adapter=name, status=AgentInstallStatus.READY),
            status=AgentInstallStatus.READY,
        )

    asyncio.run(_upsert())


def _create_cli_agent(client: TestClient, runtime: str) -> str:
    response = client.post(
        f"{PREFIX}/agents",
        json={
            "name": f"cli-{runtime}-{uuid.uuid4().hex[:6]}",
            "role": "developer",
            "runtime": runtime,
            "permissions": ["file.read", "file.write"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["id"]


def _start_task(client: TestClient, agent_id: str) -> dict:
    created = client.post(
        f"{PREFIX}/tasks", json={"description": "改一下 store.py", "agent_id": agent_id}
    )
    assert created.status_code == 200, created.text
    task_id = created.json()["data"]["id"]
    started = client.post(f"{PREFIX}/tasks/{task_id}/start", json={})
    assert started.status_code == 200, started.text
    return started.json()["data"]


def _wait_status(client: TestClient, task_id: str, wanted: str, *, timeout: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout
    task: dict = {}
    while time.monotonic() < deadline:
        task = client.get(f"{PREFIX}/tasks/{task_id}").json()["data"]
        if task["status"] == wanted:
            return task
        time.sleep(0.02)
    raise AssertionError(f"任务未在 {timeout}s 内进入 {wanted}，当前 {task.get('status')}")


def _wait_last_message(
    client: TestClient, task_id: str, kind: str, *, timeout: float = 8.0
) -> dict:
    """状态与结果消息是两次写入，等消息落库再断言，避免踩进毫秒级窗口。"""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        messages = client.get(f"{PREFIX}/tasks/{task_id}/messages").json()["data"]
        last = messages[-1] if messages else {}
        if last.get("kind") == kind:
            return last
        time.sleep(0.02)
    raise AssertionError(f"最后一条消息未在 {timeout}s 内变成 {kind}，当前 {last.get('kind')}")


def test_cli_run_completes_and_emits_unified_events(
    settings: Settings, tmp_path: Path, db_schema: None
) -> None:
    binary = _write_script(tmp_path / "fake-codex-ok", _COMPLETED_SCRIPT)
    with _cli_app(settings, tmp_path, binary) as client:
        container = client.app.state.container
        _mark_ready(container, RUNTIME_CODEX)
        agent_id = _create_cli_agent(client, RUNTIME_CODEX)
        started = _start_task(client, agent_id)
        task_id = started["task"]["id"]
        run_id = started["task"]["run_id"]
        assert run_id and started["run"]["run_id"] == run_id
        assert started["run"]["runtime"] == RUNTIME_CODEX

        settled = _wait_status(client, task_id, "completed")
        assert settled["run_id"] == run_id

        # 事件流经事件总线发布（统一词表），含 tool_call
        events = [
            payload
            for event, payload in container.bus.history
            if event == "dsh.event" and payload.get("run_id") == run_id
        ]
        types = [e["event_type"] for e in events]
        assert "tool_call" in types and "tool_result" in types and "message" in types

        # run 目录保留（供排障），配置已收权
        run_dir = Path(container.settings.runtime_home) / "runs" / run_id
        assert run_dir.is_dir()
        # codex 的 CODEX_HOME 按任务固定，才能跨轮 thread/resume；
        # 登录态与 config 仍每轮重写，隔离性不变。
        task_home = Path(container.settings.runtime_home) / "runs" / f"task-{task_id}"
        config = task_home / "codex-home" / "config.toml"
        assert config.is_file()
        # 收敛到仅属主可访问：POSIX 直接用 st_mode；Windows 的 stat 不反映 chmod，
        # 校验平台原语 secure_file（icacls）确实成功，而不是删掉这条断言。
        if platforms.IS_WINDOWS:
            assert platforms.secure_file(config) is True
        else:
            assert oct(config.stat().st_mode & 0o777) == "0o600"
        assert "mcp_servers.flux" in config.read_text(encoding="utf-8")


def test_codex_config_openai_uses_builtin_provider(settings: Settings) -> None:
    """默认（openai）：不写供应商块，只注入 MCP——与改造前的行为一致。"""
    text = _codex_config("http://127.0.0.1:8801/mcp", "tok", settings=settings)
    assert "model_providers" not in text
    assert "[mcp_servers.flux]" in text
    # 不放行 MCP 工具审批的话，codex 会判定「MCP 不可用」而退回自己读文件
    assert 'default_tools_approval_mode = "approve"' in text


def test_cli_mcp_config_binds_the_run_id_header(settings: Settings) -> None:
    """CLI 通道的 MCP 配置要把连接绑到本次 Run：step 摄入与 context.steps 都靠它归属。"""
    run_id = "run-bind-123"

    opencode = json.loads(_opencode_config("http://127.0.0.1:8802/mcp", "tok", run_id))
    headers = opencode["mcp"]["flux"]["headers"]
    assert headers["Authorization"] == "Bearer tok"
    assert headers["X-Flux-Run-Id"] == run_id

    codex = _codex_config("http://127.0.0.1:8802/mcp", "tok", settings=settings, run_id=run_id)
    assert f'X-Flux-Run-Id = "{run_id}"' in codex
    assert "X-Flux-Task-Id" not in codex


def test_codex_config_pooled_process_binds_the_task_id_header(settings: Settings) -> None:
    """常驻 codex 进程的配置只写一次、令牌是任务级：改绑任务 id，由 MCP 解析当前活动 Run。"""
    codex = _codex_config("http://127.0.0.1:8802/mcp", "tok", settings=settings, task_id="task-7")
    assert 'X-Flux-Task-Id = "task-7"' in codex
    assert "X-Flux-Run-Id" not in codex


def test_codex_config_merges_user_mcp_servers(settings: Settings) -> None:
    """隔离 CODEX_HOME 也要带上用户自建 MCP；`flux` 这个 key 始终由 Flux 独占。"""
    text = _codex_config(
        "http://127.0.0.1:8801/mcp",
        "tok",
        settings=settings,
        user_mcp_servers={
            "playwright": {
                "command": "npx",
                "args": ["-y", "@playwright/mcp"],
                "env": {"PW": "1"},
            },
            "flux": {"command": "should-be-ignored"},
        },
    )
    assert "[mcp_servers.playwright]" in text
    assert 'command = "npx"' in text
    assert 'args = ["-y", "@playwright/mcp"]' in text
    assert "[mcp_servers.playwright.env]" in text
    assert 'PW = "1"' in text
    # 用户的同名条目不能覆盖本次 Run 的 Flux 端点（含一次性令牌）
    assert "should-be-ignored" not in text
    assert "[mcp_servers.flux]" in text


def test_read_user_mcp_servers_ignores_missing_or_broken_config(tmp_path: Path) -> None:
    """用户配置缺失/写坏都只当「没有用户 MCP」，不阻断 Run。"""
    assert _read_user_mcp_servers(tmp_path / "missing.toml") == {}
    broken = tmp_path / "broken.toml"
    broken.write_text("not = [valid", encoding="utf-8")
    assert _read_user_mcp_servers(broken) == {}


def test_read_user_mcp_servers_keeps_only_mcp_table(tmp_path: Path) -> None:
    """只取 mcp_servers：model / profile 等键留在用户目录，不进本次 Run 的配置。"""
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                'model = "gpt-5"',
                "",
                "[mcp_servers.docs]",
                'command = "uvx"',
                'args = ["docs-mcp"]',
                "",
                "[mcp_servers.docs.env]",
                'API_KEY = "k"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    servers = _read_user_mcp_servers(path)
    assert set(servers) == {"docs"}
    assert servers["docs"]["command"] == "uvx"
    assert servers["docs"]["env"] == {"API_KEY": "k"}


def test_write_runtime_config_links_user_assets(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FLUX_CODEX_USER_HOME 指向用户 CODEX_HOME：MCP 合并、skills 软链、AGENTS.md 复制。"""
    user_home = tmp_path / "user-codex"
    (user_home / "skills" / "demo").mkdir(parents=True)
    (user_home / "config.toml").write_text(
        '[mcp_servers.docs]\ncommand = "uvx"\n', encoding="utf-8"
    )
    (user_home / "AGENTS.md").write_text("# 用户规则\n", encoding="utf-8")
    monkeypatch.setenv("FLUX_CODEX_USER_HOME", str(user_home))

    runtime = SimpleNamespace(_settings=settings, runtime_id=RUNTIME_CODEX)
    runtime._secure = lambda path: None
    runtime._inject_account_auth = lambda home: None

    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    env = CliRuntime._write_runtime_config(runtime, run_dir, "tok", "run-1")

    home = run_dir / "codex-home"
    assert env["CODEX_HOME"] == str(home)
    text = (home / "config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.docs]" in text
    assert "[mcp_servers.flux]" in text
    assert (home / "skills").is_symlink()
    assert (home / "AGENTS.md").read_text(encoding="utf-8") == "# 用户规则\n"


def test_opencode_config_applies_the_task_model_only_for_this_run() -> None:
    """任务级模型覆盖落在按 Run 隔离的配置里：换模型不能改用户的全局默认。"""
    default = json.loads(_opencode_config("http://127.0.0.1:8802/mcp", "tok", "r1"))
    assert "model" not in default

    override = json.loads(
        _opencode_config("http://127.0.0.1:8802/mcp", "tok", "r1", model="deepseek/deepseek-flash")
    )
    assert override["model"] == "deepseek/deepseek-flash"
    assert override["mcp"]["flux"]["headers"]["X-Flux-Run-Id"] == "r1"


def test_codex_event_normalisation_maps_items() -> None:
    """codex `--json` 是 item / turn 两级结构：只有 agent_message 进答复，其余归类。"""
    run_id = "r1"

    def _type(payload: dict) -> str:
        return _normalize_codex_event(payload, run_id, RUNTIME_CODEX).type

    assert _type({"type": "thread.started"}) == "status"
    assert _type({"type": "turn.completed", "usage": {}}) == "final"
    assert _type({"type": "turn.failed", "error": {"message": "boom"}}) == "error"
    assert (
        _type({"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}})
        == "message"
    )
    assert _type({"type": "item.completed", "item": {"type": "reasoning"}}) == "thinking"
    reasoning = _normalize_codex_event(
        {
            "type": "item.completed",
            "item": {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "先看目录"}],
            },
        },
        run_id,
        RUNTIME_CODEX,
    )
    assert reasoning.data["text"] == "先看目录"
    assert (
        _type({"type": "item.completed", "item": {"type": "error", "message": "warn"}}) == "error"
    )
    assert (
        _type({"type": "item.completed", "item": {"type": "mcp_tool_call", "tool": "context_get"}})
        == "tool_call"
    )
    assert (
        _type({"type": "item.completed", "item": {"type": "command_execution", "command": "ls"}})
        == "tool_call"
    )
    command = _normalize_codex_event(
        {
            "type": "item.completed",
            "item": {
                "id": "c1",
                "type": "command_execution",
                "command": "ls -la",
                "aggregated_output": "a.txt\n",
                "status": "completed",
            },
        },
        run_id,
        RUNTIME_CODEX,
    )
    assert command.data["name"] == "shell"
    assert command.data["detail"] == "ls -la"
    assert command.data["output"] == "a.txt"
    assert command.data["call_id"] == "c1"
    # 非 JSON 行（parse_event 带 non_json 标记）不进答复：CLI 的日志噪声不许冒充 Agent 输出
    assert (
        _type({"type": "text", "text": "Reading additional input from stdin...", "non_json": True})
        == "status"
    )
    message_event = _normalize_codex_event(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "结论"}},
        run_id,
        "codex",
    )
    assert message_event.data["text"] == "结论"


def test_opencode_event_normalisation_reads_part_payload() -> None:
    """opencode `--format json` 的 text/tool 都在 part 里；不取 part 会把答复写成空串。"""
    run_id = "r1"

    def _event(payload: dict) -> tuple[str, dict]:
        event = _normalize_opencode_event(payload, run_id, RUNTIME_OPENCODE)
        return event.type, event.data

    event_type, data = _event({"type": "text", "part": {"type": "text", "text": "结论"}})
    assert event_type == "message" and data["text"] == "结论"
    # 空文本块（工具调用前的占位）不能冒充答复
    assert _event({"type": "text", "part": {"type": "text", "text": "  "}})[0] == "status"
    event_type, data = _event(
        {"type": "tool_use", "part": {"type": "tool", "tool": "flux_workspace_read"}}
    )
    assert event_type == "tool_call" and data["name"] == "flux_workspace_read"
    event_type, data = _event(
        {
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": "bash",
                "callID": "call-1",
                "state": {
                    "status": "completed",
                    "title": "ls",
                    "input": {"command": "ls -la"},
                    "output": "a.txt\n",
                    "metadata": {"output": "a.txt\n", "exit": 0},
                },
            },
        }
    )
    # 命令卡要拿到命令、状态与输出；只有名字的话手机端只能显示「正在执行」
    assert event_type == "tool_call"
    assert data["detail"] == "ls -la"
    assert data["output"] == "a.txt"
    assert data["status"] == "completed"
    assert data["title"] == "ls"
    assert data["call_id"] == "call-1"
    # 每一步的起点是一个「思考」刻度；带推理文本的 reasoning 也一样
    assert _event({"type": "step_start", "part": {"type": "step-start"}})[0] == "thinking"
    event_type, data = _event(
        {"type": "reasoning", "part": {"type": "reasoning", "text": "先确认目录结构"}}
    )
    assert event_type == "thinking" and data["text"] == "先确认目录结构"


def test_execution_instruction_keeps_stable_prefix_before_dynamic_task() -> None:
    """稳定约束必须排在任务描述和 task_id 之前，否则每次 Run 的前缀缓存都会失效。"""
    from flux.core.agent_runtime.runtimes.service import _execution_instruction

    text = _execution_instruction(
        SimpleNamespace(description="TASK-BODY", id="task-1", project_id="proj-1"), None, []
    )
    assert text.startswith("## 执行约束")
    assert text.index("## 执行约束") < text.index("TASK-BODY")
    assert text.index("TASK-BODY") < text.index("## 平台身份")
    assert text.index("## 平台身份") < text.index("task-1")


def test_execution_instruction_requires_create_after_preview() -> None:
    """预演不产生提案：指令必须要求预演通过后再用 proposal.create 交卷。"""
    from flux.core.agent_runtime.runtimes.service import _execution_instruction

    text = _execution_instruction(
        SimpleNamespace(description="TASK-BODY", id="task-1", project_id="proj-1"), None, []
    )
    assert "proposal.preview 只是预演" in text
    assert "proposal.create 提交同一份 payload" in text


def test_opencode_step_finish_usage_is_accumulated() -> None:
    """每步的 tokens 必须由 Flux 自己累计，否则只能事后去翻 OpenCode 的库。"""
    usage = _RunUsage()
    step = _normalize_opencode_event(
        {
            "type": "step_finish",
            "part": {
                "type": "step-finish",
                "cost": 0.5,
                "tokens": {"input": 1000, "output": 80, "cache": {"read": 400, "write": 20}},
            },
        },
        "r1",
        RUNTIME_OPENCODE,
    )
    _accumulate_usage(usage, step, {"type": "step_finish"})
    tool = _normalize_opencode_event(
        {"type": "tool_use", "part": {"type": "tool", "tool": "x", "state": {"status": "error"}}},
        "r1",
        RUNTIME_OPENCODE,
    )
    _accumulate_usage(usage, tool, {"type": "tool_use"})
    summary = usage.to_dict(runtime_id=RUNTIME_OPENCODE, model="m")
    assert summary["steps"] == 1
    assert summary["tool_calls"] == 1 and summary["tool_errors"] == 1
    assert summary["input_tokens"] == 1000 and summary["output_tokens"] == 80
    assert summary["cache_read_tokens"] == 400 and summary["cache_write_tokens"] == 20
    assert summary["cost"] == 0.5


def test_opencode_run_records_native_metrics(
    settings: Settings, tmp_path: Path, db_schema: None
) -> None:
    """Run 终态后，Flux 自己的 run 记录里要有步数/工具/token，供预算报表使用。"""
    binary = _write_script(tmp_path / "fake-opencode-usage", _fake_opencode_server("usage"))
    with _cli_app(settings, tmp_path, binary, env_name="FLUX_OPENCODE_CLI_BINARY") as client:
        container = client.app.state.container
        _mark_ready(container, RUNTIME_OPENCODE)
        agent_id = _create_cli_agent(client, RUNTIME_OPENCODE)
        started = _start_task(client, agent_id)
        task_id = started["task"]["id"]
        run_id = started["task"]["run_id"]
        _wait_status(client, task_id, "completed")

        # 状态落定与指标写入是两次 UPDATE，给一个很短的观察窗
        deadline = time.monotonic() + 2.0
        run = None
        while time.monotonic() < deadline:
            run = asyncio.run(container.run_repo.get(run_id))
            if run is not None and (run.last_event or {}).get("event_type") == "run_metrics":
                break
            time.sleep(0.02)
        metrics = (run.last_event or {}) if run is not None else {}
        assert metrics["event_type"] == "run_metrics"
        assert metrics["runtime"] == RUNTIME_OPENCODE
        assert metrics["steps"] == 1
        assert metrics["tool_calls"] == 2 and metrics["tool_errors"] == 1
        assert metrics["input_tokens"] == 1000 and metrics["output_tokens"] == 80
        assert metrics["cache_read_tokens"] == 400


def test_opencode_server_streams_text_incrementally(
    settings: Settings, tmp_path: Path, db_schema: None
) -> None:
    """server 模式要逐段推正文：不是等整段生成完才一次性蹦出来。"""
    binary = _write_script(tmp_path / "fake-opencode-stream", _fake_opencode_server("stream"))
    with _cli_app(settings, tmp_path, binary, env_name="FLUX_OPENCODE_CLI_BINARY") as client:
        container = client.app.state.container
        _mark_ready(container, RUNTIME_OPENCODE)
        agent_id = _create_cli_agent(client, RUNTIME_OPENCODE)
        started = _start_task(client, agent_id)
        task_id = started["task"]["id"]
        run_id = started["task"]["run_id"]
        _wait_status(client, task_id, "completed")

        last = _wait_last_message(client, task_id, "run_finished")
        assert "流式输出已经接通" in last["content"]

        chunks = [
            payload.get("text") or ""
            for event, payload in container.bus.history
            if event == "dsh.event"
            and payload.get("run_id") == run_id
            and payload.get("event_type") == "message"
        ]
        assert len(chunks) >= 2, f"正文只推了 {len(chunks)} 帧，没有流式效果"
        expected = (
            "流式输出已经接通，这是第二段正文内容。"
            "后面还有第三段和第四段，用来验证增量是逐段推送的。"
        )
        assert "".join(chunks) == expected


def test_cli_prepare_sets_pwd_to_workspace(tmp_path: Path) -> None:
    """OpenCode 用 PWD 判定项目目录；只改 cwd 不改 PWD 会让会话落到 Flux 仓库。"""
    from flux.core.agent_runtime.runtimes.cli import CliRuntime

    settings = Settings(
        workspace_root=str(tmp_path),
        runtime_workspace=str(tmp_path / "ws"),
        runtime_home=str(tmp_path / "home"),
        mcp_enabled=False,
    )
    runtime = CliRuntime(
        runtime_id=RUNTIME_OPENCODE,
        adapter=SimpleNamespace(_path="/usr/local/bin/opencode"),
        settings=settings,
        supervisor=None,
    )

    launch = asyncio.run(
        runtime.prepare(
            task=SimpleNamespace(id="t1"),
            agent=SimpleNamespace(id_str="a1"),
            run_id="r1",
            instruction="hi",
        )
    )

    assert launch.cwd == str(tmp_path)
    assert launch.env["PWD"] == str(tmp_path)
    assert launch.env["FLUX_WORKSPACE"] == str(tmp_path)


def test_codex_config_writes_deepseek_provider_block(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """隔离 CODEX_HOME 里没有登录态：第三方供应商必须自带凭据来源（env_key，密钥不落盘）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-probe-not-a-real-key")
    codex_settings = settings.model_copy(
        update={
            "codex_provider": "deepseek",
            "codex_model": "deepseek-flash",
            "codex_model_catalog": "/opt/codex/models.json",
            "deepseek_base_url": "https://api.deepseek.com",
        }
    )
    text = _codex_config("http://127.0.0.1:8801/mcp", "tok", settings=codex_settings)
    assert 'model = "deepseek-flash"' in text
    assert 'model_provider = "deepseek"' in text
    assert "[model_providers.deepseek]" in text
    assert 'base_url = "https://api.deepseek.com"' in text
    assert 'wire_api = "responses"' in text
    assert 'env_key = "DEEPSEEK_API_KEY"' in text
    assert 'model_catalog_json = "/opt/codex/models.json"' in text
    assert "[mcp_servers.flux]" in text
    # 密钥只以变量名出现：明文绝不写进这次性配置文件
    assert "sk-probe-not-a-real-key" not in text


def test_codex_config_requires_env_key_for_third_party(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """凭据来源缺失时提前报错：不能让 Run 起来才在 codex 日志里发现「未登录」。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    codex_settings = settings.model_copy(
        update={"codex_provider": "deepseek", "codex_model": "deepseek-flash"}
    )
    with pytest.raises(ConfigurationError):
        _codex_config("http://127.0.0.1:8801/mcp", "tok", settings=codex_settings)


def test_codex_config_rejects_unknown_provider(settings: Settings) -> None:
    codex_settings = settings.model_copy(update={"codex_provider": "gemini", "codex_model": "x"})
    with pytest.raises(ConfigurationError):
        _codex_config("http://127.0.0.1:8801/mcp", "tok", settings=codex_settings)


def test_cli_run_keeps_stderr_out_of_reply(
    settings: Settings, tmp_path: Path, db_schema: None
) -> None:
    """真实 codex 的形态：stdout 是 --json 协议流，stderr 是日志。

    答复只取 agent_message；stderr 的噪声单独落 run 目录，绝不冒充 Agent 输出。
    """
    binary = _write_script(tmp_path / "fake-codex-noisy", _NOISY_CODEX_SCRIPT)
    with _cli_app(settings, tmp_path, binary) as client:
        container = client.app.state.container
        _mark_ready(container, RUNTIME_CODEX)
        agent_id = _create_cli_agent(client, RUNTIME_CODEX)
        started = _start_task(client, agent_id)
        task_id = started["task"]["id"]
        run_id = started["task"]["run_id"]

        settled = _wait_status(client, task_id, "completed")
        assert settled["status"] == "completed"
        message = _wait_last_message(client, task_id, "run_finished")
        assert message["content"] == "报告：只读完成，未改动任何文件。"
        assert "INFO" not in message["content"]

        # MCP 工具调用进了事件总线（统一词表），供 UI/排障消费
        types = [
            payload.get("event_type")
            for event, payload in container.bus.history
            if event == "dsh.event" and payload.get("run_id") == run_id
        ]
        assert "tool_call" in types and "message" in types and "final" in types

        # 噪声留在日志里，不丢证据：codex 走常驻进程，stderr 落在任务级配置目录
        stderr_log = (
            Path(container.settings.runtime_home) / "runs" / f"task-{task_id}" / "stderr.log"
        )
        assert stderr_log.is_file()
        assert "noisy log line" in stderr_log.read_text(encoding="utf-8")


def test_cli_run_nonzero_exit_is_failed(
    settings: Settings, tmp_path: Path, db_schema: None
) -> None:
    binary = _write_script(tmp_path / "fake-codex-fail", _FAILED_SCRIPT)
    with _cli_app(settings, tmp_path, binary) as client:
        _mark_ready(client.app.state.container, RUNTIME_CODEX)
        agent_id = _create_cli_agent(client, RUNTIME_CODEX)
        started = _start_task(client, agent_id)
        task_id = started["task"]["id"]

        settled = _wait_status(client, task_id, "failed")
        assert settled["status"] == "failed"
        message = _wait_last_message(client, task_id, "run_finished")
        assert "退出码 2" in message["content"]


@pytest.mark.skipif(
    platforms.IS_WINDOWS,
    reason="依赖 POSIX 进程组信号语义（SIGTERM 可被忽略）；Windows 取消链路另由 platforms 用例覆盖",
)
def test_cli_run_cancel_kills_process_group(
    settings: Settings, tmp_path: Path, db_schema: None
) -> None:
    binary = _write_script(tmp_path / "fake-codex-hold", _CANCEL_SCRIPT)
    with _cli_app(settings, tmp_path, binary) as client:
        container = client.app.state.container
        _mark_ready(container, RUNTIME_CODEX)
        agent_id = _create_cli_agent(client, RUNTIME_CODEX)
        started = _start_task(client, agent_id)
        task_id = started["task"]["id"]
        run_id = started["task"]["run_id"]

        # 明确等到进程进入 RUNNING（pid/pgid 已登记）再取消
        snapshot = None
        for _ in range(300):
            snapshot = container.run_supervisor.monitor_snapshot(run_id)
            if snapshot and snapshot["pgid"]:
                break
            time.sleep(0.02)
        assert snapshot and snapshot["pgid"]

        cancelled = client.post(f"{PREFIX}/tasks/{task_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["data"]["status"] == "cancelled"
        settled = _wait_status(client, task_id, "cancelled")
        assert settled["status"] == "cancelled"


def test_cli_runtime_requires_ready_installation(
    settings: Settings, tmp_path: Path, db_schema: None
) -> None:
    binary = _write_script(tmp_path / "fake-codex-ok2", _COMPLETED_SCRIPT)
    with _cli_app(settings, tmp_path, binary) as client:
        # 不置 READY：启动必须 409 并给出 flux agents connect 指引
        agent_id = _create_cli_agent(client, RUNTIME_CODEX)
        created = client.post(
            f"{PREFIX}/tasks", json={"description": "x", "agent_id": agent_id}
        ).json()["data"]
        response = client.post(f"{PREFIX}/tasks/{created['id']}/start", json={})
        assert response.status_code == 409
        assert "flux agents connect" in response.json()["message"]
        assert client.get(f"{PREFIX}/tasks/{created['id']}").json()["data"]["run_id"] is None


def test_agent_runtime_value_is_validated(settings: Settings, db_schema: None) -> None:
    container = create_app(settings).state.container

    async def _attempt() -> None:
        with pytest.raises(ValidationError):
            await container.agents.create(
                AgentSpec(name="bad", role=AgentRole.DEVELOPER, runtime="nope")
            )

    asyncio.run(_attempt())


def test_default_agent_runtime_is_claude_code(settings: Settings, db_schema: None) -> None:
    container = create_app(settings).state.container

    async def _create() -> str:
        handle = await container.agents.create(AgentSpec(name="默认", role=AgentRole.DEVELOPER))
        return handle.spec.runtime

    assert asyncio.run(_create()) == DEFAULT_RUNTIME
    assert DEFAULT_RUNTIME == RUNTIME_CLAUDE


def test_opencode_auth_probe_parses_config_not_only_auth_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-2 §7.2：provider 内联在配置里就算已认证，不因 auth.json 为空误判 missing。"""
    import json

    config = tmp_path / "opencode.json"
    config.write_text(
        json.dumps({"provider": {"deepseek": {"options": {"apiKey": "sk-local"}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCODE_CONFIG", str(config))
    monkeypatch.setenv("HOME", str(tmp_path))

    def _never_called(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise AssertionError("配置已解析到凭据时不应调用 auth list")

    adapter = OpenCodeAdapter(which=lambda _: "/usr/local/bin/opencode", probe_runner=_never_called)
    assert adapter.check_auth() == "ok"


def test_opencode_auth_probe_does_not_call_empty_auth_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置里没有凭据、auth list 显示 0 credentials：降级为 unknown，而非 missing。"""
    monkeypatch.setenv("OPENCODE_CONFIG", str(tmp_path / "empty.json"))
    # Linux 上 expanduser 读 HOME，Windows 上读 USERPROFILE：两个都指到临时目录，
    # 否则会读到本机真实配置里的内联 apiKey，用例失去意义。
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / "empty.json").write_text("{}", encoding="utf-8")
    adapter = OpenCodeAdapter(
        which=lambda _: "/usr/local/bin/opencode",
        probe_runner=lambda *_a, **_k: (0, "0 credentials"),
    )
    assert adapter.check_auth() == "unknown"


def test_upstream_failure_matches_main_model_only() -> None:
    """额度/鉴权错误只在主模型（small=false）上判失败，标题模型报错不能误杀。"""
    main = (
        'level=ERROR run=abc message="stream error" providerID=opencode '
        "modelID=deepseek-v4.1-flash-free small=false agent=build "
        'error.error="AI_APICallError: Free usage limit reached. Subscribe to OpenCode Go"'
    )
    reason = _upstream_failure(main, RUNTIME_OPENCODE)
    assert reason is not None
    assert "额度" in reason and "deepseek-v4.1-flash-free" in reason
    assert "Free usage limit reached" in reason

    title = main.replace("small=false", "small=true")
    assert _upstream_failure(title, RUNTIME_OPENCODE) is None

    # 网络抖动这类可自愈错误不判失败（运行时会自己重试）
    transient = main.replace("Free usage limit reached", "fetch failed ECONNRESET")
    assert _upstream_failure(transient, RUNTIME_OPENCODE) is None

    # 非 opencode 的日志格式不同，不能套这套规则
    assert _upstream_failure(main, RUNTIME_CODEX) is None
    assert _upstream_failure("INFO all good", RUNTIME_OPENCODE) is None


def test_opencode_quota_error_fails_run_immediately(
    settings: Settings, tmp_path: Path, db_schema: None
) -> None:
    """上游额度用完：几秒内失败并给出原因，而不是干等到 idle 超时（实测 600 秒）。

    真实 opencode 遇到额度错误时 stdout 一行不吐、进程也不退出；修复前用户只能
    看到一个永远转圈的对话。这里用假 CLI 复现同样的 stderr 日志。
    """
    binary = _write_script(tmp_path / "fake-opencode-quota", _fake_opencode_server("quota"))
    with _cli_app(settings, tmp_path, binary, env_name="FLUX_OPENCODE_CLI_BINARY") as client:
        container = client.app.state.container
        _mark_ready(container, RUNTIME_OPENCODE)
        agent_id = _create_cli_agent(client, RUNTIME_OPENCODE)
        started = _start_task(client, agent_id)
        task_id = started["task"]["id"]
        run_id = started["task"]["run_id"]

        # 进程不退出也不会自己报错，必须靠 stderr 识别后立即终止
        settled = _wait_status(client, task_id, "failed", timeout=8.0)
        assert settled["run_id"] == run_id
        last = _wait_last_message(client, task_id, "run_finished")
        assert "额度或鉴权异常" in last["content"]
        assert "Free usage limit reached" in last["content"]

        # 对话流里先推一条错误事件，手机端当场能看到，不必等下一次轮询
        errors = [
            payload
            for event, payload in container.bus.history
            if event == "dsh.event"
            and payload.get("run_id") == run_id
            and payload.get("event_type") == "error"
        ]
        assert errors and "Free usage limit reached" in errors[0]["message"]


def test_opencode_title_model_error_does_not_fail_run(
    settings: Settings, tmp_path: Path, db_schema: None
) -> None:
    """标题模型（small=true）报错只是日志噪音：主任务必须照常完成。"""
    binary = _write_script(tmp_path / "fake-opencode-title", _fake_opencode_server("title"))
    with _cli_app(settings, tmp_path, binary, env_name="FLUX_OPENCODE_CLI_BINARY") as client:
        container = client.app.state.container
        _mark_ready(container, RUNTIME_OPENCODE)
        agent_id = _create_cli_agent(client, RUNTIME_OPENCODE)
        task_id = _start_task(client, agent_id)["task"]["id"]

        settled = _wait_status(client, task_id, "completed", timeout=8.0)
        assert settled["status"] == "completed"


# --- 进程复用（P1-4：通用能力，opencode 是首个实现） ---


def test_cli_process_pool_reuses_idle_entry_and_evicts_expired() -> None:
    """池语义：空闲命中复用、租用中退回独占、超 TTL 摘出待回收。"""
    from flux.core.agent_runtime.runtimes import cli as cli_mod

    class _AliveProcess:
        class _Popen:
            def poll(self) -> None:
                return None

        popen = _Popen()

    key = cli_mod._pool_key(
        runtime_id=RUNTIME_OPENCODE,
        task_id="t1",
        agent_id="a1",
        mode="read_only",
        model="m1",
        channel=None,
    )
    entry = cli_mod._PooledServe(
        key=key,
        runtime_id=RUNTIME_OPENCODE,
        port=40123,
        process=_AliveProcess(),
        argv=("opencode",),
        config_dir=Path("/tmp/flux-pool-test"),
        task_id="t1",
        agent_id="a1",
        mode="read_only",
        model="m1",
    )
    cli_mod._pool_register(entry)
    try:
        hit, stale, busy = cli_mod._pool_acquire(key, run_id="r1", ttl=60)
        assert hit is entry and not stale and not busy
        # 同一任务并发第二轮：不能抢，退回独占进程
        again, _, busy = cli_mod._pool_acquire(key, run_id="r2", ttl=60)
        assert again is None and busy
        # 归还后超过 TTL：摘出交给调用方回收，不能重新租给下一轮
        cli_mod._pool_detach(key, "r1")
        entry.last_used -= 3600
        miss, stale, busy = cli_mod._pool_acquire(key, run_id="r3", ttl=60)
        assert miss is None and stale == [entry] and not busy
    finally:
        with cli_mod._SERVE_POOL_LOCK:
            cli_mod._SERVE_POOL.clear()


def test_opencode_config_binds_pooled_processes_by_task() -> None:
    """常驻进程跨 Run 存活：配置里只能写任务级头，不能写死某一轮的 run_id。"""
    pooled = json.loads(_opencode_config("http://127.0.0.1:8802/mcp", "tok", "", task_id="t1"))
    headers = pooled["mcp"]["flux"]["headers"]
    assert headers["Authorization"] == "Bearer tok"
    assert headers["X-Flux-Task-Id"] == "t1"
    assert "X-Flux-Run-Id" not in headers

    per_run = json.loads(_opencode_config("http://127.0.0.1:8802/mcp", "tok", "r1", task_id="t1"))
    headers = per_run["mcp"]["flux"]["headers"]
    assert headers["X-Flux-Run-Id"] == "r1"
    assert headers["X-Flux-Task-Id"] == "t1"


def test_opencode_pool_reuses_the_server_process(
    settings: Settings, tmp_path: Path, db_schema: None
) -> None:
    """同一任务续聊复用同一个 serve 进程：不再每轮重付一次冷启动。"""
    spawn_log = tmp_path / "spawn.log"
    binary = _write_script(tmp_path / "fake-opencode-pool", _fake_opencode_server("default"))
    previous = os.environ.get("FAKE_OPENCODE_SPAWN_LOG")
    os.environ["FAKE_OPENCODE_SPAWN_LOG"] = str(spawn_log)
    try:
        with _cli_app(settings, tmp_path, binary, env_name="FLUX_OPENCODE_CLI_BINARY") as client:
            container = client.app.state.container
            _mark_ready(container, RUNTIME_OPENCODE)
            agent_id = _create_cli_agent(client, RUNTIME_OPENCODE)
            started = _start_task(client, agent_id)
            task_id = started["task"]["id"]
            first_run = started["task"]["run_id"]
            _wait_status(client, task_id, "completed")

            continued = client.post(
                f"{PREFIX}/tasks/{task_id}/continue", json={"content": "再补充一句"}
            )
            assert continued.status_code == 200, continued.text
            second_run = continued.json()["data"]["task"]["run_id"]
            assert second_run and second_run != first_run
            _wait_status(client, task_id, "completed", timeout=10.0)

            spawns = [line for line in spawn_log.read_text().splitlines() if line.strip()]
            assert len(spawns) == 1, f"同一任务续聊起了 {len(spawns)} 个 serve 进程"
    finally:
        if previous is None:
            os.environ.pop("FAKE_OPENCODE_SPAWN_LOG", None)
        else:
            os.environ["FAKE_OPENCODE_SPAWN_LOG"] = previous


def test_codex_pool_reuses_the_app_server_process(
    settings: Settings, tmp_path: Path, db_schema: None
) -> None:
    """同一任务续聊复用同一个 app-server：不再每轮冷启动 + 重新 initialize。"""
    spawn_log = tmp_path / "codex-spawn.log"
    binary = _write_script(tmp_path / "fake-codex-pool", _POOLED_CODEX_SCRIPT)
    previous = os.environ.get("FAKE_CODEX_SPAWN_LOG")
    os.environ["FAKE_CODEX_SPAWN_LOG"] = str(spawn_log)
    try:
        with _cli_app(settings, tmp_path, binary) as client:
            container = client.app.state.container
            _mark_ready(container, RUNTIME_CODEX)
            agent_id = _create_cli_agent(client, RUNTIME_CODEX)
            started = _start_task(client, agent_id)
            task_id = started["task"]["id"]
            _wait_status(client, task_id, "completed")

            continued = client.post(
                f"{PREFIX}/tasks/{task_id}/continue", json={"content": "再补充一句"}
            )
            assert continued.status_code == 200, continued.text
            _wait_status(client, task_id, "completed", timeout=10.0)

            spawns = [line for line in spawn_log.read_text().splitlines() if line.strip()]
            assert len(spawns) == 1, f"同一任务续聊起了 {len(spawns)} 个 app-server 进程"
    finally:
        if previous is None:
            os.environ.pop("FAKE_CODEX_SPAWN_LOG", None)
        else:
            os.environ["FAKE_CODEX_SPAWN_LOG"] = previous
