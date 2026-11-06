"""跨轮会话复用：绑定仓储、opencode 会话恢复、codex thread 恢复。"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import httpx

from flux.core.agent_runtime.runtimes.codex_server import (
    CodexAppServerSession,
    CodexTurnResult,
)
from flux.core.agent_runtime.runtimes.opencode_server import OpenCodeServerClient


class _FakeResponse:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://127.0.0.1/session")
            raise httpx.HTTPStatusError(
                "fake error", request=request, response=httpx.Response(self.status_code)
            )

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, *, get: _FakeResponse, post: _FakeResponse | None = None) -> None:
        self._get = get
        self._post = post or _FakeResponse(200, {"id": "fresh-session"})
        self.posted: list[str] = []
        self.bodies: list[dict] = []

    async def get(self, path: str, params: dict | None = None, timeout: float = 0) -> _FakeResponse:
        return self._get

    async def post(self, path: str, json: dict | None = None, timeout: float = 0):
        self.posted.append(path)
        self.bodies.append(json or {})
        return self._post


def _opencode_client() -> OpenCodeServerClient:
    async def noop(*args, **kwargs) -> None:
        return None

    return OpenCodeServerClient(
        "http://127.0.0.1:1",
        on_text=noop,
        on_thinking=lambda text, phase: noop(),
        on_tool=noop,
        on_step=noop,
        on_error=noop,
    )


def test_runtime_session_repository_upserts_and_clears(container):
    repo = container.runtime_sessions
    task_id = uuid.uuid4()

    assert asyncio.run(repo.get(task_id, "opencode")) == ""
    asyncio.run(repo.set(task_id, "opencode", "session-1"))
    assert asyncio.run(repo.get(task_id, "opencode")) == "session-1"
    asyncio.run(repo.set(task_id, "opencode", "session-2"))
    assert asyncio.run(repo.get(task_id, "opencode")) == "session-2"
    # 不同 runtime 互不覆盖
    asyncio.run(repo.set(task_id, "codex", "thread-1"))
    assert asyncio.run(repo.get(task_id, "codex")) == "thread-1"

    asyncio.run(repo.clear(task_id, "opencode"))
    assert asyncio.run(repo.get(task_id, "opencode")) == ""


def test_runtime_session_repository_tracks_context_tokens(container):
    """上下文规模要跟会话绑定一起持久化：下一轮据此判断要不要压缩。"""
    repo = container.runtime_sessions
    task_id = uuid.uuid4()

    # 没有绑定时按「未知」返回 0
    assert asyncio.run(repo.get_context_tokens(task_id, "codex")) == 0
    asyncio.run(repo.set(task_id, "codex", "thread-1", context_tokens=91000))
    assert asyncio.run(repo.get_context_tokens(task_id, "codex")) == 91000
    # 不报送（None）时保留旧值，供不实现上下文治理的 runtime 沿用原写入口径
    asyncio.run(repo.set(task_id, "codex", "thread-1"))
    assert asyncio.run(repo.get_context_tokens(task_id, "codex")) == 91000
    # 每轮更新为最新规模
    asyncio.run(repo.set(task_id, "codex", "thread-1", context_tokens=12000))
    assert asyncio.run(repo.get_context_tokens(task_id, "codex")) == 12000
    # 会话失效重开后规模归零
    asyncio.run(repo.set(task_id, "codex", "thread-2", context_tokens=0))
    assert asyncio.run(repo.get_context_tokens(task_id, "codex")) == 0


def test_opencode_reuses_existing_session():
    client = _opencode_client()
    client._client = _FakeClient(get=_FakeResponse(200, {"id": "old-session"}))
    assert asyncio.run(client._resolve_session("old-session", "t")) == "old-session"
    # 复用路径不再 POST /session
    assert client._client.posted == []


def test_opencode_falls_back_when_session_missing():
    client = _opencode_client()
    client._client = _FakeClient(get=_FakeResponse(404))
    assert asyncio.run(client._resolve_session("gone", "t")) == "fresh-session"
    assert client._client.posted == ["/session"]


def test_opencode_message_carries_task_model():
    """任务切了模型后，每条消息都要显式下发模型（会话会记住旧模型）。"""
    client = _opencode_client()
    fake = _FakeClient(
        get=_FakeResponse(200, {"id": "old-session"}),
        post=_FakeResponse(200, {"parts": []}),
    )
    client._client = fake
    asyncio.run(client._prompt_async("old-session", "hi", "opencode-go/gemini-4-argon"))
    assert fake.bodies[-1]["model"] == {
        "providerID": "opencode-go",
        "modelID": "gemini-4-argon",
    }


def test_opencode_new_session_carries_task_model():
    """新建会话也要带模型：否则会话默认落回全局配置的默认模型。"""
    client = _opencode_client()
    fake = _FakeClient(get=_FakeResponse(404))
    client._client = fake
    asyncio.run(client._resolve_session("gone", "t", "opencode-go/gemini-4-argon"))
    assert fake.bodies[-1]["model"] == {
        "id": "gemini-4-argon",
        "providerID": "opencode-go",
    }


def test_opencode_without_model_keeps_server_default():
    """没选模型时不要伪造 model 字段，保持 opencode 自己的默认行为。"""
    client = _opencode_client()
    fake = _FakeClient(get=_FakeResponse(404), post=_FakeResponse(200, {"parts": []}))
    client._client = fake
    asyncio.run(client._prompt_async("s1", "hi"))
    assert "model" not in fake.bodies[-1]


def _codex_session(request_handler) -> CodexAppServerSession:
    session = CodexAppServerSession(
        SimpleNamespace(popen=SimpleNamespace(stdout=None, poll=lambda: None)),
    )
    session.bind(
        cwd="/tmp",
        on_text=lambda text: None,
        on_thinking=lambda text: None,
        on_tool=lambda fields, state: None,
    )
    session._read_stdout = lambda loop: None
    session._request = request_handler

    async def consume() -> CodexTurnResult:
        return CodexTurnResult(text="ok")

    session._consume_turn = consume
    return session


def test_codex_resumes_thread_when_available():
    calls: list[str] = []

    async def request(method: str, params: dict, *, timeout: float = 0):
        calls.append(method)
        if method == "thread/resume":
            assert params["threadId"] == "thread-old"
            return {"thread": {"id": "thread-old"}}
        return {}

    result = asyncio.run(_codex_session(request).run("继续", thread_id="thread-old"))
    assert calls == ["initialize", "thread/resume", "turn/start"]
    assert result.thread_id == "thread-old"


def test_codex_run_overrides_thread_model():
    """续聊必须带本轮模型：thread 会记住创建时的模型，不覆盖就一直是旧模型。"""
    seen: dict[str, dict] = {}

    async def request(method: str, params: dict, *, timeout: float = 0):
        if method == "thread/resume":
            seen["resume"] = params
            return {"thread": {"id": "thread-old"}}
        if method == "turn/start":
            seen["turn"] = params
        return {}

    asyncio.run(
        _codex_session(request).run(
            "继续",
            thread_id="thread-old",
            model="gpt-5.6-luna",
            model_provider="openai",
        )
    )
    assert seen["resume"]["model"] == "gpt-5.6-luna"
    assert seen["resume"]["modelProvider"] == "openai"
    assert seen["turn"]["model"] == "gpt-5.6-luna"


def test_codex_starts_new_thread_when_resume_fails():
    calls: list[str] = []

    async def request(method: str, params: dict, *, timeout: float = 0):
        calls.append(method)
        if method == "thread/resume":
            raise RuntimeError("thread not found")
        if method == "thread/start":
            return {"thread": {"id": "thread-new"}}
        return {}

    result = asyncio.run(_codex_session(request).run("继续", thread_id="thread-old"))
    assert calls == ["initialize", "thread/resume", "thread/start", "turn/start"]
    assert result.thread_id == "thread-new"


def test_codex_second_turn_on_same_process_reuses_handshake_and_thread():
    """常驻进程复用：第二轮不再 initialize，也不重复 resume 已打开的 thread。"""
    calls: list[str] = []

    async def request(method: str, params: dict, *, timeout: float = 0):
        calls.append(method)
        if method in ("thread/start", "thread/resume"):
            return {"thread": {"id": "thread-1"}}
        return {}

    session = _codex_session(request)

    async def scenario() -> None:
        first = await session.run("第一轮")
        second = await session.run("第二轮", thread_id=first.thread_id)
        assert session.usable
        return second

    second = asyncio.run(scenario())
    assert second.thread_id == "thread-1"
    assert calls == ["initialize", "thread/start", "turn/start", "turn/start"]


def test_codex_session_must_be_bound_before_run():
    """换绑是每轮前置条件：没 bind 就 run 必须立刻报错，不能拿上一轮的回调乱发。"""
    session = CodexAppServerSession(
        SimpleNamespace(popen=SimpleNamespace(stdout=None, poll=lambda: None))
    )
    session._read_stdout = lambda loop: None
    try:
        asyncio.run(session.run("你好"))
    except RuntimeError as exc:
        assert "未绑定" in str(exc)
    else:  # pragma: no cover - 未抛异常说明守卫失效
        raise AssertionError("未绑定时不应允许执行")
