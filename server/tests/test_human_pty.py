"""Human Terminal PTY 隔离与 I/O 测试。

Windows 不启用 native PTY；本文件在 Windows CI 上只做模块导入/收集，不执行 Unix PTY 用例。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys

import pytest
from starlette.websockets import WebSocketDisconnect

from flux.container import Container
from flux.core.terminal.pty_service import HumanPtyService, _live_group_members
from flux.enums import TerminalSessionKind, TerminalSessionStatus
from flux.errors import ConflictError

pytestmark = pytest.mark.skipif(os.name == "nt", reason="native PTY is not enabled on Windows")


async def test_human_terminal_session_is_persistently_isolated(
    apply_container: Container,
) -> None:
    human = await apply_container.human_pty.create_session()
    agent = await apply_container.terminal.create_session()

    assert human.kind == TerminalSessionKind.HUMAN.value
    assert agent.kind == TerminalSessionKind.AGENT.value

    with pytest.raises(ConflictError):
        await apply_container.human_pty.get_session(agent.id)

    await apply_container.human_pty.stop(human.id, force=True)
    assert (
        await apply_container.human_pty.get_session(human.id)
    ).status == TerminalSessionStatus.STOPPED.value


async def test_human_terminal_can_receive_input_and_stream_output(
    apply_container: Container,
) -> None:
    session = await apply_container.human_pty.create_session()
    chunks: list[str] = []
    marker = "flux-human-pty-check"

    async def consume_until_marker() -> None:
        async for chunk in apply_container.human_pty.stream_output(session.id):
            chunks.append(chunk)
            if marker in "".join(chunks):
                return

    consumer = asyncio.create_task(consume_until_marker())
    try:
        await asyncio.sleep(0.1)
        await apply_container.human_pty.send_input(session.id, f"printf '%s\\n' '{marker}'\\n")
        await asyncio.wait_for(consumer, timeout=5)
        assert marker in "".join(chunks)
    finally:
        if not consumer.done():
            consumer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await consumer
        await apply_container.human_pty.stop(session.id, force=True)


def test_human_terminal_api_only_lists_human_sessions(apply_client) -> None:
    created = apply_client.post("/api/v1/terminal/pty/sessions").json()
    assert created["success"] is True
    session_id = created["data"]["id"]
    assert created["data"]["kind"] == TerminalSessionKind.HUMAN.value

    agent = apply_client.post("/api/v1/terminal/sessions", json={}).json()
    assert agent["success"] is True
    assert agent["data"]["kind"] == TerminalSessionKind.AGENT.value

    human_list = apply_client.get("/api/v1/terminal/pty/sessions").json()
    ids = {item["id"] for item in human_list["data"]}
    assert session_id in ids
    assert agent["data"]["id"] not in ids

    agent_get_human = apply_client.get(f"/api/v1/terminal/sessions/{session_id}")
    assert agent_get_human.status_code == 409

    agent_run_human = apply_client.post(
        f"/api/v1/terminal/sessions/{session_id}/commands",
        json={"command": "echo should-not-run"},
    )
    assert agent_run_human.status_code == 409

    agent_stop_human = apply_client.post(
        f"/api/v1/terminal/sessions/{session_id}/stop",
        json={"force": True},
    )
    assert agent_stop_human.status_code == 409

    human_get_agent = apply_client.get(f"/api/v1/terminal/pty/sessions/{agent['data']['id']}")
    assert human_get_agent.status_code == 409

    stopped = apply_client.post(
        f"/api/v1/terminal/pty/sessions/{session_id}/stop",
        json={"force": False},
    )
    assert stopped.status_code == 200
    assert stopped.json()["data"]["status"] == TerminalSessionStatus.STOPPED.value

    stopped_detail = apply_client.get(f"/api/v1/terminal/pty/sessions/{session_id}")
    assert stopped_detail.status_code == 200
    assert stopped_detail.json()["data"]["status"] == TerminalSessionStatus.STOPPED.value

    live_humans = apply_client.get("/api/v1/terminal/pty/sessions").json()["data"]
    assert session_id not in {item["id"] for item in live_humans}


async def test_human_terminal_list_excludes_stopped_sessions(
    apply_container: Container,
) -> None:
    session = await apply_container.human_pty.create_session()
    assert session.id in {item.id for item in await apply_container.human_pty.list_sessions()}

    await apply_container.human_pty.stop(session.id, force=True)

    assert session.id not in {item.id for item in await apply_container.human_pty.list_sessions()}


def test_human_terminal_websocket_rejects_unknown_session(apply_client) -> None:
    ws_path = "/api/v1/terminal/pty/sessions/00000000-0000-0000-0000-000000000000/ws"
    with pytest.raises(WebSocketDisconnect) as exc, apply_client.websocket_connect(ws_path):
        pass
    assert exc.value.code == 1008


def test_human_terminal_websocket_roundtrip(apply_client) -> None:
    created = apply_client.post("/api/v1/terminal/pty/sessions").json()
    assert created["success"] is True
    session_id = created["data"]["id"]

    marker = "flux-human-ws-check"
    with apply_client.websocket_connect(
        f"/api/v1/terminal/pty/sessions/{session_id}/ws"
    ) as websocket:
        websocket.send_json({"type": "input", "data": f"printf '%s\\n' '{marker}'\\n"})
        chunks: list[str] = []
        for _ in range(20):
            message = websocket.receive_json()
            if message.get("type") != "output":
                continue
            chunks.append(str(message.get("data", "")))
            if marker in "".join(chunks):
                break
        assert marker in "".join(chunks)
        websocket.send_json({"type": "stop", "force": True})


async def test_human_terminal_recovers_stale_persisted_sessions(
    apply_container: Container,
) -> None:
    session = await apply_container.terminal_repo.create_session(
        workspace_root=str(apply_container.settings.workspace_root),
        kind=TerminalSessionKind.HUMAN,
    )
    assert session.status == TerminalSessionStatus.ACTIVE.value

    recovered = await apply_container.human_pty.recover_orphaned_sessions()

    assert recovered == 1
    assert (
        await apply_container.human_pty.get_session(session.id)
    ).status == TerminalSessionStatus.CLOSED.value


async def test_human_terminal_creation_cleans_up_when_created_event_fails(
    apply_container: Container,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_emit(*_args, **_kwargs):
        raise RuntimeError("simulated event-store failure")

    monkeypatch.setattr(apply_container.human_pty, "_emit", fail_emit)

    with pytest.raises(RuntimeError, match="simulated event-store failure"):
        await apply_container.human_pty.create_session()

    assert apply_container.human_pty._pids == {}
    assert apply_container.human_pty._fds == {}
    created = await apply_container.terminal_repo.list_sessions(
        kind=TerminalSessionKind.HUMAN,
        status=TerminalSessionStatus.CLOSED,
    )
    assert created


def _zombie_children(pid: int) -> set[int]:
    """返回 `pid` 的直接子进程中处于僵尸态的那些（Linux /proc 口径）。"""
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as handle:
            children = [int(item) for item in handle.read().split()]
    except (OSError, ValueError):
        return set()
    zombies: set[int] = set()
    for child in children:
        try:
            with open(f"/proc/{child}/stat", "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        end = data.rfind(b")")
        if end >= 0 and data[end + 2 : end + 3] == b"Z":
            zombies.add(child)
    return zombies


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="需要 /proc 核对进程状态")
async def test_pty_group_liveness_ignores_unreaped_zombies() -> None:
    """组内只剩未被回收的僵尸时，探活不得把僵尸算作"仍存活"。

    登录 shell 启动期 fork 的辅助进程在 SIGKILL 后可能以僵尸形态短暂留在组里
    （V1 验收 T-PROC-003 记录过同源口径问题）；把僵尸算作存活会让 stop 的组确认
    在 CI 负载下误判"未退出"。
    """
    script = (
        "import os, time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    os._exit(0)\n"
        "time.sleep(60)\n"  # 父进程不回收子进程，僵尸留在组内
    )
    leader = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    try:
        deadline = asyncio.get_running_loop().time() + 10
        zombies: set[int] = set()
        while asyncio.get_running_loop().time() < deadline and not zombies:
            zombies = _zombie_children(leader.pid)
            await asyncio.sleep(0.02)
        assert zombies, "未能构造出组内僵尸进程"
        live = _live_group_members(leader.pid)
        assert live is not None and len(live) == 1, f"僵尸被误判为存活：{live}"
        assert live[0].startswith(f"{leader.pid}(")
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(leader.pid, signal.SIGKILL)
        leader.wait(timeout=10)


async def test_pty_group_confirmation_resends_kill_signal() -> None:
    """忽略 SIGTERM 的组：不重发时窗口内确认不出来，重发 SIGKILL 必须杀干净。

    对应 CI 复现的边界：信号发出后新 fork 的组内成员收不到第一次信号，
    只发一次的确认会在负载下失败。
    """
    script = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", script],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert leader.stdout is not None
        assert leader.stdout.readline().strip() == "ready", "子进程未就绪"
        try:
            os.killpg(leader.pid, signal.SIGTERM)  # 温和信号被忽略：一次杀不掉
        except ProcessLookupError:
            pytest.fail("进程组未能建立")
        assert await HumanPtyService._wait_for_group_exit(leader.pid, leader.pid, 0.3) is False
        assert (
            await HumanPtyService._wait_for_group_exit(
                leader.pid, leader.pid, 2.0, resend_signal=signal.SIGKILL
            )
            is True
        )
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(leader.pid, signal.SIGKILL)
        leader.wait(timeout=10)


async def test_pty_stop_never_targets_flux_own_group(
    apply_container: Container,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fork 后子进程尚未 setsid 时，stop 绝不能向 Flux 自己的进程组发信号。

    `pty.fork()` 的 setsid 在子进程里完成，父进程可以在子进程完成 setsid 之前读到
    「进程组 == Flux 自己的组」（本地 2000 次抽样命中 470 次）。若照此 killpg，Flux
    会连自己一起杀掉——本地两次 pytest 被 SIGKILL、exit 137 即此路径。本用例用
    monkeypatch 稳定复现该窗口：能执行到断言，就说明 stop 没有误伤自身进程组
    （否则整个 pytest 进程已被信号杀死，根本跑不到这里）。
    """
    session = await apply_container.human_pty.create_session()
    pid = apply_container.human_pty._pids[str(session.id)]
    real_getpgid = os.getpgid

    def fake_getpgid(target: int) -> int:
        if target == pid:
            return os.getpgrp()  # 模拟"子进程尚未 setsid"的窗口期读数
        return real_getpgid(target)

    monkeypatch.setattr(os, "getpgid", fake_getpgid)
    stopped = await apply_container.human_pty.stop(session.id, force=True)

    assert stopped.status == TerminalSessionStatus.STOPPED.value
    assert session.id not in {item.id for item in await apply_container.human_pty.list_sessions()}
