"""终端会话测试（Agent Terminal Console §8 / §14 的 T1 部分）。

T1 覆盖：会话创建（工作区根与 Apply 同源）、用户命令执行与输出落库、exit code、
按 seq 续读历史、Stop 后拒绝新命令、Force Stop 真的终止长命令，以及 REST 闭环。

除 REST 用例外都是原生 async 测试（pytest asyncio_mode=auto）：整个用例跑在同一个
事件循环里，避免同一引擎被多个 asyncio.run 的循环反复使用。
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
import uuid

import pytest

from flux.container import Container
from flux.core.agent_runtime import platforms
from flux.core.mcp.auth import AgentIdentity
from flux.core.mcp.tools.base import ToolContext
from flux.core.mcp.tools.terminal_tools import terminal_execute
from flux.enums import (
    Capability,
    TerminalEventKind,
    TerminalSessionKind,
    TerminalSessionStatus,
    TerminalSource,
)
from flux.errors import ConflictError, NotFoundError, ValidationError


def _py(code: str) -> str:
    """一条跨平台可执行的 `python -c` 命令。

    终端执行的是 shell 命令，Windows 上是 cmd.exe：`sleep` / `echo` 这类 POSIX 命令不存在，
    且 cmd 不认单引号。用例统一改用 python 调用，命令语义在两个平台一致。
    """
    exe = f'"{sys.executable}"' if os.name == "nt" else shlex.quote(sys.executable)
    body = f'"{code}"' if os.name == "nt" else shlex.quote(code)
    return f"{exe} -c {body}"


async def test_create_session_uses_configured_workspace_root(
    apply_container: Container, workspace_root
) -> None:
    session = await apply_container.terminal.create_session()
    assert session.status == TerminalSessionStatus.ACTIVE.value
    assert session.kind == TerminalSessionKind.AGENT.value
    assert session.workspace_root == str(workspace_root.resolve())


async def test_create_session_without_workspace_root_is_rejected(container: Container) -> None:
    """未配置工作区根时拒绝开会话：与 Apply 同一口径，不猜默认目录。"""
    with pytest.raises(ValidationError):
        await container.terminal.create_session()


async def test_run_command_records_output_and_exit_code(apply_container: Container) -> None:
    session = await apply_container.terminal.create_session()

    event = await apply_container.terminal.run_command(session.id, _py("print('flux-terminal')"))

    assert event.kind == TerminalEventKind.COMMAND_FINISHED.value
    assert event.exit_code == 0
    # §5：命令来源必须可区分（T1 一律 user）
    assert event.source == TerminalSource.USER.value
    events = await apply_container.terminal.list_events(session.id)
    kinds = [item.kind for item in events]
    assert kinds[0] == TerminalEventKind.SESSION_CREATED.value
    assert TerminalEventKind.COMMAND_STARTED.value in kinds
    assert TerminalEventKind.OUTPUT.value in kinds
    assert any("flux-terminal" in (item.chunk or "") for item in events)


async def test_run_command_reports_failure_exit_code(apply_container: Container) -> None:
    session = await apply_container.terminal.create_session()

    event = await apply_container.terminal.run_command(session.id, _py("import sys; sys.exit(3)"))

    assert event.kind == TerminalEventKind.COMMAND_FAILED.value
    assert event.exit_code == 3


async def test_events_are_seq_ordered_and_resumable(apply_container: Container) -> None:
    """seq 单调不重复，且 after_seq 之后的续读结果与全量一致（§12 历史恢复的基础）。"""
    session = await apply_container.terminal.create_session()
    await apply_container.terminal.run_command(session.id, _py("print('one')"))

    all_events = await apply_container.terminal.list_events(session.id)
    seqs = [item.seq for item in all_events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)

    tail = await apply_container.terminal.list_events(session.id, after_seq=seqs[0])
    assert [item.seq for item in tail] == seqs[1:]


async def test_stop_closes_session_and_rejects_further_commands(
    apply_container: Container,
) -> None:
    session = await apply_container.terminal.create_session()

    stopped = await apply_container.terminal.stop(session.id)

    assert stopped.status == TerminalSessionStatus.STOPPED.value
    kinds = [item.kind for item in await apply_container.terminal.list_events(session.id)]
    assert TerminalEventKind.STOP_REQUESTED.value in kinds
    assert TerminalEventKind.SESSION_CLOSED.value in kinds
    with pytest.raises(ConflictError):
        await apply_container.terminal.run_command(session.id, _py("print('x')"))


async def test_stop_blocks_a_command_already_queued(
    apply_container: Container,
) -> None:
    session = await apply_container.terminal.create_session()
    running = asyncio.create_task(
        apply_container.terminal.run_command(
            session.id,
            _py("import time; time.sleep(30)"),
        )
    )
    await asyncio.sleep(0.4)

    queued = asyncio.create_task(
        apply_container.terminal.run_command(
            session.id,
            _py("print('must-not-run-after-stop')"),
        )
    )
    await asyncio.sleep(0.1)

    stopped = await apply_container.terminal.stop(session.id, force=True)
    assert stopped.status == TerminalSessionStatus.STOPPED.value

    with pytest.raises(ConflictError):
        await queued
    finished = await asyncio.wait_for(running, timeout=10)
    assert finished.kind == TerminalEventKind.COMMAND_FAILED.value

    events = await apply_container.terminal.list_events(session.id)
    assert not any(event.command and "must-not-run-after-stop" in event.command for event in events)


async def test_shutdown_closes_active_agent_sessions_and_rejects_new_work(
    apply_container: Container,
) -> None:
    first = await apply_container.terminal.create_session()
    second = await apply_container.terminal.create_session()

    await apply_container.terminal.shutdown()

    first_closed = await apply_container.terminal.get_session(first.id)
    second_closed = await apply_container.terminal.get_session(second.id)
    assert first_closed.status == TerminalSessionStatus.CLOSED.value
    assert second_closed.status == TerminalSessionStatus.CLOSED.value

    events = await apply_container.terminal.list_events(first.id)
    assert events[-1].kind == TerminalEventKind.SESSION_CLOSED.value

    with pytest.raises(ConflictError):
        await apply_container.terminal.create_session()
    with pytest.raises(ConflictError):
        await apply_container.terminal.run_command(second.id, _py("print('must-not-run')"))


async def test_shutdown_leaves_session_active_when_process_exit_is_unconfirmed(
    apply_container: Container,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = await apply_container.terminal.create_session()

    class LiveProcess:
        pid = 4242

        def poll(self):
            return None

    key = str(session.id)
    apply_container.terminal._processes[key] = LiveProcess()  # type: ignore[assignment]
    monkeypatch.setattr(
        apply_container.terminal,
        "_terminate_tree",
        lambda *_args, **_kwargs: False,
    )

    await apply_container.terminal.shutdown()

    assert (
        await apply_container.terminal.get_session(session.id)
    ).status == TerminalSessionStatus.ACTIVE.value
    apply_container.terminal._processes.clear()


async def test_create_session_closes_db_row_when_created_event_fails(
    apply_container: Container,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_emit(*_args, **_kwargs):
        raise RuntimeError("simulated event-store failure")

    monkeypatch.setattr(apply_container.terminal, "_emit", fail_emit)

    with pytest.raises(RuntimeError, match="simulated event-store failure"):
        await apply_container.terminal.create_session()

    rows = await apply_container.terminal_repo.list_sessions(
        kind=TerminalSessionKind.AGENT,
        status=TerminalSessionStatus.CLOSED,
    )
    assert rows


async def test_stop_fails_closed_when_process_tree_cannot_be_confirmed(
    apply_container: Container,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = await apply_container.terminal.create_session()
    monkeypatch.setattr(
        apply_container.terminal,
        "_terminate_tree",
        lambda *_args, **_kwargs: False,
    )
    task = asyncio.create_task(
        apply_container.terminal.run_command(session.id, _py("import time; time.sleep(30)"))
    )
    await asyncio.sleep(0.4)

    with pytest.raises(ConflictError):
        await apply_container.terminal.stop(session.id, force=True)

    assert (
        await apply_container.terminal.get_session(session.id)
    ).status == TerminalSessionStatus.ACTIVE.value

    # Clean up the process using the real terminator after validating fail-closed behavior.
    monkeypatch.undo()
    await apply_container.terminal.stop(session.id, force=True)
    await asyncio.wait_for(task, timeout=10)


async def test_force_stop_terminates_a_long_running_command(apply_container: Container) -> None:
    """Force Stop 必须真的杀掉进程树：命令立刻结束，而不是等它自然跑完（§3.4 / §10）。"""
    session = await apply_container.terminal.create_session()
    task = asyncio.create_task(
        apply_container.terminal.run_command(session.id, _py("import time; time.sleep(30)"))
    )
    await asyncio.sleep(1.0)  # 让命令真正跑起来

    stopped = await apply_container.terminal.stop(session.id, force=True)
    event = await asyncio.wait_for(task, timeout=10)

    assert stopped.status == TerminalSessionStatus.STOPPED.value
    assert event.kind == TerminalEventKind.COMMAND_FAILED.value
    assert event.exit_code not in (0, None)


def test_terminal_api_roundtrip(apply_client, workspace_root) -> None:
    created = apply_client.post("/api/v1/terminal/sessions", json={}).json()
    assert created["success"] is True
    session_id = created["data"]["id"]

    ran = apply_client.post(
        f"/api/v1/terminal/sessions/{session_id}/commands",
        json={"command": _py("print('api-check')")},
    ).json()
    assert ran["success"] is True
    assert ran["data"]["exit_code"] == 0

    events = apply_client.get(f"/api/v1/terminal/sessions/{session_id}/events").json()
    assert any("api-check" in (item["chunk"] or "") for item in events["data"])

    stopped = apply_client.post(
        f"/api/v1/terminal/sessions/{session_id}/stop", json={"force": False}
    ).json()
    assert stopped["data"]["status"] == TerminalSessionStatus.STOPPED.value
    assert stopped["metadata"]["force"] is False


# --- T3：SSE 实时流（§11 窗口同步 / §12 历史恢复）---


async def test_stream_replays_history_and_pushes_live_until_closed(
    apply_container: Container,
) -> None:
    """打开既运行中的会话：先补历史，再实时推 Stop 产生的事件，收流于 session.closed。"""
    session = await apply_container.terminal.create_session()
    await apply_container.terminal.run_command(session.id, _py("print('stream-me')"))

    collected: list[dict] = []

    async def consume() -> None:
        async for payload in apply_container.terminal.stream_events(session.id):
            if payload is None:  # 心跳哨兵
                continue
            collected.append(payload)
            if payload["kind"] == TerminalEventKind.SESSION_CLOSED.value:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.3)  # 让补历史先跑完，再触发实时事件
    await apply_container.terminal.stop(session.id)
    await asyncio.wait_for(task, timeout=5)

    kinds = [item["kind"] for item in collected]
    assert TerminalEventKind.OUTPUT.value in kinds
    assert kinds[-1] == TerminalEventKind.SESSION_CLOSED.value
    assert any("stream-me" in (item["chunk"] or "") for item in collected)
    seqs = [item["seq"] for item in collected]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)  # 历史与实时合并后不重复


async def test_stream_resumes_from_after_seq_on_finished_session(
    apply_container: Container,
) -> None:
    """已结束会话 + after_seq 续读：只补断点之后的事件，补完立即收流（不挂住连接）。"""
    session = await apply_container.terminal.create_session()
    await apply_container.terminal.run_command(session.id, _py("print('done')"))
    head = await apply_container.terminal.list_events(session.id)
    await apply_container.terminal.stop(session.id)
    tail = await apply_container.terminal.list_events(session.id, after_seq=head[-1].seq)

    collected: list[dict] = []
    async for payload in apply_container.terminal.stream_events(session.id, after_seq=head[-1].seq):
        if payload is not None:
            collected.append(payload)

    assert [item["seq"] for item in collected] == [item.seq for item in tail]
    assert collected[-1]["kind"] == TerminalEventKind.SESSION_CLOSED.value


async def test_stream_unknown_session_raises_not_found(apply_container: Container) -> None:
    with pytest.raises(NotFoundError):
        async for _ in apply_container.terminal.stream_events(
            "00000000-0000-0000-0000-000000000000"
        ):
            pass


def test_terminal_stream_sse_endpoint(apply_client) -> None:
    """SSE 闭环：事件帧带 id/event/data，会话结束后服务端主动收流。"""
    created = apply_client.post("/api/v1/terminal/sessions", json={}).json()
    session_id = created["data"]["id"]
    apply_client.post(
        f"/api/v1/terminal/sessions/{session_id}/commands",
        json={"command": _py("print('sse-check')")},
    )
    apply_client.post(f"/api/v1/terminal/sessions/{session_id}/stop", json={})

    url = f"/api/v1/terminal/sessions/{session_id}/stream?after_seq=0"
    with apply_client.stream("GET", url) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    assert "event: terminal.event" in body
    assert "id: 1" in body
    assert "sse-check" in body
    assert '"kind": "terminal.session.closed"' in body

    # 断线重连：Last-Event-ID 已到末尾时不再重放旧帧，也不挂住连接
    with apply_client.stream("GET", url, headers={"Last-Event-ID": "9999"}) as response:
        assert response.status_code == 200
        assert "data:" not in "".join(response.iter_text())


def test_terminal_stream_unknown_session_returns_404(apply_client) -> None:
    response = apply_client.get(
        "/api/v1/terminal/sessions/00000000-0000-0000-0000-000000000000/stream"
    )
    assert response.status_code == 404


@pytest.mark.skipif(os.name == "nt", reason="native PTY is not enabled on Windows")
def test_agent_terminal_list_excludes_human_pty_sessions(apply_client) -> None:
    human = apply_client.post("/api/v1/terminal/pty/sessions").json()
    assert human["success"] is True

    agent = apply_client.post("/api/v1/terminal/sessions", json={}).json()
    assert agent["success"] is True

    listed = apply_client.get("/api/v1/terminal/sessions").json()
    ids = {item["id"] for item in listed["data"]}
    assert agent["data"]["id"] in ids
    assert human["data"]["id"] not in ids


# --- FINDING-1 / FINDING-2：终端会话随 Run 回收 + 重启回收孤儿命令进程 ---

#: 一条会跑很久的命令：用于制造"会话 active 且命令进程在跑"的状态
_LONG_CMD = _py("import time; time.sleep(30)")

POSIX_ONLY = pytest.mark.skipif(platforms.IS_WINDOWS, reason="POSIX /proc 与进程组语义")


def _identity(run_id: str | None) -> AgentIdentity:
    return AgentIdentity(
        agent_id=str(uuid.uuid4()),
        token_id="tok",
        scopes=frozenset({Capability.TERMINAL_EXECUTE}),
        run_id=run_id,
    )


async def test_terminal_execute_binds_session_to_identity_run_id(
    apply_container: Container,
) -> None:
    """Agent 经 terminal.execute 建的会话必须落上 run_id（服务端校验过的 X-Flux-Run-Id）。"""
    run_id = uuid.uuid4()
    ctx = ToolContext(
        container=apply_container,
        identity=_identity(str(run_id)),
        tool_name="terminal.execute",
    )

    # 用低风险只读命令：本用例只验证会话绑 Run，不想触发高风险审批闸门
    result = await terminal_execute(ctx, {"command": "echo bound"})

    session = await apply_container.terminal.get_session(result["session_id"])
    assert session.run_id == run_id
    assert session.to_dict()["run_id"] == str(run_id)


async def test_terminal_execute_without_run_id_leaves_session_unbound(
    apply_container: Container,
) -> None:
    ctx = ToolContext(
        container=apply_container, identity=_identity(None), tool_name="terminal.execute"
    )
    result = await terminal_execute(ctx, {"command": "echo x"})
    session = await apply_container.terminal.get_session(result["session_id"])
    assert session.run_id is None


async def test_stop_run_sessions_stops_only_that_run(apply_container: Container) -> None:
    """Run 终态回收：只停该 Run 名下会话，其它 Run 的会话不受影响，命令进程被清掉。"""
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    a = await apply_container.terminal.create_session(run_id=run_a)
    b = await apply_container.terminal.create_session(run_id=run_b)

    task = asyncio.create_task(
        apply_container.terminal.run_command(a.id, _LONG_CMD, source=TerminalSource.AI)
    )
    # 等命令真正起进程（cmd_pgid 落库）再回收，否则测不到"进程被停"这一环
    for _ in range(100):
        if str(a.id) in apply_container.terminal._processes:
            break
        await asyncio.sleep(0.1)
    assert str(a.id) in apply_container.terminal._processes

    stopped = await apply_container.terminal.stop_run_sessions(run_a)
    await asyncio.gather(task, return_exceptions=True)

    assert stopped == 1
    assert (await apply_container.terminal.get_session(a.id)).status == (
        TerminalSessionStatus.STOPPED.value
    )
    assert (await apply_container.terminal.get_session(b.id)).status == (
        TerminalSessionStatus.ACTIVE.value
    )


@POSIX_ONLY
async def test_recover_orphan_commands_kills_matching_process(
    apply_container: Container,
) -> None:
    """重启回收：cmd_pgid 与事件里的命令一致（确认是本会话那条命令）才强杀。"""
    session = await apply_container.terminal.create_session(run_id=uuid.uuid4())
    process = subprocess.Popen(  # noqa: S602 - 用例本身就是要造一条真实命令进程
        _LONG_CMD, shell=True, start_new_session=True
    )
    try:
        await apply_container.terminal._repo.append_event(
            session.id,
            kind=TerminalEventKind.COMMAND_STARTED,
            source=TerminalSource.AI,
            command=_LONG_CMD,
        )
        await apply_container.terminal._repo.set_cmd_pgid(session.id, os.getpgid(process.pid))

        reclaimed = await apply_container.terminal.recover_orphan_commands()

        assert reclaimed == 1
        assert process.wait(timeout=5) is not None  # 进程已被杀（wait 返回退出码）
        recovered = await apply_container.terminal.get_session(session.id)
        assert recovered.status == TerminalSessionStatus.CLOSED.value
        assert recovered.cmd_pgid is None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@POSIX_ONLY
async def test_recover_orphan_commands_leaves_foreign_process(
    apply_container: Container,
) -> None:
    """命令文本与进程 argv 不一致（模拟 pgid 被复用）→ 绝不误杀，只清跟踪并标 closed。"""
    session = await apply_container.terminal.create_session(run_id=uuid.uuid4())
    process = subprocess.Popen(_LONG_CMD, shell=True, start_new_session=True)  # noqa: S602
    try:
        await apply_container.terminal._repo.append_event(
            session.id,
            kind=TerminalEventKind.COMMAND_STARTED,
            source=TerminalSource.AI,
            command="echo nothing-like-this",
        )
        await apply_container.terminal._repo.set_cmd_pgid(session.id, os.getpgid(process.pid))

        assert await apply_container.terminal.recover_orphan_commands() == 0
        assert process.poll() is None  # 未被误杀
        recovered = await apply_container.terminal.get_session(session.id)
        assert recovered.status == TerminalSessionStatus.CLOSED.value
        assert recovered.cmd_pgid is None
    finally:
        process.kill()
        process.wait()


@POSIX_ONLY
def test_can_reclaim_refuses_self_group_and_missing_command() -> None:
    """安全护栏：Flux 自身进程组绝不动手；没有命令文本也不动手。"""
    from flux.core.terminal.service import TerminalService

    assert TerminalService._can_reclaim(os.getpgrp(), "anything") is False
    assert TerminalService._can_reclaim(os.getpid(), None) is False
