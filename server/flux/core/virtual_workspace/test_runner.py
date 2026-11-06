"""Test Runner（主规格 §7.6 的"运行测试"一步；实施计划 ⑦）。

跑的是**项目自己配置的测试命令**（如 `pytest -q`），不是 Agent 临时编出来的命令——
命令来源只有配置，Agent 无权指定，避免把提权入口开在 AI 侧。
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flux.logging import get_logger

logger = get_logger(__name__)

MAX_OUTPUT_CHARS = 20000

# 截断标记：中间省略了多少字符
_TRUNCATION_MARKER = "\n…（输出已截断，中间省略 {omitted} 字符）\n"


@dataclass(frozen=True)
class TestOutcome:
    command: str
    exit_code: int
    output: str
    timed_out: bool
    duration_ms: int

    @property
    def passed(self) -> bool:
        return not self.timed_out and self.exit_code == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "output": self.output,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "passed": self.passed,
        }


class TestRunner:
    # 名字以 Test 开头会被 pytest 当成测试类收集，这里显式声明它不是测试
    __test__ = False

    def __init__(self, *, timeout_seconds: float = 300.0) -> None:
        self._timeout = timeout_seconds

    def run(self, command: str, *, cwd: Path) -> TestOutcome:
        started = time.monotonic()
        try:
            completed = subprocess.run(  # noqa: S602 - 命令来自项目配置，不是模型输出
                command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                # 测试输出常含中文（断言失败信息）。Windows 文本管道默认按 locale
                # 解码，遇到 UTF-8 汉字会抛 UnicodeDecodeError 把整条门禁链路打断。
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return TestOutcome(
                command=command,
                exit_code=-1,
                output=self._truncate(f"测试命令超时（{self._timeout}s）：{exc}"),
                timed_out=True,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        output = self._truncate((completed.stdout or "") + (completed.stderr or ""))
        outcome = TestOutcome(
            command=command,
            exit_code=completed.returncode,
            output=output,
            timed_out=False,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        logger.info(
            "apply.test command=%s exit=%d passed=%s",
            command,
            outcome.exit_code,
            outcome.passed,
        )
        return outcome

    @staticmethod
    def _truncate(text: str) -> str:
        """超长输出保留「头 + 尾」，中间省略。

        测试汇总行（如 `12 passed, 2 failed in 0.53s`）永远在输出末尾，只截掉尾部
        会让 Tester 拿不到用例统计（⑧ 要求回报 Tests: N passed），所以尾部必须保留。
        """
        if len(text) <= MAX_OUTPUT_CHARS:
            return text
        # 先按最坏情况估算标记长度，保证最终长度不超上限
        marker_len = len(_TRUNCATION_MARKER.format(omitted=len(text)))
        keep = max(MAX_OUTPUT_CHARS - marker_len, 0)
        head = keep // 2
        tail = keep - head
        marker = _TRUNCATION_MARKER.format(omitted=len(text) - keep)
        return text[:head] + marker + (text[-tail:] if tail else "")
