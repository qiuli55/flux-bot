"""日志系统（主规格 §15.3 日志级别：DEBUG / INFO / WARNING / ERROR）。"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s [%(filename)s:%(lineno)d] %(message)s"

#: 本次进程的启动时刻（UTC）与日志文件。供 flux.cloud.uploader 在下次启动时
#: 定位「上一轮运行日志」——所以必须是模块级状态，而不是函数局部变量。
_run_started_at: datetime | None = None
_run_log_file: Path | None = None


def configure_logging(level: str = "INFO", log_dir: str | Path | None = None) -> None:
    """初始化根 logger。重复调用安全（幂等）。

    `log_dir` 非空时额外落一份文件日志 `run-<启动时间>.log`：Flux 的日志上传是
    「自动」的——下次启动时由 flux.cloud.uploader 把上一轮这份文件传给账号云，
    不需要人工打包。留空则只输出到 stdout（测试与本地零配置）。
    """
    global _run_started_at, _run_log_file

    normalized = (level or "INFO").upper()
    if normalized not in _VALID_LEVELS:
        normalized = "INFO"

    formatter = logging.Formatter(_FORMAT)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    _run_started_at = datetime.now(timezone.utc)
    _run_log_file = None
    if log_dir is not None:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        _run_log_file = directory / f"run-{_run_started_at.strftime('%Y%m%dT%H%M%SZ')}.log"
        handlers.append(logging.FileHandler(_run_log_file, encoding="utf-8"))

    for handler in handlers:
        handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(normalized)

    # 降低三方库噪声，保留自身 INFO 级别
    logging.getLogger("uvicorn.access").setLevel("WARNING")


def run_started_at() -> datetime | None:
    """本次进程的启动时刻（UTC）；`configure_logging` 之前为 None。"""
    return _run_started_at


def run_log_file() -> Path | None:
    """本次进程的日志文件；未启用落盘时为 None。"""
    return _run_log_file


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
