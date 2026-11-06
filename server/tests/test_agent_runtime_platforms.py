"""平台原语测试（W-1 / P2-1，设计 §11.3 / §11.5）。

同一套用例双平台跑：CI 的 ubuntu 与 windows 各执行一遍。覆盖 spawn（独立进程组）、
terminate_process_tree（含抗优雅信号的顽固进程、已退出进程）、is_alive / is_group_alive、
secure_file 的正常与边界。平台特有语义用 skipif 标注，避免在另一平台上报假失败。
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from flux.core.agent_runtime import platforms

POSIX_ONLY = pytest.mark.skipif(platforms.IS_WINDOWS, reason="POSIX 信号/权限语义")


def _spawn(snippet: str) -> subprocess.Popen[str]:
    """用平台原语起一个独立进程组的 python 子进程。"""
    return subprocess.Popen(
        platforms.launch_argv((sys.executable, "-c", snippet)),
        **platforms.popen_kwargs(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def test_launch_argv_adds_group_shim_only_on_posix() -> None:
    argv = platforms.launch_argv(("some-agent", "--flag"))
    if platforms.IS_WINDOWS:
        # Windows 无 exec 语义：直接 argv，进程组隔离交给 popen_kwargs
        assert argv == ("some-agent", "--flag")
    else:
        # POSIX：套 setsid 垫片，垫片之后再 exec 原命令
        assert argv[0] == sys.executable
        assert argv[1] == "-c"
        assert argv[3:] == ("some-agent", "--flag")


def test_popen_kwargs_isolates_process_group() -> None:
    kwargs = platforms.popen_kwargs()
    if platforms.IS_WINDOWS:
        assert kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kwargs == {"start_new_session": True}


def test_is_alive_rejects_invalid_and_missing_pids() -> None:
    assert platforms.is_alive(0) is False
    assert platforms.is_alive(-1) is False
    assert platforms.is_alive(999_999_999) is False
    assert platforms.is_group_alive(0) is False


def test_running_process_is_alive_and_reports_its_group() -> None:
    process = _spawn("import time; time.sleep(30)")
    try:
        pid, pgid = platforms.process_group_of(process)
        assert pid == process.pid
        assert pgid > 0
        assert platforms.is_alive(pid) is True
        assert platforms.is_group_alive(pgid) is True
        if not platforms.IS_WINDOWS:
            # POSIX：垫片保证 pid == pgid == sid，取消时能精准清理整棵树
            assert pgid == pid
    finally:
        platforms.terminate_process_tree(process, force=True, confirm=3.0)
        process.wait(timeout=5)


def test_terminate_process_tree_kills_running_process() -> None:
    process = _spawn("import time; time.sleep(30)")
    pid, pgid = platforms.process_group_of(process)
    assert platforms.terminate_process_tree(process, grace=1.0, confirm=3.0) is True
    assert process.poll() is not None
    assert platforms.is_alive(pid) is False
    assert platforms.is_group_alive(pgid) is False


@POSIX_ONLY
def test_terminate_process_tree_escalates_for_stubborn_process() -> None:
    """子进程忽略 SIGTERM：优雅期过后必须升级强杀，并确认已清理（设计 §3.4）。"""
    process = _spawn(
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    )
    assert platforms.terminate_process_tree(process, grace=0.5, confirm=3.0) is True
    assert process.poll() is not None


def test_terminate_process_tree_is_idempotent_for_exited_process() -> None:
    process = _spawn("pass")
    process.wait(timeout=10)
    # 已退出：幂等返回 True，不抛异常
    assert platforms.terminate_process_tree(process) is True
    assert platforms.terminate_process_tree(process, force=True) is True


@POSIX_ONLY
def test_is_self_group_detects_flux_own_group() -> None:
    assert platforms.is_self_group(os.getpgrp()) is True
    assert platforms.is_self_group(0) is False


def test_secure_file_tightens_sensitive_file(tmp_path) -> None:
    path = tmp_path / "run-config.yaml"
    path.write_text("token: redacted\n", encoding="utf-8")
    assert platforms.secure_file(path) is True
    if not platforms.IS_WINDOWS:
        assert (path.stat().st_mode & 0o777) == 0o600


def test_secure_file_missing_path_reports_failure(tmp_path) -> None:
    # 收权失败必须如实返回 False（调用方据此记 warning），不抛异常
    assert platforms.secure_file(tmp_path / "not-there.yaml") is False
