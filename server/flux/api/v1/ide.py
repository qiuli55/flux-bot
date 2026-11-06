"""IDE 代码智能 API：语言服务器清单、安装、进程状态与 LSP WebSocket 网关。

前端（Web 的 Monaco、桌面的 AvaloniaEdit）都通过同一套接口拿能力：

- `GET /ide/languages` —— 有哪些语言、该语言用哪个服务器、是否已安装；
- `POST /ide/languages/{id}/install` —— 按注册表里写死的命令安装（仅本地环境）；
- `GET /ide/sessions` —— 当前常驻的语言服务器进程（排查「为什么没补全」）；
- `POST /ide/sessions/stop` —— 手动停掉某个进程，下次连接会重新拉起；
- `WS /ide/lsp/{project_id}?server=&workspace_root=` —— JSON-RPC 双向通道。

WebSocket 上跑的是原样的 LSP JSON-RPC（一帧一条消息，不要 Content-Length 头），
网关额外会下发两个自定义通知：

- `flux/serverExited`：语言服务器进程退出，附带 stderr 尾部；
- `flux/error`：连接建立失败（未安装、启动失败等）。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect

from flux.api.deps import get_container
from flux.api.response import ok
from flux.api.ws_auth import authenticated_websocket, same_origin_websocket
from flux.container import Container
from flux.core.code_intelligence.registry import (
    EXTENSION_LANGUAGES,
    FILENAME_LANGUAGES,
    LANGUAGE_LABELS,
    SERVERS,
    describe_servers,
    servers_for_language,
)
from flux.core.code_intelligence.service import (
    LanguageServerFailed,
    LanguageServerUnavailable,
    install_server,
)
from flux.errors import NotFoundError
from flux.schemas.api import (
    CompletionAcceptedRequest,
    CompletionContextRequest,
    CompletionPrefsRequest,
)

router = APIRouter(prefix="/ide", tags=["ide"])
websocket_router = APIRouter(prefix="/ide", tags=["ide"])

#: 单个连接待发消息上限；超过说明前端消费不过来，直接断开比无限吃内存好
OUTBOUND_QUEUE_LIMIT = 20_000


def _language_catalog() -> list[dict[str, object]]:
    """每种语言 → 推荐服务器，只探测一次可用性。"""

    availability = {spec.id: spec.available() for spec in SERVERS}
    catalog: list[dict[str, object]] = []
    for language_id, label in LANGUAGE_LABELS.items():
        candidates = servers_for_language(language_id)
        chosen = next(
            (spec for spec in candidates if availability[spec.id]),
            candidates[0] if candidates else None,
        )
        catalog.append(
            {
                "id": language_id,
                "label": label,
                "server": chosen.id if chosen is not None else None,
                "server_name": chosen.name if chosen is not None else None,
                "available": bool(chosen is not None and availability[chosen.id]),
                "servers": [spec.id for spec in candidates],
            }
        )
    return catalog


@router.get("/languages")
async def list_languages(container: Container = Depends(get_container)) -> dict[str, object]:
    """语言服务器清单 + 语言目录（含是否已安装）。"""

    servers = await asyncio.to_thread(describe_servers)
    languages = await asyncio.to_thread(_language_catalog)
    return ok(
        {
            "servers": servers,
            "languages": languages,
            # 前端按路径推断语言：先精确文件名（Dockerfile/CMakeLists.txt），再扩展名。
            "extensions": EXTENSION_LANGUAGES,
            "filenames": FILENAME_LANGUAGES,
        },
        metadata={
            "servers": len(servers),
            "languages": len(languages),
            "installed": sum(1 for item in servers if item["available"]),
        },
    )


@router.post("/languages/{server_id}/install")
async def install_language_server(
    server_id: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    """按注册表里写死的命令安装语言服务器（只在本地环境可用）。"""

    if container.settings.env != "local":
        raise HTTPException(status_code=403, detail="安装语言服务器只允许在本地环境使用")
    try:
        result = await install_server(server_id)
    except LanguageServerUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LanguageServerFailed as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ok(result, metadata={"ok": result["ok"], "available": result["available"]})


@router.get("/sessions")
async def list_sessions(container: Container = Depends(get_container)) -> dict[str, object]:
    """当前常驻的语言服务器进程。"""

    sessions = container.code_intel.active_sessions()
    return ok(sessions, metadata={"count": len(sessions)})


@router.post("/sessions/stop")
async def stop_session(
    server: str = Query(min_length=1, description="语言服务器 id"),
    workspace_root: str | None = Query(default=None),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """停掉一个常驻语言服务器；下次打开文件会自动重新拉起。"""

    stopped = await container.code_intel.stop_session(
        server_id=server, workspace_root=workspace_root
    )
    return ok({"server": server, "stopped": stopped})


# --- 行内补全（AI_COMPLETION_SPEC §3.2 / §5.3）---


@router.post("/completion/context")
async def completion_context(
    payload: CompletionContextRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    """聚合 L0 / L1 / L2 三层补全上下文（只读，不写盘）。"""

    data = await container.completion.build_context(payload)
    return ok(
        data,
        metadata={
            "project_id": payload.project_id,
            "cache_key": data.get("cache_key"),
            "l1_files": len(data.get("l1") or []),
        },
    )


@router.get("/completion/prefs")
async def get_completion_prefs(
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """补全偏好 + 当前 Agent 选择 + 实际解析到的模型通道。"""

    return ok(await container.completion_resolver.describe())


@router.put("/completion/prefs")
async def put_completion_prefs(
    payload: CompletionPrefsRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    """保存补全偏好；响应与 GET 一致，方便前端一次拿到解析结果。"""

    container.completion_prefs.update_prefs(
        enabled=payload.enabled,
        model=payload.model,
        debounce_ms=payload.debounce_ms,
        max_lines=payload.max_lines,
    )
    return ok(await container.completion_resolver.describe(), metadata={"saved": True})


@router.post("/completion/accepted")
async def record_completion_accepted(
    payload: CompletionAcceptedRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    """上报一次补全接受（AI_COMPLETION_SPEC §8）。

    插件在 `InlineCompletionItem.command` 里回调；服务端只做计数，不落任何代码内容。
    """

    container.completion_stats.record_accept(
        accepted=payload.accepted,
        mode=payload.mode or "",
        model=payload.model or "",
        language_id=payload.language_id or "",
        completion_mode=payload.completion_mode or "",
    )
    return ok({"accepted": bool(payload.accepted)})


@router.get("/completion/stats")
async def get_completion_stats(
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """补全埋点汇总：接受率、首字延迟、空结果率、按模型 / 模式拆分。"""

    snapshot = container.completion_stats.snapshot()
    totals = snapshot["totals"]
    return ok(
        snapshot,
        metadata={
            "requests": totals["requests"],
            "acceptance_rate": totals["acceptance_rate"],
            "avg_first_token_ms": totals["avg_first_token_ms"],
        },
    )


async def _pump_outbound(websocket: WebSocket, queue: asyncio.Queue[dict[str, Any]]) -> None:
    while True:
        message = await queue.get()
        await websocket.send_json(message)


async def _pump_inbound(websocket: WebSocket, session: Any, client: Any) -> None:
    while True:
        raw = await websocket.receive_text()
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        await session.handle_client_message(client, message)


@websocket_router.websocket("/lsp/{project_id}")
async def lsp_websocket(
    websocket: WebSocket,
    project_id: uuid.UUID,
    server: str = Query(min_length=1, description="语言服务器 id"),
    workspace_root: str | None = Query(default=None),
) -> None:
    """把一条 WebSocket 接到常驻语言服务器上。"""

    if not same_origin_websocket(websocket):
        await websocket.close(code=1008, reason="cross-origin websocket rejected")
        return
    if not authenticated_websocket(websocket):
        await websocket.close(code=1008, reason="authentication required")
        return

    container: Container = websocket.app.state.container
    try:
        await container.brain.get_project(project_id)
    except NotFoundError:
        await websocket.close(code=1008, reason="project not found")
        return

    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def send(message: dict[str, Any]) -> None:
        if queue.qsize() >= OUTBOUND_QUEUE_LIMIT:
            raise RuntimeError("outbound queue overflow")
        queue.put_nowait(message)

    try:
        session, client = await container.code_intel.open(
            server_id=server,
            workspace_root=workspace_root or container.settings.workspace_root,
            send=send,
        )
    except (LanguageServerUnavailable, LanguageServerFailed) as exc:
        await websocket.accept()
        await websocket.send_json(
            {
                "jsonrpc": "2.0",
                "method": "flux/error",
                "params": {"message": str(exc), "server": server},
            }
        )
        await websocket.close(code=1011, reason="language server unavailable")
        return

    await websocket.accept()
    outbound_task = asyncio.create_task(
        _pump_outbound(websocket, queue), name=f"flux-lsp-out-{session.spec.id}"
    )
    inbound_task = asyncio.create_task(
        _pump_inbound(websocket, session, client), name=f"flux-lsp-in-{session.spec.id}"
    )
    try:
        done, _ = await asyncio.wait(
            {outbound_task, inbound_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            with suppress(Exception):
                task.result()
    except WebSocketDisconnect:
        pass
    finally:
        for task in (outbound_task, inbound_task):
            task.cancel()
        await asyncio.gather(outbound_task, inbound_task, return_exceptions=True)
        await container.code_intel.close_client(session, client)
        with suppress(Exception):
            await websocket.close()
