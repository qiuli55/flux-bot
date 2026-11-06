"""LSP 的帧编解码：`Content-Length` 头 + JSON 体。

语言服务器的 stdio 用的是 LSP 基础协议（`\r\n` 分隔的头 + 定长 body），
WebSocket 则是一帧一个完整 JSON。两端之间的转换点只有这一个文件：

- 读子进程 stdout：`read_message()`；
- 写给子进程 stdin：`encode_message()`；
- 网关自己构造的请求/通知/响应：`make_request()` / `make_notification()` / `make_response()`。
"""

from __future__ import annotations

import json
from typing import Any

#: 头部上限，防止畸形输出把内存吃穿
MAX_HEADER_BYTES = 16 * 1024
#: 单条消息上限（语义高亮、大文件诊断可能很大）
MAX_MESSAGE_BYTES = 48 * 1024 * 1024

JsonObject = dict[str, Any]


class RpcProtocolError(RuntimeError):
    """帧格式错误 / 超限 / 不是合法 JSON。"""


async def read_message(reader: Any) -> JsonObject | None:
    """从 `asyncio.StreamReader` 读一条消息；EOF 返回 None。"""

    headers: dict[bytes, bytes] = {}
    consumed = 0
    while True:
        line = await reader.readline()
        if not line:
            if not headers:
                return None
            raise RpcProtocolError("头部未读完就遇到 EOF")
        consumed += len(line)
        if consumed > MAX_HEADER_BYTES:
            raise RpcProtocolError("头部超过上限")
        if line in (b"\r\n", b"\n"):
            break
        key, separator, value = line.partition(b":")
        if not separator:
            continue
        headers[key.strip().lower()] = value.strip()

    raw_length = headers.get(b"content-length")
    if raw_length is None:
        raise RpcProtocolError("缺少 Content-Length")
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise RpcProtocolError(f"Content-Length 非法：{raw_length!r}") from exc
    if length < 0 or length > MAX_MESSAGE_BYTES:
        raise RpcProtocolError(f"消息长度越界：{length}")

    body = await reader.readexactly(length)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RpcProtocolError("body 不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise RpcProtocolError("LSP 消息必须是 JSON 对象")
    return payload


def encode_message(payload: JsonObject) -> bytes:
    """把一条 JSON-RPC 消息编码成 stdio 帧。"""

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body


def make_request(request_id: Any, method: str, params: Any = None) -> JsonObject:
    message: JsonObject = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def make_notification(method: str, params: Any = None) -> JsonObject:
    message: JsonObject = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return message


def make_response(
    request_id: Any,
    *,
    result: Any = None,
    error: JsonObject | None = None,
) -> JsonObject:
    message: JsonObject = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result
    return message


def make_error(code: int, message: str, data: Any = None) -> JsonObject:
    error: JsonObject = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return error
