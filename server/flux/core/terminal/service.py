"""Terminal Session 服务（Agent Terminal Console §6 / §10）。

Flux 自己执行命令，因此命令、输出、退出码都拿得到（不再依赖 Agent 运行时内部行为）：

- 每个会话固定一个工作区根，命令在其中以**独立进程组**执行；
- 输出按行落成 `terminal.output` 事件（seq 单调），前端按 seq 续读即恢复历史（§12）；
- Stop 走 SIGTERM → grace → SIGKILL → **确认进程组消失**，确认之后才落 stopped，
  杜绝 §10 禁止的"UI 已停止而后台仍在跑"。

平台：进程组隔离与终止走 platforms 原语（W-1：POSIX `setsid`/`killpg`，Windows
`CREATE_NEW_PROCESS_GROUP`/`taskkill /T /F`），本模块不再直接触碰平台 API。
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from flux.core.agent_runtime import platforms
from flux.core.context.redact import RedactionMatch, RedactionStore, redact
from flux.core.event.bus import EventBus, Events
from flux.core.terminal.repository import TerminalRepository
from flux.core.virtual_workspace.apply_engine import resolve_workspace_root
from flux.enums import (
    TerminalEventKind,
    TerminalSessionKind,
    TerminalSessionStatus,
    TerminalSource,
)
from flux.errors import ConflictError, ValidationError
from flux.logging import get_logger
from flux.models.terminal import TerminalEvent, TerminalSession

logger = get_logger(__name__)

#: Stop 的宽限期：SIGTERM 后等这么久，仍不退出就升级 SIGKILL
STOP_GRACE_SECONDS = 5.0
#: SIGKILL 后确认进程组消失的等待上限
KILL_CONFIRM_SECONDS = 2.0
#: 实时流无事件时的心跳间隔（秒）；到点发一个 None 哨兵，API 层转成 SSE 注释行
STREAM_HEARTBEAT_SECONDS = 15.0
#: 补历史时的分页大小
STREAM_REPLAY_PAGE = 1000


def _as_run_uuid(value: str | uuid.UUID) -> uuid.UUID | None:
    """把 run_id（uuid 或 32 位 hex 字符串）规整成 UUID；非法返回 None。"""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _read_proc_cmdline(pid: int) -> list[str] | None:
    """读 `/proc/<pid>/cmdline`（POSIX）并 NUL 切分为 argv；读不到返回 None。

    用于重启后回收孤儿命令进程前的归属核实：pid/pgid 可能被系统复用，比对 argv 才能确认
    这确实是上次 Flux 遗留的那条命令，而不是恰好复用同一 id 的无关进程。
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:  # noqa: PTH123 - /proc 无 pathlib 语义
            raw = handle.read()
    except OSError:
        return None
    if not raw:
        return None
    return [part.decode("utf-8", "replace") for part in raw.split(b"\x00") if part]


