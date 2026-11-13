"""跨平台进程与文件原语（W-1 / P2-1，设计 §11.3）。

Flux 里所有平台差异只在这里出现：runtimes / supervisor / adapters / terminal 只表达意图
——把进程放进独立进程组、终止整棵进程树、探活、保护敏感文件——具体实现按宿主平台选择。

POSIX 侧是既有行为的收编（`setsid` 垫片、`killpg`、`kill(pid, 0)`、`chmod 0600`，行为不变）；
Windows 侧按设计 §11.3 给等价实现（`CREATE_NEW_PROCESS_GROUP`、`CTRL_BREAK_EVENT` →
`taskkill /T /F`、`OpenProcess` + `WaitForSingleObject` 只读探活、`icacls`）。

关键约束：**探活只读，绝不顺带终止**——Windows 上 `os.kill(pid, 0)` 语义接近
TerminateProcess，正是设计点名的"最危险的一处"。
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from flux.logging import get_logger

logger = get_logger(__name__)

#: 宿主是否为 Windows（唯一平台判定点，便于阅读与测试）
IS_WINDOWS = os.name == "nt"

#: POSIX 垫片：先 setsid() 再 execv，使子进程 pid == pgid == sid。
#: 直接 Popen 的进程与 Flux 同组，`killpg` 会连 uvicorn 一起杀。
_SETSID_SHIM = (
    "import os,sys\n"
    "try:\n"
    "    os.setsid()\n"
    "except OSError:\n"
    "    pass\n"
    "os.execv(sys.argv[1], sys.argv[1:])\n"
)

#: Windows 探活常量（避免顶层 import ctypes.windll，Linux 上也能导入本模块）
_WAIT_TIMEOUT = 0x00000102
_SYNCHRONIZE = 0x00100000


def launch_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """给要启动的命令套上"独立进程组"前提。

    POSIX 用 setsid 垫片（pid == pgid == sid，取消时能精准清理整棵进程树）；
    Windows 无 exec 语义，直接返回原 argv，进程组隔离由 `popen_kwargs()` 的
    `CREATE_NEW_PROCESS_GROUP` 提供。
    """
    if IS_WINDOWS:
        return tuple(argv)
    return (sys.executable, "-c", _SETSID_SHIM, *argv)


def popen_kwargs() -> dict[str, Any]:
    """`subprocess.Popen` 的平台参数：让新进程自成进程组，取消时不误伤 Flux 自身。"""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def executable_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """把 argv 规整成宿主能直接启动的形式（shell=False 前提）。

    Windows 上 CreateProcess 不能直接执行 .bat/.cmd（`WinError 193`），而 npm 全局安装的
    CLI（codex / opencode 等）在 Windows 上正是 `*.cmd` 包装脚本——被测/真实调用都会踩到。
    按系统 shell 的做法用 `cmd.exe /c <脚本> <原参数...>` 启动；进程组隔离仍由
    `popen_kwargs()` 的 CREATE_NEW_PROCESS_GROUP 提供，取消时 taskkill /T 能清整棵子树。
    POSIX 原样返回（脚本靠 shebang + 可执行位）。
    """
    if not IS_WINDOWS or not argv:
        return tuple(argv)
    executable = str(argv[0])
    if executable.lower().endswith((".cmd", ".bat")):
        # Windows 上 os.environ 的键不区分大小写；COMSPEC/SYSTEMROOT 是 cmd.exe 的定位依据
        comspec = os.environ.get("COMSPEC") or os.path.join(
            os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "cmd.exe"
        )
        return (comspec, "/c", *argv)
    return tuple(argv)


def process_group_of(process: subprocess.Popen[Any]) -> tuple[int, int]:
    """取 (pid, pgid)。Windows 无进程组查询，组长 pid 即组标识。"""
    pid = int(process.pid)
    if IS_WINDOWS:
        return pid, pid
    try:
        return pid, os.getpgid(pid)
    except OSError:  # 进程已退出 / 权限不足：退化成单进程清理
        return pid, pid


def is_alive(pid: int) -> bool:
    """进程是否仍存在（只读判定）。pid <= 0 视为不存在。"""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        return _windows_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_group_alive(pgid: int) -> bool:
    """进程组是否仍有成员（只读判定）。没有 pgid 概念的平台按组长进程判定。"""
    if pgid <= 0:
        return False
    if IS_WINDOWS:
        return _windows_is_alive(pgid)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True


def is_self_group(pgid: int) -> bool:
    """pgid 是否就是 Flux 自己所在的进程组（绝不能对它动手）。"""
    if IS_WINDOWS or pgid <= 0:
        return False
    try:
        return pgid == os.getpgrp()
    except OSError:  # 极少数平台不支持：保守起见按"是"处理，宁可不清理
        return True


def signal_group_graceful(pgid: int, *, owns_group: bool = False) -> None:
    """请求进程组优雅退出：POSIX SIGTERM；Windows CTRL_BREAK_EVENT。

    Windows 的 `CTRL_BREAK_EVENT` 只对**本进程用 CREATE_NEW_PROCESS_GROUP 创建的组**安全：
    GenerateConsoleCtrlEvent 要求 dwProcessGroupId 是本进程创建的组，否则行为不可预期——
    实测（CI）会把 Ctrl+Break 波及到共享控制台的宿主进程，把运行 Flux 的终端/pwsh 一起
    打断，正是设计 §11 要求避免的"误伤自身"。故 Windows 上默认**什么都不发**
    （owns_group=False，交由 kill_group 用 taskkill 强杀）；只有调用方确认该组确由本进程
    经 `popen_kwargs()` 创建时才传 owns_group=True。POSIX 侧 killpg(SIGTERM) 无此限制。
    """
    if pgid <= 0:
        return
    if IS_WINDOWS:
        if not owns_group:
            return
        with contextlib.suppress(OSError, ValueError, AttributeError):
            os.kill(pgid, signal.CTRL_BREAK_EVENT)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGTERM)


def signal_process_graceful(pid: int) -> None:
    """请求单个进程优雅退出。"""
    if pid <= 0:
        return
    if IS_WINDOWS:
        return  # Windows 无"温和信号"可发给非控制台进程，交给 kill_process 兜底
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)


def kill_group(pgid: int) -> None:
    """强制终止整个进程组：POSIX SIGKILL；Windows `taskkill /T /F`（含子树）。"""
    if pgid <= 0:
        return
    if IS_WINDOWS:
        _windows_taskkill(pgid)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def kill_process(pid: int) -> None:
    """强制终止单个进程。"""
    if pid <= 0:
        return
    if IS_WINDOWS:
        _windows_taskkill(pid)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


def terminate_process_tree(
    process: subprocess.Popen[Any],
    *,
    force: bool = False,
    grace: float = 5.0,
    confirm: float = 2.0,
    poll: float = 0.1,
) -> bool:
    """终止 Popen 代表的进程树（阻塞）：优雅信号 → grace → 强杀 → 确认消失。

    确认以 `process.poll()`（会回收僵尸子进程）为主、进程组探活为辅：只靠
    `killpg(pgid, 0)` 时，子进程已成僵尸但尚未 `wait()` 会一直"存在"，导致误判未清理。
    已退出则幂等返回 True。返回是否确认已清理。
    """
    if process.poll() is not None:
        return True
    pid, pgid = process_group_of(process)
    if not force:
        # process 由本进程经 popen_kwargs() 启动，组归属明确，Windows 上可安全用 CTRL_BREAK
        signal_group_graceful(pgid, owns_group=True)
        if _wait_process_gone(process, pid, pgid, grace, poll):
            return True
    kill_group(pgid)
    if _wait_process_gone(process, pid, pgid, confirm, poll):
        return True
    logger.warning("进程树未能确认退出 pid=%s pgid=%s", pid, pgid)
    return False


def secure_file(path: str | Path) -> bool:
    """把敏感文件收紧到"仅属主可访问"，返回是否成功收紧。

    POSIX：`chmod 0600`；Windows：`icacls /inheritance:r /grant:r <user>:F`。
    失败不抛异常——调用方据此记 warning 并把"文件权限弱化"如实告知用户（不静默）。
    """
    target = str(path)
    if IS_WINDOWS:
        user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        if not user:
            return False
        try:
            completed = subprocess.run(
                ["icacls", target, "/inheritance:r", "/grant:r", f"{user}:F"],
                capture_output=True,
                text=True,
                # 路径含中文时 icacls 输出不是 locale 可解码的：Windows 默认 cp1252 会在
                # _readerthread 抛 UnicodeDecodeError（仅告警但污染日志），显式钉 UTF-8。
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError:
            return False
        return completed.returncode == 0
    try:
        os.chmod(target, 0o600)
    except OSError:
        return False
    return True


# --- 内部 ---


def _wait_process_gone(
    process: subprocess.Popen[Any], pid: int, group: int, timeout: float, poll: float
) -> bool:
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        process.poll()  # 回收僵尸子进程，否则进程组探活会把僵尸算作"仍在"
        if not is_group_alive(group) and not is_alive(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(poll, 0.01))


def _windows_kernel32() -> Any:
    import ctypes  # noqa: PLC0415 - 仅 Windows 分支使用，避免顶层依赖

    return ctypes.WinDLL("kernel32", use_last_error=True)


def _windows_is_alive(pid: int) -> bool:
    """只读探活：能同步等待且尚未 signaled ⇒ 仍存活。绝不调用 TerminateProcess。"""
    kernel32 = _windows_kernel32()
    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        return False  # 打不开（已退出 / 无权限）→ 视为不存在
    try:
        return kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def _windows_taskkill(pid: int) -> None:
    with contextlib.suppress(OSError):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )


__all__ = [
    "IS_WINDOWS",
    "executable_argv",
    "is_alive",
    "is_group_alive",
    "is_self_group",
    "kill_group",
    "kill_process",
    "launch_argv",
    "popen_kwargs",
    "process_group_of",
    "secure_file",
    "signal_group_graceful",
    "signal_process_graceful",
    "terminate_process_tree",
]
