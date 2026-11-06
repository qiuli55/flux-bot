"""Human Terminal PTY service.

Human Terminal is intentionally separate from Agent Terminal. A human terminal is an
interactive shell backed by a real pseudo-terminal: input is written to the PTY, output
keeps ANSI/control sequences, and resize changes the PTY window size.

The service owns one reader per PTY and fans output out to websocket subscribers. This
is important: a websocket reconnect must never create a second competing reader on the
same PTY. Sessions live in the process and are reaped by Container.dispose().

Linux/macOS use Python's native pty module. A Windows desktop build needs a ConPTY-backed
implementation before this service is enabled there.
"""

from __future__ import annotations

import asyncio
import errno
import os
import select
import signal
import struct
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

from flux.core.agent_runtime import platforms
from flux.core.event.bus import EventBus, Events
from flux.core.terminal.repository import TerminalRepository
from flux.core.virtual_workspace.apply_engine import resolve_workspace_root
from flux.enums import (
    TerminalEventKind,
    TerminalSessionKind,
    TerminalSessionStatus,
    TerminalSource,
)
from flux.errors import ConflictError
from flux.logging import get_logger
from flux.models.terminal import TerminalSession

if os.name != "nt":
    import pty
    import termios

logger = get_logger(__name__)

DEFAULT_COLS = 120
DEFAULT_ROWS = 32
SCROLLBACK_BYTES = 256 * 1024
QUEUE_SIZE = 256
STOP_GRACE_SECONDS = 1.0
#: 强杀后的"组已退出"确认窗口（与 Agent Terminal 的 KILL_CONFIRM_SECONDS=2.0 对齐）
KILL_CONFIRM_SECONDS = 2.0


def _live_group_members(pgid: int) -> list[str] | None:
    """Linux：列出进程组里仍"存活"（非僵尸）的成员；None 表示当前平台无法核对状态。

    为什么需要：`killpg(pgid, 0)` 探活会把"已死但未被回收的僵尸"算作"仍在"
    （V1 验收 T-PROC-003 已验证过该口径问题）。僵尸不会再执行任何指令、也无法
    再被信号杀死，按真实状态把它排除是事实判定，不是放宽判据。
    """
    if os.name == "nt" or not os.path.isdir("/proc"):
        return None
    live: list[str] = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        end = data.rfind(b")")
        if end < 0:
            continue
        fields = data[end + 2 :].split()
        if len(fields) < 3:
            continue
        try:
            if int(fields[2]) != pgid:
                continue
        except ValueError:
            continue
        if fields[0] in {b"Z", b"X", b"x"}:  # 僵尸/死亡态不参与"存活"判定
            continue
        live.append(f"{name}({fields[0].decode()})")
    return live


def _group_is_gone(pgid: int) -> bool:
    """进程组是否已无存活成员（僵尸不计；平台无法核对状态时沿用探活口径）。"""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    live = _live_group_members(pgid)
    if live is None:
        return False
    return not live


