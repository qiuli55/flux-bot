"""`context.fetch` 测试（FLUX_CONTEXT_MECHANISM §6.7 / §7.1 / §13-3）。

第三档降级（只给 step_id + 目录行）必须有一个**消费方**，否则那一档是断的。
本文件钉住 `context.fetch(step_id, range, budget)` 的三条底线：

1. **逐字可取**：按 step_id 取回的原文与写进内容寻址库的**完全一致**，且带上 sha256；
2. **可按需分页**：range + budget 同时生效，`next_offset` 能把长输出切成可拼接的页；
3. **失败显式**：缺 step、无结果、参数非法、权限不足一律 isError，不静默降级。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from flux.config import Settings
from flux.core.context import ContentStore, ContextStepRepository
from flux.db.session import create_engine, create_session_factory
from flux.enums import Capability, StepKind, StepStatus

from .conftest import issue_token

TEXT = "line-%03d: the quick brown fox jumps over the lazy dog\n" * 12


def rpc(client: TestClient, token: str, method: str, params: dict | None = None):
    body: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}"})


def call(client: TestClient, token: str, name: str, arguments: dict) -> dict:
    response = rpc(client, token, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def payload(result: dict) -> dict:
    assert result["isError"] is False, result["content"][0]["text"]
    return json.loads(result["content"][0]["text"])


def error_text(result: dict) -> str:
    assert result["isError"] is True, result
    return result["content"][0]["text"]


def _seed_step(
    settings: Settings,
    *,
    text: str | None,
    turn_id: str = "turn-1",
    run_id: str = "run-1",
    target_ref: str | None = "todo_service/store.py",
) -> tuple[int, str | None]:
    """用独立引擎写一条带原文的 step，返回 (step_id, sha256)。

    TestClient 的事件循环跑在它自己的 portal 线程里，容器引擎与它绑定；这里另开一个
    引擎、用 asyncio.run 播种，避开"跨事件循环用同一个 aiosqlite 连接"的坑。
    """

    async def _run() -> tuple[int, str | None]:
        engine = create_engine(settings.database_url)
        try:
            factory = create_session_factory(engine)
            sha: str | None = None
            size: int | None = None
            if text is not None:
                blob = await ContentStore(factory).put(text.encode())
                sha, size = blob.sha256, blob.size_raw
            step = await ContextStepRepository(factory).append(
                run_id=run_id,
                turn_id=turn_id,
                kind=StepKind.COMMAND,
                status=StepStatus.OK,
                tool="terminal.execute",
                target_ref=target_ref,
                exit_code=0,
                result_sha256=sha,
                result_bytes=size,
            )
            return step.id, sha
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def test_fetch_returns_full_text_verbatim(mcp_client: TestClient, settings: Settings) -> None:
    step_id, sha = _seed_step(settings, text=TEXT)
    token = issue_token(mcp_client)

    data = payload(call(mcp_client, token, "context.fetch", {"step_id": step_id}))

    assert data["content"] == TEXT
    assert data["sha256"] == sha
    assert data["size_bytes"] == len(TEXT.encode())
    assert data["total_chars"] == len(TEXT)
    assert data["offset"] == 0 and data["returned_chars"] == len(TEXT)
    assert data["truncated"] is False and data["next_offset"] is None
    # 目录行露出 #编号，agent 靠它与 fetch 对上号（§7.1）
    assert f"#{step_id:04d}" in data["catalog_line"]
    assert data["kind"] == StepKind.COMMAND.value and data["status"] == StepStatus.OK.value


def test_fetch_range_returns_the_slice(mcp_client: TestClient, settings: Settings) -> None:
    step_id, _ = _seed_step(settings, text=TEXT)
    token = issue_token(mcp_client)

    data = payload(
        call(mcp_client, token, "context.fetch", {"step_id": step_id, "range": [10, 40]})
    )

    assert data["content"] == TEXT[10:40]
    assert data["offset"] == 10 and data["returned_chars"] == 30
    assert data["next_offset"] == 40


def test_fetch_budget_pages_through_the_whole_text(
    mcp_client: TestClient, settings: Settings
) -> None:
    """budget 生效且给出 next_offset：按页取回再拼起来必须与原文逐字相同。"""
    step_id, _ = _seed_step(settings, text=TEXT)
    token = issue_token(mcp_client)

    collected: list[str] = []
    offset = 0
    while offset is not None:
        args: dict = {"step_id": step_id, "budget": 100}
        if offset:
            args["range"] = [offset, len(TEXT)]
        data = payload(call(mcp_client, token, "context.fetch", args))
        assert data["returned_chars"] <= 100
        collected.append(data["content"])
        offset = data["next_offset"]

    assert "".join(collected) == TEXT


def test_fetch_rejects_unknown_step(mcp_client: TestClient) -> None:
    token = issue_token(mcp_client)
    text = error_text(call(mcp_client, token, "context.fetch", {"step_id": 999999}))
    assert "不存在" in text


def test_fetch_rejects_step_without_result(mcp_client: TestClient, settings: Settings) -> None:
    step_id, _ = _seed_step(settings, text=None)
    token = issue_token(mcp_client)
    text = error_text(call(mcp_client, token, "context.fetch", {"step_id": step_id}))
    assert "没有可取的原文" in text


@pytest.mark.parametrize(
    "arguments",
    [
        {"step_id": "42"},
        {"step_id": 1, "range": [30, 10]},
        {"step_id": 1, "range": [1]},
        {"step_id": 1, "budget": 0},
        {"step_id": 1, "unexpected": 1},
    ],
)
def test_fetch_rejects_bad_arguments(mcp_client: TestClient, arguments: dict) -> None:
    token = issue_token(mcp_client)
    assert call(mcp_client, token, "context.fetch", arguments)["isError"] is True


def test_fetch_requires_file_read_capability(mcp_client: TestClient, settings: Settings) -> None:
    step_id, _ = _seed_step(settings, text=TEXT)
    # 空的 scopes 会被签发助手补成默认值，故换成"有令牌但没给 file.read"来验越权
    token = issue_token(mcp_client, agent_id="no-read", scopes=[Capability.BROWSER_OPEN])
    text = error_text(call(mcp_client, token, "context.fetch", {"step_id": step_id}))
    assert "权限不足" in text


def test_fetch_is_advertised_with_file_read_capability(mcp_client: TestClient) -> None:
    token = issue_token(mcp_client)
    listed = rpc(mcp_client, token, "tools/list").json()["result"]["tools"]
    tools = {tool["name"]: tool for tool in listed}
    assert "context.fetch" in tools


def test_capability_enum_has_file_read() -> None:
    """防呆：能力名一旦被改写，上面的越权用例会假绿。"""
    assert Capability.FILE_READ.value == "file.read"
