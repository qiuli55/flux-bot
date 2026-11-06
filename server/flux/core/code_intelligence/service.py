"""语言服务器进程的生命周期与网关。

一个 `LspSession` 对应「一个工作区 + 一个语言服务器」的常驻子进程。多个
WebSocket 客户端（Web 端、桌面端、同一端的多个窗口）挂在同一个会话上，因此
网关不是透明代理，它必须替客户端处理四件事：

1. **initialize 只做一次**：语言服务器不允许二次握手，所以第一次握手的结果
   缓存起来，后来的客户端直接从缓存回放；
2. **文档生命周期按引用计数**：同一个文件被两个客户端打开时，服务器只收到
   一次 `didOpen`；最后一个客户端关闭时才发 `didClose`；
3. **server → client 请求由网关兜底**：`workspace/configuration` 之类必须有人
   回答，否则服务器会卡住；能兜底的就兜底，兜不了才转给前端；
4. **shutdown / exit 由网关拦下**：客户端断开不能杀掉常驻进程。

所有客户端请求的 id 在网关处重写，避免两个客户端用同一个 id 互相串响应。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flux.core.code_intelligence import rpc
from flux.core.code_intelligence.registry import LanguageServerSpec, spec_by_id
from flux.core.virtual_workspace.apply_engine import resolve_workspace_root

logger = logging.getLogger(__name__)

#: 无客户端连接的会话存活时间；超时回收，避免一堆语言服务器常驻吃内存
IDLE_TIMEOUT_SECONDS = 30 * 60.0
#: 回收扫描间隔
REAPER_INTERVAL_SECONDS = 60.0
#: 单个会话最多挂几个客户端
MAX_CLIENTS_PER_SESSION = 8
#: 子进程 stderr 保留行数（排查崩溃用）
STDERR_RING_SIZE = 80
#: 网关自身请求 id 的起点，避开服务器常用于自身请求的小整数
GATEWAY_ID_BASE = 1_000_000
#: 安装命令的输出上限
INSTALL_OUTPUT_LIMIT = 64 * 1024
#: 优雅退出的等待时间
STOP_TIMEOUT_SECONDS = 3.0

SendMessage = Callable[[dict[str, Any]], Awaitable[None]]
Message = dict[str, Any]


class CodeIntelligenceError(RuntimeError):
    """代码智能层的统一异常基类。"""


class LanguageServerUnavailable(CodeIntelligenceError):
    """语言服务器未安装 / 该语言没有可用服务器。"""


class LanguageServerFailed(CodeIntelligenceError):
    """语言服务器启动失败或启动后立即退出。"""


@dataclass
class ClientHandle:
    """一个已接入的客户端（一次 WebSocket 连接）。"""

    id: str
    send: SendMessage
    #: 本客户端打开过的文档（用于断线时补 didClose）
    opened: set[str] = field(default_factory=set)
    #: 是否已完成握手（用于决定何时回放动态注册）
    initialized: bool = False


@dataclass
class _PendingRequest:
    """网关发出的请求（id 是网关侧的）。"""

    #: 转发给哪个客户端；None 表示网关内部请求，响应直接丢弃
    client_id: str | None
    #: 客户端原始 id，响应要还原成它
    client_request_id: Any
    #: 是否是 initialize，响应需要缓存
    capture_initialize: bool = False


class LspSession:
    """一个常驻的语言服务器进程，以及挂在它上面的客户端。"""

    def __init__(
        self,
        *,
        spec: LanguageServerSpec,
        workspace_root: Path,
        on_close: Callable[[LspSession], None] | None = None,
    ) -> None:
        self.spec = spec
        self.workspace_root = workspace_root
        self.last_activity = time.monotonic()

        self._on_close = on_close
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._closing = False
        self._closed = False

        self._clients: dict[str, ClientHandle] = {}
        self._next_gateway_id = GATEWAY_ID_BASE
        self._pending: dict[int, _PendingRequest] = {}

        #: 缓存下来的 initialize 结果
        self._initialize_result: Message | None = None
        #: initialized 通知是否已透传过
        self._initialized_forwarded = False
        #: 动态注册的能力（用于向新客户端回放）
        self._registrations: list[Message] = []
        #: 文档 URI → 引用计数
        self._open_counts: dict[str, int] = {}
        #: 文档 URI → 网关侧版本号（客户端版本号不可信）
        self._doc_versions: dict[str, int] = {}
        self._stderr: deque[str] = deque(maxlen=STDERR_RING_SIZE)

    # --- 生命周期 -------------------------------------------------------------

    @property
    def key(self) -> tuple[str, str]:
        return (str(self.workspace_root), self.spec.id)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        executable = self.spec.executable()
        if executable is None:
            raise LanguageServerUnavailable(
                f"未找到语言服务器 `{self.spec.executable_name}`（{self.spec.install_hint}）"
            )
        argv = [executable, *self.spec.command[1:]]
        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        try:
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self.workspace_root),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise LanguageServerFailed(f"启动 `{argv[0]}` 失败：{exc}") from exc

        self._reader_task = asyncio.create_task(self._read_loop(), name=f"lsp-read-{self.spec.id}")
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name=f"lsp-stderr-{self.spec.id}"
        )

    async def stop(self) -> None:
        """优雅关闭：先 shutdown/exit，超时再 terminate/kill。"""

        if self._closed:
            return
        self._closing = True
        process = self._process
        if process is not None and process.returncode is None:
            try:
                await self._write_to_server(rpc.make_request(self._next_id(), "shutdown"))
                await self._write_to_server(rpc.make_notification("exit"))
            except (CodeIntelligenceError, ConnectionError, RuntimeError):
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=STOP_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=STOP_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        self._closed = True
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *[task for task in (self._reader_task, self._stderr_task) if task is not None],
            return_exceptions=True,
        )

    # --- 客户端挂载 -----------------------------------------------------------

    async def attach(self, send: SendMessage) -> ClientHandle:
        if len(self._clients) >= MAX_CLIENTS_PER_SESSION:
            raise LanguageServerFailed("该语言服务器连接数已达上限")
        client = ClientHandle(id=uuid.uuid4().hex, send=send)
        self._clients[client.id] = client
        self._touch()
        return client

    async def detach(self, client: ClientHandle) -> None:
        self._clients.pop(client.id, None)
        for uri in list(client.opened):
            client.opened.discard(uri)
            await self._release_document(uri)
        for gateway_id, pending in list(self._pending.items()):
            if pending.client_id == client.id:
                self._pending.pop(gateway_id, None)
        self._touch()

    # --- 客户端 → 服务器 ------------------------------------------------------

    async def handle_client_message(self, client: ClientHandle, message: Message) -> None:
        self._touch()
        method = message.get("method")
        request_id = message.get("id")
        is_request = request_id is not None and method is not None

        if method is None:
            # 客户端对「网关转发的服务器请求」（如动态注册）的响应
            await self._handle_client_response(message)
            return

        if method == "initialize":
            await self._handle_initialize(client, message, is_request)
            return
        if method == "initialized":
            client.initialized = True
            if not self._initialized_forwarded:
                self._initialized_forwarded = True
                await self._forward(client, message)
            await self._replay_registrations(client)
            return
        if method == "shutdown":
            # 常驻进程不能因为某个客户端断开就关掉
            if is_request:
                await self._safe_send(client, rpc.make_response(request_id, result=None))
            return
        if method == "exit":
            return

        if method == "textDocument/didOpen":
            await self._handle_did_open(client, message)
            return
        if method == "textDocument/didClose":
            await self._handle_did_close(client, message)
            return
        if method == "textDocument/didChange":
            await self._handle_did_change(client, message)
            return

        await self._forward(client, message)

    async def _handle_client_response(self, message: Message) -> None:
        """客户端响应网关转发出去的服务器请求。

        网关内部请求（动态注册回放）的响应直接丢弃；转发给客户端的服务器
        请求（比如 `workspace/configuration`）则把 id 还原后还给服务器。
        """

        pending = self._pending.pop(message.get("id"), None)
        if pending is None or pending.client_id is None:
            return
        forwarded = dict(message)
        forwarded["id"] = pending.client_request_id
        await self._write_to_server(forwarded)

    async def _handle_initialize(
        self, client: ClientHandle, message: Message, is_request: bool
    ) -> None:
        if self._initialize_result is not None:
            if is_request:
                await self._safe_send(
                    client, rpc.make_response(message.get("id"), result=self._initialize_result)
                )
            client.initialized = True
            return

        if not is_request:
            return
        gateway_id = self._next_id()
        self._pending[gateway_id] = _PendingRequest(
            client_id=client.id,
            client_request_id=message.get("id"),
            capture_initialize=True,
        )
        outgoing = dict(message)
        outgoing["id"] = gateway_id
        # 强制把 rootUri / workspaceFolders 对齐到服务端认定的工作区，
        # 避免前端传一个不存在的路径让服务器起不来。
        params = dict(outgoing.get("params") or {})
        root_uri = self.workspace_root.as_uri()
        params["rootUri"] = root_uri
        params["rootPath"] = str(self.workspace_root)
        params["workspaceFolders"] = [{"uri": root_uri, "name": self.workspace_root.name}]
        extra = self.spec.initialization_options(self.workspace_root)
        if extra:
            existing = dict(params.get("initializationOptions") or {})
            for key, value in extra.items():
                if isinstance(value, dict):
                    merged = dict(value)
                    merged.update(existing.get(key) or {})
                    existing[key] = merged
                else:
                    existing.setdefault(key, value)
            params["initializationOptions"] = existing
        outgoing["params"] = params
        await self._write_to_server(outgoing)

    async def _handle_did_open(self, client: ClientHandle, message: Message) -> None:
        params = message.get("params") or {}
        document = params.get("textDocument") or {}
        uri = document.get("uri")
        if not isinstance(uri, str):
            return
        client.opened.add(uri)
        count = self._open_counts.get(uri, 0)
        self._open_counts[uri] = count + 1
        if count > 0:
            # 已经有别的客户端打开了：转成整篇 didChange，保证服务器拿到最新内容
            version = self._bump_version(uri)
            await self._write_to_server(
                rpc.make_notification(
                    "textDocument/didChange",
                    {
                        "textDocument": {"uri": uri, "version": version},
                        "contentChanges": [{"text": document.get("text", "")}],
                    },
                )
            )
            return
        self._doc_versions[uri] = 1
        document["version"] = 1
        params["textDocument"] = document
        message["params"] = params
        await self._forward(client, message)

    async def _handle_did_change(self, client: ClientHandle, message: Message) -> None:
        params = message.get("params") or {}
        document = params.get("textDocument") or {}
        uri = document.get("uri")
        if not isinstance(uri, str):
            return
        version = self._bump_version(uri)
        document["version"] = version
        params["textDocument"] = document
        message["params"] = params
        await self._forward(client, message)

    async def _handle_did_close(self, client: ClientHandle, message: Message) -> None:
        params = message.get("params") or {}
        document = params.get("textDocument") or {}
        uri = document.get("uri")
        if not isinstance(uri, str):
            return
        client.opened.discard(uri)
        await self._release_document(uri)

    async def _release_document(self, uri: str) -> None:
        count = self._open_counts.get(uri, 0)
        if count <= 1:
            self._open_counts.pop(uri, None)
            self._doc_versions.pop(uri, None)
            if count == 1:
                await self._write_to_server(
                    rpc.make_notification("textDocument/didClose", {"textDocument": {"uri": uri}})
                )
            return
        self._open_counts[uri] = count - 1

    async def _forward(self, client: ClientHandle, message: Message) -> None:
        """把客户端消息透传给服务器；请求需要重写 id 并登记。"""

        if message.get("method") is not None and message.get("id") is not None:
            gateway_id = self._next_id()
            self._pending[gateway_id] = _PendingRequest(
                client_id=client.id,
                client_request_id=message.get("id"),
            )
            outgoing = dict(message)
            outgoing["id"] = gateway_id
            await self._write_to_server(outgoing)
            return
        await self._write_to_server(message)

    async def _replay_registrations(self, client: ClientHandle) -> None:
        for params in list(self._registrations):
            await self._send_registration(client, params)

    def _forget_registrations(self, params: Message) -> None:
        """注销掉的能力不再回放给后续客户端。"""

        removed = {
            item.get("id")
            for item in params.get("unregisterations") or params.get("unregistrations") or []
            if isinstance(item, dict) and item.get("id") is not None
        }
        if not removed:
            return
        kept: list[Message] = []
        for entry in self._registrations:
            registrations = [
                registration
                for registration in entry.get("registrations") or []
                if isinstance(registration, dict) and registration.get("id") not in removed
            ]
            if registrations:
                kept.append({**entry, "registrations": registrations})
        self._registrations = kept

    async def _send_registration(self, client: ClientHandle, params: Message) -> None:
        """把动态注册当作「请求」发给客户端。

        vscode-languageclient 只在请求通道上处理 `client/registerCapability`，
        用通知发过去会被直接忽略，诊断（pull）和语义高亮这类依赖动态注册的
        能力就全丢了。
        """

        gateway_id = self._next_id()
        self._pending[gateway_id] = _PendingRequest(client_id=None, client_request_id=None)
        await self._safe_send(
            client,
            {
                "jsonrpc": "2.0",
                "id": gateway_id,
                "method": "client/registerCapability",
                "params": params,
            },
        )

    async def _send_unregistration(self, client: ClientHandle, params: Message) -> None:
        gateway_id = self._next_id()
        self._pending[gateway_id] = _PendingRequest(client_id=None, client_request_id=None)
        await self._safe_send(
            client,
            {
                "jsonrpc": "2.0",
                "id": gateway_id,
                "method": "client/unregisterCapability",
                "params": params,
            },
        )

    # --- 服务器 → 客户端 ------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        stdout = self._process.stdout
        try:
            while True:
                try:
                    message = await rpc.read_message(stdout)
                except rpc.RpcProtocolError as exc:
                    logger.warning("语言服务器 %s 输出异常：%s", self.spec.id, exc)
                    break
                except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
                    break
                if message is None:
                    break
                self._touch()
                await self._dispatch_from_server(message)
        finally:
            await self._on_process_gone()

    async def _dispatch_from_server(self, message: Message) -> None:
        method = message.get("method")
        if method is not None:
            if message.get("id") is not None:
                await self._handle_server_request(message)
            else:
                await self._broadcast(message)
            return
        await self._handle_response(message)

    async def _handle_response(self, message: Message) -> None:
        pending = self._pending.pop(message.get("id"), None)
        if pending is None:
            return
        if pending.client_id is None:
            return
        client = self._clients.get(pending.client_id)
        if client is None:
            return
        if pending.capture_initialize and "result" in message:
            result = message.get("result")
            if isinstance(result, dict):
                self._initialize_result = result
        forwarded = dict(message)
        forwarded["id"] = pending.client_request_id
        await self._safe_send(client, forwarded)

    async def _handle_server_request(self, message: Message) -> None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        if method == "client/registerCapability":
            self._registrations.append(params)
            await self._write_to_server(rpc.make_response(request_id, result=None))
            for client in list(self._clients.values()):
                await self._send_registration(client, params)
            return
        if method == "client/unregisterCapability":
            self._forget_registrations(params)
            await self._write_to_server(rpc.make_response(request_id, result=None))
            for client in list(self._clients.values()):
                await self._send_unregistration(client, params)
            return
        if method == "workspace/configuration":
            items = params.get("items") or []
            await self._write_to_server(rpc.make_response(request_id, result=[None for _ in items]))
            return
        if method == "workspace/workspaceFolders":
            root_uri = self.workspace_root.as_uri()
            await self._write_to_server(
                rpc.make_response(
                    request_id,
                    result=[{"uri": root_uri, "name": self.workspace_root.name}],
                )
            )
            return
        if method == "workspace/applyEdit":
            await self._write_to_server(
                rpc.make_response(
                    request_id,
                    result={"applied": False, "failureReason": "Flux 不在服务端应用编辑"},
                )
            )
            return
        if method == "window/showDocument":
            await self._write_to_server(rpc.make_response(request_id, result={"success": False}))
            return
        if method in (
            "window/workDoneProgress/create",
            "window/showMessageRequest",
            "workspace/codeLens/refresh",
            "workspace/semanticTokens/refresh",
            "workspace/inlayHint/refresh",
            "workspace/diagnostic/refresh",
        ):
            result = None
            await self._write_to_server(rpc.make_response(request_id, result=result))
            return

        # 剩下的一律转给最近接入的客户端；没有客户端就回 null 让服务器别等
        client = self._primary_client()
        if client is None:
            await self._write_to_server(rpc.make_response(request_id, result=None))
            return
        gateway_id = self._next_id()
        self._pending[gateway_id] = _PendingRequest(
            client_id=client.id, client_request_id=request_id
        )
        forwarded = dict(message)
        forwarded["id"] = gateway_id
        await self._safe_send(client, forwarded)

    async def _broadcast(self, message: Message) -> None:
        for client in list(self._clients.values()):
            await self._safe_send(client, message)

    def _primary_client(self) -> ClientHandle | None:
        if not self._clients:
            return None
        return next(reversed(self._clients.values()))

    async def _on_process_gone(self) -> None:
        process = self._process
        code = process.returncode if process is not None else None
        if not self._closing:
            detail = "\n".join(list(self._stderr)[-10:])
            logger.warning("语言服务器 %s 已退出（code=%s）：%s", self.spec.id, code, detail)
        await self._broadcast(
            {
                "jsonrpc": "2.0",
                "method": "flux/serverExited",
                "params": {
                    "server": self.spec.id,
                    "code": code,
                    "stderr": "\n".join(list(self._stderr)[-20:]),
                },
            }
        )
        self._closed = True
        for pending in self._pending.values():
            if pending.client_id is None:
                continue
            client = self._clients.get(pending.client_id)
            if client is None:
                continue
            await self._safe_send(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": pending.client_request_id,
                    "error": {"code": -32000, "message": "语言服务器已退出"},
                },
            )
        self._pending.clear()
        self._open_counts.clear()
        self._doc_versions.clear()
        if self._on_close is not None:
            self._on_close(self)

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr.append(text)
        except (asyncio.CancelledError, ConnectionResetError):
            return

    # --- 底层写入 -------------------------------------------------------------

    async def _write_to_server(self, message: Message) -> None:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            raise LanguageServerFailed(f"语言服务器 {self.spec.id} 未运行")
        data = rpc.encode_message(message)
        async with self._write_lock:
            process.stdin.write(data)
            await process.stdin.drain()

    async def _safe_send(self, client: ClientHandle, message: Message) -> None:
        try:
            await client.send(message)
        except Exception:  # noqa: BLE001 - 单个客户端写失败不能影响其它客户端
            logger.debug("向客户端 %s 推送失败，已移除", client.id, exc_info=True)
            self._clients.pop(client.id, None)

    def _next_id(self) -> int:
        self._next_gateway_id += 1
        return self._next_gateway_id

    def _bump_version(self, uri: str) -> int:
        version = self._doc_versions.get(uri, 0) + 1
        self._doc_versions[uri] = version
        return version

    def _touch(self) -> None:
        self.last_activity = time.monotonic()

    def stderr_tail(self, limit: int = 20) -> list[str]:
        return list(self._stderr)[-limit:]


class CodeIntelligenceService:
    """按 (工作区, 语言服务器) 复用进程；容器级单例。"""

    def __init__(self, *, idle_timeout: float = IDLE_TIMEOUT_SECONDS) -> None:
        self._sessions: dict[tuple[str, str], LspSession] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None
        self._idle_timeout = idle_timeout

    async def open(
        self,
        *,
        server_id: str,
        workspace_root: str | Path | None,
        send: SendMessage,
    ) -> tuple[LspSession, ClientHandle]:
        spec = spec_by_id(server_id)
        if spec is None:
            raise LanguageServerUnavailable(f"未知的语言服务器：{server_id}")
        root = resolve_workspace_root(workspace_root)
        key = (str(root), spec.id)

        async with self._lock:
            session = self._sessions.get(key)
            if session is None or not session.alive:
                session = LspSession(spec=spec, workspace_root=root, on_close=self._forget)
                await session.start()
                self._sessions[key] = session
            client = await session.attach(send)
            self._ensure_reaper()
        return session, client

    async def close_client(self, session: LspSession, client: ClientHandle) -> None:
        await session.detach(client)

    async def shutdown(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
            self._reaper = None
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        await asyncio.gather(*[session.stop() for session in sessions], return_exceptions=True)

    def _forget(self, session: LspSession) -> None:
        current = self._sessions.get(session.key)
        if current is session:
            self._sessions.pop(session.key, None)

    def _ensure_reaper(self) -> None:
        if self._reaper is not None and not self._reaper.done():
            return
        self._reaper = asyncio.create_task(self._reap_idle(), name="lsp-reaper")

    async def _reap_idle(self) -> None:
        while True:
            await asyncio.sleep(REAPER_INTERVAL_SECONDS)
            now = time.monotonic()
            stale: list[LspSession] = []
            async with self._lock:
                for key, session in list(self._sessions.items()):
                    if session.client_count > 0:
                        continue
                    if now - session.last_activity < self._idle_timeout:
                        continue
                    self._sessions.pop(key, None)
                    stale.append(session)
            for session in stale:
                await session.stop()

    async def stop_session(self, *, server_id: str, workspace_root: str | Path | None) -> bool:
        """手动停止某个会话（前端「重启语言服务器」用）。"""

        root = resolve_workspace_root(workspace_root)
        async with self._lock:
            session = self._sessions.pop((str(root), server_id), None)
        if session is None:
            return False
        await session.stop()
        return True

    def active_sessions(self) -> list[dict[str, object]]:
        return [
            {
                "server": session.spec.id,
                "workspace_root": str(session.workspace_root),
                "clients": session.client_count,
                "idle_seconds": round(time.monotonic() - session.last_activity, 1),
            }
            for session in self._sessions.values()
        ]


async def install_server(server_id: str, *, timeout: float = 600.0) -> dict[str, Any]:
    """执行登记在注册表里的安装命令（命令写死在代码里，不接受外部参数）。"""

    spec = spec_by_id(server_id)
    if spec is None:
        raise LanguageServerUnavailable(f"未知的语言服务器：{server_id}")
    if spec.install is None:
        raise LanguageServerUnavailable(f"{spec.name} 不支持自动安装：{spec.install_hint}")

    executable = shutil.which(spec.install[0])
    if executable is None:
        raise LanguageServerUnavailable(
            f"找不到安装命令 `{spec.install[0]}`，请先安装它（{spec.install_hint}）"
        )
    argv = [executable, *spec.install[1:]]
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (OSError, ValueError) as exc:
        raise LanguageServerFailed(f"执行安装命令失败：{exc}") from exc

    try:
        output_bytes, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise LanguageServerFailed(f"安装超时（{int(timeout)}s）") from None

    output = output_bytes.decode("utf-8", errors="replace")[-INSTALL_OUTPUT_LIMIT:]
    return {
        "server": spec.id,
        "command": argv,
        "exit_code": process.returncode,
        "ok": process.returncode == 0,
        "output": output,
        "available": spec.available(),
    }