class HumanPtyService:
    """Manage interactive user-owned PTY sessions."""

    def __init__(
        self,
        repository: TerminalRepository,
        bus: EventBus | None = None,
        *,
        workspace_root: str | Path | None = None,
    ) -> None:
        self._repo = repository
        self._bus = bus
        self._configured_root = Path(workspace_root).expanduser() if workspace_root else None
        self._fds: dict[str, int] = {}
        self._pids: dict[str, int] = {}
        self._write_locks: dict[str, asyncio.Lock] = {}
        self._closed: set[str] = set()
        self._buffers: dict[str, bytearray] = {}
        self._subscribers: dict[str, set[asyncio.Queue[bytes | None]]] = {}
        self._reader_tasks: dict[str, asyncio.Task[None]] = {}
        self._finalizers: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    @property
    def workspace_root(self) -> Path | None:
        """当前 PTY 会话工作目录根（桌面端切换工作目录时由 Container 赋值）。"""
        return self._configured_root

    @workspace_root.setter
    def workspace_root(self, value: str | Path | None) -> None:
        self._configured_root = Path(value).expanduser() if value else None

    async def create_session(
        self, *, cols: int = DEFAULT_COLS, rows: int = DEFAULT_ROWS
    ) -> TerminalSession:
        if os.name == "nt":
            raise ConflictError("Windows 暂不支持 Human Terminal PTY，请等待 ConPTY 后端")
        root = resolve_workspace_root(self._configured_root)
        session = await self._repo.create_session(
            workspace_root=str(root),
            run_id=None,
            kind=TerminalSessionKind.HUMAN,
        )
        try:
            # pty.fork() must run on the event-loop thread. Forking from an asyncio
            # worker thread can inherit partially locked runtime state.
            pid, fd = self._spawn(str(root), cols, rows)
        except Exception:
            await self._repo.set_status(session.id, TerminalSessionStatus.CLOSED)
            raise

        key = str(session.id)
        self._pids[key] = pid
        self._fds[key] = fd
        self._buffers[key] = bytearray()
        self._subscribers[key] = set()

        # Persist session.created before starting the reader so a very fast shell exit
        # can never make session.closed appear before session.created in the event stream.
        try:
            await self._emit(session.id, TerminalEventKind.SESSION_CREATED)
        except Exception:
            await self._cleanup_spawned_process(key, pid, fd)
            with suppress(Exception):
                await self._repo.set_status(session.id, TerminalSessionStatus.CLOSED)
            raise

        self._reader_tasks[key] = asyncio.create_task(self._read_loop(key), name=f"flux-pty-{key}")
        return session

    async def recover_orphaned_sessions(self) -> int:
        """Mark persisted Human PTYs from a previous process as closed.

        PTY processes are process-local and intentionally not resurrected after a Flux
        restart. The database must therefore not advertise stale active sessions.
        """
        sessions = await self._repo.list_sessions(
            limit=10000,
            kind=TerminalSessionKind.HUMAN,
            status=TerminalSessionStatus.ACTIVE,
        )
        recovered = 0
        for session in sessions:
            if str(session.id) in self._fds:
                continue
            await self._repo.set_status(session.id, TerminalSessionStatus.CLOSED)
            await self._emit(session.id, TerminalEventKind.SESSION_CLOSED)
            recovered += 1
        return recovered

    async def get_session(self, session_id: str | uuid.UUID) -> TerminalSession:
        session = await self._repo.get_session(session_id)
        if session.kind != TerminalSessionKind.HUMAN.value:
            raise ConflictError(
                "该会话属于 Agent Terminal，不是 Human Terminal",
                details={"session_id": str(session.id), "kind": session.kind},
            )
        return session

    async def list_sessions(self, *, limit: int = 20) -> list[TerminalSession]:
        """List only live Human Terminal sessions owned by this Flux process."""
        sessions = await self._repo.list_sessions(
            limit=limit,
            kind=TerminalSessionKind.HUMAN,
            status=TerminalSessionStatus.ACTIVE,
        )
        return [session for session in sessions if str(session.id) in self._fds]

    async def send_input(self, session_id: str | uuid.UUID, data: str) -> None:
        key = str(session_id)
        fd = self._fds.get(key)
        if fd is None or key in self._closed:
            raise ConflictError("终端会话已结束", details={"session_id": key})
        if not data:
            return
        payload = data.encode("utf-8", errors="replace")
        lock = self._write_locks.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                await asyncio.to_thread(self._write_all, fd, payload)
            except (OSError, ValueError) as exc:
                raise ConflictError(
                    "终端输入写入失败",
                    details={"session_id": key},
                ) from exc

    async def resize(self, session_id: str | uuid.UUID, cols: int, rows: int) -> None:
        key = str(session_id)
        fd = self._fds.get(key)
        if fd is None or key in self._closed:
            raise ConflictError("终端会话已结束", details={"session_id": key})
        cols = max(20, min(int(cols), 500))
        rows = max(5, min(int(rows), 200))
        await asyncio.to_thread(self._resize_fd, fd, cols, rows)

    async def stream_output(self, session_id: str | uuid.UUID) -> AsyncIterator[str]:
        """Subscribe to one PTY's output without creating a competing reader."""
        key = str(session_id)
        if key not in self._fds:
            raise ConflictError("PTY 会话不存在或已结束", details={"session_id": key})

        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=QUEUE_SIZE)
        subscribers = self._subscribers.setdefault(key, set())
        subscribers.add(queue)
        try:
            buffered = bytes(self._buffers.get(key, b""))
            if buffered:
                yield buffered.decode("utf-8", errors="replace")
            while key not in self._closed:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield chunk.decode("utf-8", errors="replace")
        finally:
            subscribers.discard(queue)

    async def stop(self, session_id: str | uuid.UUID, *, force: bool = False) -> TerminalSession:
        key = str(session_id)
        pid = self._pids.get(key)
        if pid is None:
            # 进程句柄已被摘除，但终结可能还在落库（reader 触发的）。等它完成再读状态，
            # 否则 stop 会把仍是 active 的会话返回给用户。
            finalizer = self._finalizers.get(key)
            if finalizer is not None and not finalizer.done():
                with suppress(asyncio.CancelledError):
                    await asyncio.shield(finalizer)
            return await self.get_session(session_id)
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            await self._finish(key)
            return await self.get_session(session_id)

        if platforms.is_self_group(pgid):
            # `pty.fork()` 的 setsid 是在子进程里完成的：父进程可能在子进程完成
            # setsid 之前读到「进程组 == Flux 自己的组」（本地 2000 次抽样命中 470 次）。
            # 对这个 pgid 绝不能 killpg——那会把 Flux 自己杀掉（本地两次 pytest 被
            # SIGKILL、exit 137 即此路径）。此刻子进程还没 exec shell、不可能有别的
            # 组员，按 pid 终止这一个进程并等回收即可。
            if not await self._stop_single_process(pid, force=force):
                raise self._unconfirmed_group_error(key, pid)
            await self._finish(key)
            return await self.get_session(session_id)

        try:
            signal_value = signal.SIGKILL if force else signal.SIGTERM
            await asyncio.to_thread(os.killpg, pgid, signal_value)
        except ProcessLookupError:
            await self._finish(key)
            return await self.get_session(session_id)

        if not force:
            confirmed = await self._wait_for_group_exit(pgid, pid, STOP_GRACE_SECONDS)
            if not confirmed:
                with suppress(ProcessLookupError):
                    await asyncio.to_thread(os.killpg, pgid, signal.SIGKILL)
                confirmed = await self._wait_for_group_exit(
                    pgid, pid, KILL_CONFIRM_SECONDS, resend_signal=signal.SIGKILL
                )
            if not confirmed:
                raise self._unconfirmed_group_error(key, pgid)
        else:
            confirmed = await self._wait_for_group_exit(
                pgid, pid, KILL_CONFIRM_SECONDS, resend_signal=signal.SIGKILL
            )
            if not confirmed:
                raise self._unconfirmed_group_error(key, pgid)
        await self._finish(key)
        return await self.get_session(session_id)

    @staticmethod
    def _unconfirmed_group_error(key: str, pgid: int) -> ConflictError:
        """确认失败：先把组内残留成员写进日志（CI 复现时可直接定位），再给统一错误。"""
        logger.warning(
            "Human Terminal 进程组未能确认退出 session=%s pgid=%s 残留成员=%s",
            key,
            pgid,
            _live_group_members(pgid),
        )
        return ConflictError(
            "无法确认 Human Terminal 进程组已退出",
            details={"session_id": key, "pgid": pgid},
        )

    async def shutdown(self) -> None:
        """Best-effort cleanup during app shutdown.

        Never block the whole shutdown path on one PTY.
        """
        for key in list(self._pids):
            pid = self._pids.get(key)
            fd = self._fds.get(key)
            try:
                await self.stop(key, force=True)
            except Exception:
                if pid is not None and fd is not None:
                    with suppress(Exception):
                        await self._cleanup_spawned_process(key, pid, fd)
                with suppress(Exception):
                    await self._repo.set_status(
                        key,
                        TerminalSessionStatus.CLOSED,
                    )

    async def _cleanup_spawned_process(self, key: str, pid: int, fd: int) -> None:
        self._closed.add(key)
        self._pids.pop(key, None)
        self._fds.pop(key, None)
        self._write_locks.pop(key, None)
        self._subscribers.pop(key, None)
        self._buffers.pop(key, None)
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            pgid = pid
        if platforms.is_self_group(pgid):
            # 尚未 setsid 的窗口期（详见 stop()）：killpg 会打到 Flux 自己，只单杀。
            with suppress(ProcessLookupError):
                await asyncio.to_thread(os.kill, pid, signal.SIGKILL)
        else:
            with suppress(ProcessLookupError):
                await asyncio.to_thread(os.killpg, pgid, signal.SIGKILL)
        with suppress(OSError):
            await asyncio.to_thread(os.close, fd)
        with suppress(ChildProcessError, ProcessLookupError):
            await asyncio.to_thread(os.waitpid, pid, 0)

    def _spawn(self, cwd: str, cols: int, rows: int) -> tuple[int, int]:
        if os.name == "nt":
            raise ConflictError("Windows 暂不支持 Human Terminal PTY，请使用 ConPTY 实现")
        shell = os.environ.get("SHELL") or "/bin/bash"
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(cwd)
            env = os.environ.copy()
            env.setdefault("TERM", "xterm-256color")
            env.setdefault("COLORTERM", "truecolor")
            os.execvpe(shell, [shell, "-l"], env)
        self._resize_fd(fd, cols, rows)
        os.set_blocking(fd, False)
        return pid, fd

    @staticmethod
    def _resize_fd(fd: int, cols: int, rows: int) -> None:
        if os.name == "nt":
            return
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        with suppress(AttributeError):
            termios.tcsetwinsize(fd, (rows, cols))
        try:
            import fcntl

            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        except (AttributeError, OSError):
            pass

    @staticmethod
    async def _stop_single_process(pid: int, *, force: bool) -> bool:
        """只终止 pid 这一个进程并确认回收（绝不按进程组）。

        用于「子进程尚未 setsid、仍与 Flux 同组」的窗口期：此时它还没有 exec shell，
        不可能已有其他组员，按 pid 单杀即可，且绝不能用 killpg（会打到 Flux 自己）。
        """
        if not force:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
            if await HumanPtyService._wait_for_process_exit(pid, STOP_GRACE_SECONDS):
                return True
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        return await HumanPtyService._wait_for_process_exit(pid, KILL_CONFIRM_SECONDS)

    @staticmethod
    async def _wait_for_process_exit(pid: int, timeout: float) -> bool:
        """确认单个子进程已被回收：以 waitpid 为准（顺带回收僵尸），不依赖组探活。"""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                if (await asyncio.to_thread(os.waitpid, pid, os.WNOHANG))[0] == pid:
                    return True
            except ChildProcessError:
                return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.05)

    @staticmethod
    async def _wait_for_group_exit(
        pgid: int,
        pid: int,
        timeout: float,
        *,
        resend_signal: int | None = None,
    ) -> bool:
        """确认组长已被回收「且」组内不再有存活成员。

        `resend_signal` 给出时在等待期间周期重发该信号：登录 shell 启动阶段会在信号
        发出之后继续 fork 新成员（子 shell / 启动辅助进程，它们收不到第一次信号），
        只发一次就可能留下"杀不干净"的组——CI 上复现的 stop 确认失败即由这类残留
        触发（本地探针 40 次中 25 次可见同源残留成员，存活 6~27ms）。

        两个条件缺一不可：只看 `_group_is_gone` 时，组长已成僵尸（被僵尸过滤排除）
        但尚未回收就会被误判为"已退出"；只看 waitpid 则漏掉组内残留成员。
        """
        deadline = asyncio.get_running_loop().time() + timeout
        leader_reaped = False
        while asyncio.get_running_loop().time() < deadline:
            if resend_signal is not None:
                with suppress(ProcessLookupError):
                    os.killpg(pgid, resend_signal)
            if not leader_reaped:
                try:
                    result = await asyncio.to_thread(os.waitpid, pid, os.WNOHANG)
                    leader_reaped = result[0] == pid
                except ChildProcessError:
                    leader_reaped = True

            if leader_reaped and _group_is_gone(pgid):
                return True

            await asyncio.sleep(0.05)

        if not leader_reaped:
            try:
                result = await asyncio.to_thread(os.waitpid, pid, os.WNOHANG)
                leader_reaped = result[0] == pid
            except ChildProcessError:
                leader_reaped = True
        if not leader_reaped:
            return False
        return _group_is_gone(pgid)

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            try:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("PTY write returned no progress")
                view = view[written:]
            except BlockingIOError:
                _ready, writable, _exceptional = select.select([], [fd], [], 0.2)
                if not writable:
                    continue

    async def _read_loop(self, key: str) -> None:
        fd = self._fds.get(key)
        if fd is None:
            return
        loop = asyncio.get_running_loop()
        done = asyncio.Event()

        def on_readable() -> None:
            try:
                data = os.read(fd, 64 * 1024)
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno not in {errno.EIO, errno.EBADF}:
                    return
                done.set()
                return
            if not data:
                done.set()
                return
            buffer = self._buffers.setdefault(key, bytearray())
            buffer.extend(data)
            if len(buffer) > SCROLLBACK_BYTES:
                del buffer[:-SCROLLBACK_BYTES]
            for queue in tuple(self._subscribers.get(key, ())):
                try:
                    queue.put_nowait(data)
                except asyncio.QueueFull:
                    with suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                    with suppress(asyncio.QueueFull):
                        queue.put_nowait(data)

        loop.add_reader(fd, on_readable)
        try:
            await done.wait()
        finally:
            with suppress(Exception):
                loop.remove_reader(fd)
            await self._finish(key)

    async def _finish(self, key: str) -> None:
        """终结一个 PTY 会话；并发调用共享同一次终结，并且都等它跑完。

        reader 退出（shell 自己结束）与 stop() 会同时走到这里，谁先到不固定。
        状态与事件是在终结末尾才落库的，若后到的调用直接返回，stop() 就会把
        "刚停掉却仍是 active"的会话返回出去（真机复现：SIGKILL 后 reader 先拿到
        EIO 触发终结，stop 在守卫处提前返回）。因此这里不做提前返回。
        """
        async with self._lock:
            finalizer = self._finalizers.get(key)
            if finalizer is None:
                if key in self._closed and key not in self._fds:
                    # 状态已被此前的终结摘除（例如创建失败路径已清理），没有要等的东西。
                    return
                finalizer = asyncio.create_task(self._finalize(key), name=f"flux-pty-finish-{key}")
                self._finalizers[key] = finalizer
        if finalizer is asyncio.current_task():
            return
        with suppress(asyncio.CancelledError):
            await asyncio.shield(finalizer)

    async def _finalize(self, key: str) -> None:
        try:
            async with self._lock:
                if key in self._closed and key not in self._fds:
                    return
                self._closed.add(key)
                fd = self._fds.pop(key, None)
                pid = self._pids.pop(key, None)
                reader = self._reader_tasks.pop(key, None)
                subscribers = self._subscribers.pop(key, set())
                self._buffers.pop(key, None)
                self._write_locks.pop(key, None)

            if fd is not None:
                with suppress(OSError):
                    asyncio.get_running_loop().remove_reader(fd)
                with suppress(OSError):
                    os.close(fd)
            for queue in subscribers:
                with suppress(asyncio.QueueFull):
                    queue.put_nowait(None)

            # 只取消、不等待 reader：reader 的收尾里会 await 本次终结（_finish），
            # 终结再等 reader 会互相等待成死锁。
            if reader is not None and reader is not asyncio.current_task():
                reader.cancel()

            if pid is not None:
                with suppress(ChildProcessError, ProcessLookupError):
                    await asyncio.to_thread(os.waitpid, pid, 0)

            try:
                session = await self._repo.get_session(key)
                if TerminalSessionStatus(session.status) is TerminalSessionStatus.ACTIVE:
                    await self._repo.set_status(session.id, TerminalSessionStatus.STOPPED)
                await self._emit(session.id, TerminalEventKind.SESSION_CLOSED)
            except Exception:
                return
        finally:
            self._finalizers.pop(key, None)

    async def _emit(self, session_id: uuid.UUID, kind: TerminalEventKind) -> None:
        event = await self._repo.append_event(
            session_id,
            kind=kind,
            source=TerminalSource.USER,
        )
        if self._bus is not None:
            await self._bus.publish(Events.TERMINAL_EVENT, event.to_dict())