class TerminalService:
    """终端会话管理：会话生命周期 + 命令执行 + Stop / Force Stop。"""

    def __init__(
        self,
        repository: TerminalRepository,
        bus: EventBus | None = None,
        *,
        workspace_root: str | Path | None = None,
        redactions: RedactionStore | None = None,
    ) -> None:
        self._repo = repository
        self._bus = bus
        self._configured_root = Path(workspace_root).expanduser() if workspace_root else None
        #: 入站脱敏映射表（§10.1）：command/chunk 落库前先打码，换下来的原文进此表；无密钥时不落库
        self._redactions = redactions
        #: 正在跑的进程（会话 ID → Popen），Stop 与 shutdown 都从这里找
        self._processes: dict[str, subprocess.Popen[str]] = {}
        #: 正在执行的命令 task；Stop 必须等它写完 command.finished/failed 再关闭会话。
        self._command_tasks: dict[str, asyncio.Task[TerminalEvent]] = {}
        #: 正在停止的会话：阻断已经排队、尚未启动的下一条命令。
        self._stopping: set[str] = set()
        #: 服务开始退出后拒绝新的会话/命令，避免 shutdown 与新请求产生竞态。
        self._shutting_down = False
        #: 每会话一把锁：同一会话的命令串行执行
        self._locks: dict[str, asyncio.Lock] = {}
        #: seq 分配与落库串行化：并发 emit（输出线程 + Stop）不允许撞号
        self._emit_lock = asyncio.Lock()

    @property
    def workspace_root(self) -> Path | None:
        """当前终端工作目录根（桌面端切换工作目录时由 Container 赋值）。"""
        return self._configured_root

    @workspace_root.setter
    def workspace_root(self, value: str | Path | None) -> None:
        self._configured_root = Path(value).expanduser() if value else None

    @property
    def redactions(self) -> RedactionStore | None:
        """入站脱敏映射表；由 Container 装配注入（构造顺序在 TerminalService 之后）。"""
        return self._redactions

    @redactions.setter
    def redactions(self, value: RedactionStore | None) -> None:
        self._redactions = value

    # --- 会话 ---

    async def create_session(
        self, *, run_id: uuid.UUID | None = None, workspace_root: str | Path | None = None
    ) -> TerminalSession:
        if self._shutting_down:
            raise ConflictError("终端服务正在关闭，暂不接受新会话")
        root = self._resolve_root(workspace_root)
        session = await self._repo.create_session(workspace_root=str(root), run_id=run_id)
        try:
            await self._emit(session.id, TerminalEventKind.SESSION_CREATED, TerminalSource.USER)
        except Exception:
            with contextlib.suppress(Exception):
                await self._repo.set_status(session.id, TerminalSessionStatus.CLOSED)
            raise
        return session

    async def get_session(self, session_id: str | uuid.UUID) -> TerminalSession:
        session = await self._repo.get_session(session_id)
        self._ensure_agent_session(session)
        return session

    async def list_sessions(
        self,
        *,
        limit: int = 20,
        kind: TerminalSessionKind = TerminalSessionKind.AGENT,
    ) -> list[TerminalSession]:
        """List sessions owned by the requested terminal runtime."""
        return await self._repo.list_sessions(limit=limit, kind=kind)

    async def list_events(
        self, session_id: str | uuid.UUID, *, after_seq: int = 0, limit: int = 2000
    ) -> list[TerminalEvent]:
        session = await self.get_session(session_id)
        return await self._repo.list_events(session.id, after_seq=after_seq, limit=limit)

    async def stream_events(
        self, session_id: str | uuid.UUID, *, after_seq: int = 0
    ) -> AsyncIterator[dict[str, Any] | None]:
        """历史续读 + 实时增量（§11 窗口同步 / §12 历史恢复）。

        顺序上先订阅总线再补历史：两者之间到达的事件会进订阅缓冲，补历史后按 seq 去重
        合并，因此既不重复也不丢。会话已结束（session.closed）时推完即收流；仍在跑则保持
        连接，直到客户端断开（关闭终端窗口不停 Agent，§11）。产出 `None` 表示心跳哨兵。
        """
        session = await self.get_session(session_id)
        key = str(session.id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def on_event(_event: str, payload: dict[str, Any]) -> None:
            if str(payload.get("session_id")) == key:
                queue.put_nowait(payload)

        if self._bus is not None:
            self._bus.subscribe(Events.TERMINAL_EVENT, on_event)
        last_seq = after_seq
        closed = False
        try:
            # 1) 补历史：分页读到追上为止
            while True:
                batch = await self._repo.list_events(
                    session.id, after_seq=last_seq, limit=STREAM_REPLAY_PAGE
                )
                if not batch:
                    break
                for event in batch:
                    if event.seq <= last_seq:
                        continue
                    last_seq = event.seq
                    payload = event.to_dict()
                    yield payload
                    if payload["kind"] == TerminalEventKind.SESSION_CLOSED.value:
                        closed = True
                if len(batch) < STREAM_REPLAY_PAGE or closed:
                    break
            # 2) 合并补历史期间缓冲的实时事件（按 seq 去重）
            while not closed and not queue.empty():
                payload = queue.get_nowait()
                seq = payload.get("seq")
                if not isinstance(seq, int) or seq <= last_seq:
                    continue
                last_seq = seq
                yield payload
                if payload["kind"] == TerminalEventKind.SESSION_CLOSED.value:
                    closed = True
            # 会话已结束且历史已补完：不可能再有新事件，直接收流（重连时 after_seq 已在末尾）
            if (
                not closed
                and TerminalSessionStatus(session.status) is not TerminalSessionStatus.ACTIVE
                and last_seq >= session.next_seq - 1
                and queue.empty()
            ):
                return
            # 3) 实时推送
            while not closed:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=STREAM_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:  # noqa: UP041 - 3.10 下与内置 TimeoutError 不同源
                    # 总线不可用时也能推进：直接查库补漏
                    if self._bus is None:
                        for event in await self._repo.list_events(
                            session.id, after_seq=last_seq, limit=STREAM_REPLAY_PAGE
                        ):
                            last_seq = event.seq
                            yield event.to_dict()
                    if not closed:
                        yield None
                    continue
                seq = payload.get("seq")
                if not isinstance(seq, int) or seq <= last_seq:
                    continue
                last_seq = seq
                yield payload
                if payload["kind"] == TerminalEventKind.SESSION_CLOSED.value:
                    closed = True
        finally:
            if self._bus is not None:
                self._bus.unsubscribe(Events.TERMINAL_EVENT, on_event)

    # --- 执行 ---

    async def run_command(
        self,
        session_id: str | uuid.UUID,
        command: str,
        *,
        source: TerminalSource = TerminalSource.USER,
    ) -> TerminalEvent:
        """在会话的工作区根里执行一条命令；输出逐行落库，返回结束事件。"""
        text = (command or "").strip()
        if not text:
            raise ValidationError("命令不能为空", details={"session_id": str(session_id)})
        if self._shutting_down:
            raise ConflictError(
                "终端服务正在关闭，不能执行命令",
                details={"session_id": str(session_id)},
            )
        session = await self.get_session(session_id)
        if TerminalSessionStatus(session.status) is not TerminalSessionStatus.ACTIVE:
            raise ConflictError(
                f"终端会话已 {session.status}，不能再执行命令",
                details={"session_id": str(session.id), "status": session.status},
            )
        key = str(session.id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Stop 可能在本条命令排队期间发生；拿到锁后必须重新读取权威状态。
            session = await self.get_session(key)
            if (
                key in self._stopping
                or TerminalSessionStatus(session.status) is not TerminalSessionStatus.ACTIVE
            ):
                raise ConflictError(
                    f"终端会话已 {session.status}，不能再执行命令",
                    details={"session_id": key, "status": session.status},
                )
            return await self._run_locked(session, text, source)

    @staticmethod
    def _ensure_agent_session(session: TerminalSession) -> None:
        if session.kind != TerminalSessionKind.AGENT.value:
            raise ConflictError(
                "该会话属于 Human Terminal，不是 Agent Terminal",
                details={"session_id": str(session.id), "kind": session.kind},
            )

    async def _run_locked(
        self, session: TerminalSession, command: str, source: TerminalSource
    ) -> TerminalEvent:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Terminal command must run inside an asyncio task")
        key = str(session.id)
        # 会话级一次性取值：run_id 可能为 None → 回退用会话 id；turn_id 即会话 id。
        # 逐行输出的热路径（_pump_output）只转发这两个值，不再查库（§10.1）。
        run_id = str(session.run_id) if session.run_id is not None else key
        turn_id = key
        self._command_tasks[key] = task
        try:
            await self._emit(
                session.id,
                TerminalEventKind.COMMAND_STARTED,
                source,
                command=command,
                run_id=run_id,
                turn_id=turn_id,
            )
            loop = asyncio.get_running_loop()
            process = subprocess.Popen(  # noqa: S602 - 终端本身就是为了执行命令（用户/Agent 主动发起）
                command,
                shell=True,
                cwd=session.workspace_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                # 命令输出常含中文（git / pytest / 用户脚本）：Windows 文本管道默认走
                # locale（cp1252），读取线程会 UnicodeDecodeError 并丢掉整段输出。
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **platforms.popen_kwargs(),  # 独立进程组：Stop 只杀本会话，不误伤 Flux 自身
            )
            self._processes[key] = process
            # 记下命令进程组 id 并落库：Flux 若被 SIGKILL，重启后据此回收这个孤儿进程。
            try:
                await self._repo.set_cmd_pgid(session.id, platforms.process_group_of(process)[1])
            except Exception:  # noqa: BLE001 - 记账失败不能拖垮命令执行
                logger.warning("记录命令进程组 id 失败 session=%s", session.id, exc_info=True)
            try:
                await asyncio.to_thread(
                    self._pump_output, loop, session.id, process, source, run_id, turn_id
                )
                exit_code = await asyncio.to_thread(process.wait)
            finally:
                self._processes.pop(key, None)
                # 命令已结束：清空进程组跟踪，避免重启时把已退出的 id 误当孤儿处理。
                try:
                    await self._repo.set_cmd_pgid(session.id, None)
                except Exception:  # noqa: BLE001 - 清账失败不能拖垮命令收尾
                    logger.warning("清空命令进程组 id 失败 session=%s", session.id, exc_info=True)
            finished = (
                TerminalEventKind.COMMAND_FINISHED
                if exit_code == 0
                else TerminalEventKind.COMMAND_FAILED
            )
            return await self._emit(
                session.id,
                finished,
                source,
                command=command,
                exit_code=exit_code,
                run_id=run_id,
                turn_id=turn_id,
            )
        finally:
            self._command_tasks.pop(key, None)

    def _pump_output(
        self,
        loop: asyncio.AbstractEventLoop,
        session_id: uuid.UUID,
        process: subprocess.Popen[str],
        source: TerminalSource,
        run_id: str,
        turn_id: str,
    ) -> None:
        """工作线程按行读输出 → 回事件循环落库（保证输出顺序与 seq 一致）。"""
        stream = process.stdout
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                future = asyncio.run_coroutine_threadsafe(
                    self._emit(
                        session_id,
                        TerminalEventKind.OUTPUT,
                        source,
                        chunk=line,
                        run_id=run_id,
                        turn_id=turn_id,
                    ),
                    loop,
                )
                future.result()
        finally:
            stream.close()

    # --- Stop / Force Stop（§3.3 / §3.4 / §10）---

    async def stop(self, session_id: str | uuid.UUID, *, force: bool = False) -> TerminalSession:
        """停止会话；confirm 到进程组消失之后才落 stopped，状态不谎报。"""
        session = await self.get_session(session_id)
        key = str(session.id)
        if key in self._stopping:
            raise ConflictError(
                "终端会话正在停止",
                details={"session_id": key, "status": session.status},
            )
        self._stopping.add(key)
        try:
            await self._emit(
                session.id,
                TerminalEventKind.STOP_REQUESTED,
                TerminalSource.USER,
                command="force-stop" if force else "stop",
            )
            process = self._processes.get(key)
            if process is not None and process.poll() is None:
                terminated = await asyncio.to_thread(
                    self._terminate_tree,
                    process,
                    force=force,
                )
                if not terminated:
                    raise ConflictError(
                        "无法确认终端进程树已退出",
                        details={"session_id": key, "force": force},
                    )

            command_task = self._command_tasks.get(key)
            if command_task is not None and command_task is not asyncio.current_task():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(command_task),
                        timeout=STOP_GRACE_SECONDS + KILL_CONFIRM_SECONDS + 1.0,
                    )
                except asyncio.TimeoutError as exc:
                    raise ConflictError(
                        "终端命令未能在停止后完成收尾",
                        details={"session_id": key},
                    ) from exc

            if process is not None:
                await self._emit(
                    session.id,
                    TerminalEventKind.PROCESS_TERMINATED,
                    TerminalSource.USER,
                )
            await self._emit(
                session.id,
                TerminalEventKind.SESSION_CLOSED,
                TerminalSource.USER,
            )
            return await self._repo.set_status(session.id, TerminalSessionStatus.STOPPED)
        finally:
            self._stopping.discard(key)

    async def stop_run_sessions(self, run_id: str | uuid.UUID) -> int:
        """把某个 Run 名下仍未结束的 Agent 终端会话全部 Force Stop（Run 终态时调用）。

        Agent 经 `terminal.execute` 派生的会话/命令进程由此随 Run 一起收摊，杜绝
        "Run 已结束、命令进程还在跑、会话还 active" 的泄漏（FINDING-1）。单个会话停止
        失败只记日志、不影响其它会话。返回成功停掉的条数。
        """
        key = _as_run_uuid(run_id)
        if key is None:
            return 0
        sessions = await self._repo.list_sessions(
            limit=10000,
            kind=TerminalSessionKind.AGENT,
            status=TerminalSessionStatus.ACTIVE,
            run_id=key,
        )
        stopped = 0
        for session in sessions:
            try:
                await self.stop(session.id, force=True)
                stopped += 1
            except ConflictError:
                logger.warning("Run 终态回收会话冲突 session=%s run=%s", session.id, key)
            except Exception:  # noqa: BLE001 - 回收单条失败不能拖垮其它会话
                logger.exception("Run 终态回收会话失败 session=%s run=%s", session.id, key)
        if stopped:
            logger.info("Run 终态已回收 %d 个终端会话 run=%s", stopped, key)
        return stopped

    async def recover_orphan_commands(self) -> int:
        """Flux 重启后回收上一进程遗留的命令进程（FINDING-2：SIGKILL 后 OS 进程不被回收）。

        逐个处理仍记录着 `cmd_pgid` 且未结束的会话：取该会话最后一条 `command.started`
        的命令文本，与 `/proc/<pgid>/cmdline` 的 argv 比对，**只在这条进程组确为本会话那条
        命令时**才强杀——pid/pgid 会被系统复用，比对 argv 才能避免误杀无关进程。命中后清空
        跟踪并标 closed、发 `session.closed`。

        安全护栏：pgid 若就是 Flux 自己所在进程组（`is_self_group`）绝不动手；Windows 无
        `/proc` 无法核实，只清跟踪并标 closed、不做 kill（宁可不清理，也不能误杀）。
        """
        sessions = await self._repo.list_sessions_with_cmd_pgid()
        reclaimed = 0
        for session in sessions:
            pgid = session.cmd_pgid
            if pgid is None:
                continue
            command = await self._last_command_text(session.id)
            if self._can_reclaim(pgid, command):
                with contextlib.suppress(Exception):
                    platforms.kill_group(pgid)
                reclaimed += 1
                logger.warning("重启回收孤儿命令进程 pgid=%s session=%s", pgid, session.id)
            else:
                logger.info("孤儿命令未被确认归属，仅清跟踪 pgid=%s session=%s", pgid, session.id)
            with contextlib.suppress(Exception):
                await self._repo.set_cmd_pgid(session.id, None)
            with contextlib.suppress(Exception):
                await self._emit(session.id, TerminalEventKind.SESSION_CLOSED, TerminalSource.USER)
            with contextlib.suppress(Exception):
                await self._repo.set_status(session.id, TerminalSessionStatus.CLOSED)
        if reclaimed:
            logger.warning("重启共回收 %d 个孤儿命令进程", reclaimed)
        return reclaimed

    async def _last_command_text(self, session_id: uuid.UUID) -> str | None:
        """取会话最后一条 `command.started` 的命令文本（回收孤儿进程前的归属核实依据）。"""
        events = await self._repo.list_events(session_id, after_seq=0)
        for event in reversed(events):
            if event.kind == TerminalEventKind.COMMAND_STARTED.value and event.command:
                return event.command
        return None

    @staticmethod
    def _can_reclaim(pgid: int, command: str | None) -> bool:
        """能否安全回收该进程组：确为上次遗留的那条命令，且不是 Flux 自身进程组。"""
        if not command:
            return False
        if platforms.IS_WINDOWS:  # 无 /proc 可核实，绝不冒险 kill 复用 id
            return False
        if platforms.is_self_group(pgid):
            return False
        if not platforms.is_group_alive(pgid):
            return False
        argv = _read_proc_cmdline(pgid)
        if argv is None:
            return False
        # shell=True 下 POSIX 进程组长是 `sh -c <command>`，command 必是其中一个 argv 元素，
        # 精确匹配（不做子串判断）以避免误伤。
        return command in argv

    @staticmethod
    def _terminate_tree(process: subprocess.Popen[str], *, force: bool) -> bool:
        """优雅信号 → grace → 强杀 → 确认进程树消失（平台原语，见 platforms）。"""
        return platforms.terminate_process_tree(
            process,
            force=force,
            grace=STOP_GRACE_SECONDS,
            confirm=KILL_CONFIRM_SECONDS,
        )

    async def shutdown(self) -> None:
        """进程退出前拒绝新任务、清理进程树，并把本进程留下的 active 会话收敛为 closed。"""
        if self._shutting_down:
            return
        self._shutting_down = True

        for session_id in list(self._processes):
            process = self._processes.get(session_id)
            if process is not None and process.poll() is None:
                terminated = await asyncio.to_thread(
                    self._terminate_tree,
                    process,
                    force=True,
                )
                if not terminated:
                    # Keep the process/session tracked. We must not manufacture a
                    # terminal.closed state while a process may still be alive.
                    continue

        tasks = [
            task for task in self._command_tasks.values() if task is not asyncio.current_task()
        ]
        if tasks:
            await asyncio.gather(
                *(asyncio.wait_for(asyncio.shield(task), timeout=10.0) for task in tasks),
                return_exceptions=True,
            )

        # By this point no new command can start. Only close sessions whose process
        # has actually gone away (or which never had a command process at all).
        sessions: list[TerminalSession] = []
        with contextlib.suppress(Exception):
            sessions = await self._repo.list_sessions(
                limit=10000,
                kind=TerminalSessionKind.AGENT,
                status=TerminalSessionStatus.ACTIVE,
            )
        for session in sessions:
            process = self._processes.get(str(session.id))
            if process is not None and process.poll() is None:
                continue
            with contextlib.suppress(Exception):
                await self._repo.set_status(session.id, TerminalSessionStatus.CLOSED)
            with contextlib.suppress(Exception):
                await self._emit(
                    session.id,
                    TerminalEventKind.SESSION_CLOSED,
                    TerminalSource.USER,
                )

        # Do not discard references to unconfirmed live processes: keeping them tracked
        # is preferable to falsely reporting a clean shutdown.
        self._processes = {
            key: process for key, process in self._processes.items() if process.poll() is None
        }
        self._command_tasks = {
            key: task for key, task in self._command_tasks.items() if not task.done()
        }
        self._stopping.clear()
        self._locks.clear()

    # --- 内部 ---

    def _resolve_root(self, override: str | Path | None) -> Path:
        return resolve_workspace_root(override if override is not None else self._configured_root)

    async def _emit(
        self,
        session_id: uuid.UUID,
        kind: TerminalEventKind,
        source: TerminalSource,
        *,
        command: str | None = None,
        chunk: str | None = None,
        exit_code: int | None = None,
        run_id: str | None = None,
        turn_id: str | None = None,
    ) -> TerminalEvent:
        # 入站脱敏（§10.1）：落库与广播共用同一份打码后的文本，杜绝"历史已打码、实时未打码"。
        # 打码是纯函数、零模型依赖；无密钥时只是换下来的原文不落映射表（fail-closed）。
        matches: list[RedactionMatch] = []
        if command is not None:
            command_result = redact(command)
            command = command_result.text
            matches.extend(command_result.matches)
        if chunk is not None:
            chunk_result = redact(chunk)
            chunk = chunk_result.text
            matches.extend(chunk_result.matches)
        async with self._emit_lock:
            event = await self._repo.append_event(
                session_id,
                kind=kind,
                source=source,
                command=command,
                chunk=chunk,
                exit_code=exit_code,
            )
        if self._bus is not None:
            await self._bus.publish(Events.TERMINAL_EVENT, event.to_dict())
        if matches and self._redactions is not None:
            # 映射留痕 best-effort：失败不影响事件已经落库/广播（无密钥时 record 返回 0）。
            with contextlib.suppress(Exception):
                await self._redactions.record(
                    run_id=run_id or str(session_id),
                    turn_id=turn_id or str(session_id),
                    matches=matches,
                )
        return event
