"""代码智能网关（LSP）的单元测试。

用一段假语言服务器（本地 python 脚本）验证网关的四项职责：initialize 只做一次、
文档引用计数、server→client 请求兜底应答、shutdown 不杀常驻进程。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from flux.core.code_intelligence import rpc
from flux.core.code_intelligence.registry import (
    EXTENSION_LANGUAGES,
    SERVERS,
    LanguageServerSpec,
    find_server,
    language_for_path,
    spec_by_id,
)
from flux.core.code_intelligence.service import LspSession

FAKE_SERVER = """
import json
import sys

log_path = sys.argv[1]


def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        key, _, value = line.partition(b":")
        headers[key.lower()] = value.strip()
    length = int(headers[b"content-length"])
    return json.loads(sys.stdin.buffer.read(length))


def write_message(payload):
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\\r\\n\\r\\n" % len(body) + body)
    sys.stdout.buffer.flush()


def log(payload):
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\\n")


while True:
    message = read_message()
    if message is None:
        break
    log(message)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        write_message({"jsonrpc": "2.0", "id": request_id,
                       "result": {"capabilities": {"completionProvider": {}}}})
        # 主动向客户端发一个请求：网关必须兜底应答，否则这里会一直等
        write_message({"jsonrpc": "2.0", "id": 100,
                       "method": "workspace/configuration",
                       "params": {"items": [{}]}})
    elif method == "textDocument/completion":
        write_message({"jsonrpc": "2.0", "id": request_id,
                       "result": {"isIncomplete": False,
                                  "items": [{"label": "alpha"}]}})
    elif request_id is not None:
        write_message({"jsonrpc": "2.0", "id": request_id, "result": None})
"""


def test_registry_exposes_all_servers() -> None:
    assert len(SERVERS) >= 30
    assert spec_by_id("pyright") is not None
    assert find_server("python") is not None
    assert language_for_path("a/b/main.py") == "python"
    assert language_for_path("a/b/app.tsx") == "typescriptreact"
    assert language_for_path("a/b/no-such-file.zzz") is None
    assert EXTENSION_LANGUAGES[".py"] == "python"


def test_typescript_prefers_workspace_typescript(tmp_path: Path) -> None:
    lib = tmp_path / "node_modules" / "typescript" / "lib"
    lib.mkdir(parents=True)
    (lib / "tsserver.js").write_text("", encoding="utf-8")
    spec = spec_by_id("typescript")
    assert spec is not None
    assert spec.initialization_options(tmp_path) == {"tsserver": {"path": str(lib / "tsserver.js")}}
    pyright = spec_by_id("pyright")
    assert pyright is not None and pyright.initialization_options(tmp_path) is None


def test_encode_and_read_message_roundtrip() -> None:
    payload = rpc.make_request(1, "initialize", {"a": 1})
    frame = rpc.encode_message(payload)
    assert frame.startswith(b"Content-Length: ")

    async def run() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(frame)
        reader.feed_eof()
        assert await rpc.read_message(reader) == payload
        assert await rpc.read_message(reader) is None

    asyncio.run(run())


class _Chat:
    """一个客户端输出队列，模拟 WebSocket 的 send。"""

    def __init__(self) -> None:
        self.queued: asyncio.Queue[dict] = asyncio.Queue()

    async def send(self, message: dict) -> None:
        await self.queued.put(message)

    async def expect(self, predicate, timeout: float = 10.0) -> dict:
        async def loop() -> dict:
            while True:
                message = await self.queued.get()
                if predicate(message):
                    return message

        return await asyncio.wait_for(loop(), timeout=timeout)


@pytest.fixture()
def fake_spec(tmp_path: Path) -> LanguageServerSpec:
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    return LanguageServerSpec(
        id="fake",
        name="Fake",
        languages=("python",),
        command=(sys.executable, str(script), str(tmp_path / "lsp.log")),
        install=None,
        install_hint="",
        description="test double",
        homepage="",
    )


def test_gateway_shares_one_server_between_clients(
    tmp_path: Path, fake_spec: LanguageServerSpec
) -> None:
    async def run() -> None:
        session = LspSession(spec=fake_spec, workspace_root=tmp_path)
        await session.start()
        try:
            first, second = _Chat(), _Chat()
            client_a = await session.attach(first.send)
            client_b = await session.attach(second.send)

            # 1. 两个客户端各自 initialize：服务器只收到一次，第二个直接回放
            await session.handle_client_message(
                client_a, rpc.make_request(1, "initialize", {"rootUri": None})
            )
            result_a = await first.expect(lambda m: m.get("id") == 1)
            assert result_a["result"]["capabilities"]["completionProvider"] == {}

            await session.handle_client_message(
                client_b, rpc.make_request(7, "initialize", {"rootUri": None})
            )
            result_b = await second.expect(lambda m: m.get("id") == 7)
            assert result_b["result"]["capabilities"] == result_a["result"]["capabilities"]

            # 2. 服务器发来的 workspace/configuration 由网关兜底应答
            log_path = tmp_path / "lsp.log"
            for _ in range(200):
                entries = [
                    json.loads(line)
                    for line in log_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                if any(e.get("id") == 100 and "result" in e for e in entries):
                    break
                await asyncio.sleep(0.02)
            answers = [e for e in entries if e.get("id") == 100]
            assert answers and answers[-1]["result"] == [None]

            # 3. 文档引用计数：同一 URI 两个客户端只 didOpen 一次，
            #    第二个客户端的 didOpen 被改写成 didChange
            uri = (tmp_path / "main.py").as_uri()
            open_params = {
                "textDocument": {
                    "uri": uri,
                    "languageId": "python",
                    "version": 1,
                    "text": "x = 1\n",
                }
            }
            await session.handle_client_message(
                client_a, rpc.make_notification("textDocument/didOpen", open_params)
            )
            await session.handle_client_message(
                client_b, rpc.make_notification("textDocument/didOpen", open_params)
            )
            await session.handle_client_message(
                client_a,
                rpc.make_notification("textDocument/didClose", {"textDocument": {"uri": uri}}),
            )
            await asyncio.sleep(0.2)
            entries = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            assert len([e for e in entries if e.get("method") == "initialize"]) == 1
            assert len([e for e in entries if e.get("method") == "textDocument/didOpen"]) == 1
            assert len([e for e in entries if e.get("method") == "textDocument/didChange"]) == 1
            # 还有一个客户端开着文档，didClose 不能下发
            assert not [e for e in entries if e.get("method") == "textDocument/didClose"]

            # 4. 正常请求走完整链路
            await session.handle_client_message(
                client_b,
                rpc.make_request(
                    9,
                    "textDocument/completion",
                    {"textDocument": {"uri": uri}, "position": {"line": 0, "character": 1}},
                ),
            )
            completion = await second.expect(lambda m: m.get("id") == 9)
            assert completion["result"]["items"][0]["label"] == "alpha"

            # 5. shutdown 被网关拦下：客户端拿到成功响应，进程还活着
            await session.handle_client_message(client_a, rpc.make_request(11, "shutdown"))
            shutdown = await first.expect(lambda m: m.get("id") == 11)
            assert shutdown["result"] is None
            assert session.alive

            # 6. 最后一个客户端关闭文档后才补 didClose
            await session.handle_client_message(
                client_b,
                rpc.make_notification("textDocument/didClose", {"textDocument": {"uri": uri}}),
            )
            await asyncio.sleep(0.2)
            entries = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            assert len([e for e in entries if e.get("method") == "textDocument/didClose"]) == 1
        finally:
            await session.stop()

    asyncio.run(run())
