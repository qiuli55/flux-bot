"""Model Gateway API（主规格 §12.6 统一 LLM 接口）。

请求字段：model / messages / task_id / project_id
响应字段：content / token usage / cost

另含 IDE 行内补全的 FIM 流（AI_COMPLETION_SPEC §5.4）：
`POST /models/fim` 以 SSE 推送 `delta` / `done` / `error` 事件。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.core.model_gateway.base import ChatMessage
from flux.schemas.api import ChatRequest, FimRequest

router = APIRouter(prefix="/models", tags=["models"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # 反向代理（nginx 等）不能缓冲事件流，否则增量会一次性到达
    "X-Accel-Buffering": "no",
}


def _format_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/chat")
async def chat(
    payload: ChatRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    messages = [ChatMessage(role=m.role, content=m.content) for m in payload.messages]
    result = await container.router.chat(messages, provider=payload.provider)
    return ok(
        {
            "content": result.content,
            "provider": str(result.provider),
            "model": result.model,
            "usage": {
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "total_tokens": result.usage.total_tokens,
            },
            # cost 为 null：该供应商未配置计价，不臆造金额（主规格 §15.4）
            "cost": result.cost,
            "latency_ms": result.latency_ms,
        },
        metadata={"task_id": payload.task_id, "project_id": payload.project_id},
    )


@router.post("/fim")
async def fim(
    payload: FimRequest, container: Container = Depends(get_container)
) -> StreamingResponse:
    """行内补全（SSE）：先解析模型通道，再流式吐 `delta`，结束给 `done`。

    通道解析失败（没配密钥、模型不存在、Agent 已被删）在建立流之前抛领域异常，
    走统一 JSON 错误响应；一旦开始推流，错误只能作为 `error` 事件下发。
    """

    target = await container.completion.resolve(
        model=payload.model, key_id=payload.key_id, agent_id=payload.agent_id
    )
    stats = container.completion_stats

    async def generate() -> AsyncIterator[str]:
        stats.record_request(model=target.model)
        done: dict[str, Any] | None = None
        failure: dict[str, Any] | None = None
        try:
            async for event, data in container.completion.stream(payload, target):
                if event == "done":
                    done = data
                elif event == "error":
                    failure = data
                yield _format_sse(event, data)
        except (asyncio.CancelledError, GeneratorExit):
            # 编辑器继续敲字 / 换位置时旧流会被主动关闭：这不算失败，单独计数
            stats.record_abort(mode=str((done or {}).get("mode") or ""), model=target.model)
            raise
        if failure is not None:
            stats.record_failure(str(failure.get("code") or ""), model=target.model)
        elif done is not None:
            stats.record_result(
                mode=str(done.get("mode") or ""),
                model=str(done.get("model") or target.model),
                first_token_ms=_as_int(done.get("first_token_ms")),
                total_ms=_as_int(done.get("total_ms")),
                chars=_as_int(done.get("chars")) or 0,
                empty=bool(done.get("empty")),
            )
        else:
            stats.record_abort(model=target.model)

    return StreamingResponse(generate(), media_type="text/event-stream", headers=SSE_HEADERS)


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
