"""提案预演：在临时副本里落改动并跑项目配置的测试命令（自检闭环）。

为什么需要它：Flux 的提案是**虚拟**的，真实工作区在 Apply 之前仍是旧代码。
Agent 若直接 `terminal.execute` 跑测试，测到的是改动前的代码，绿了也不算数。
这里把同一份提案内容应用到工作区的临时副本，再跑 `FLUX_TEST_COMMAND`——
测的是"提案之后的世界"，且全程不碰用户真实文件。

边界：

- 测试命令仍然只来自配置（与 Apply / Tester 同一口径），Agent 无权指定；
- 副本排除依赖与构建产物（`node_modules` / `.venv` / `build` 等），依赖目录以
  软链挂回，避免每次自检都复制几百 MB；
- 副本无论成败都会被删除（`finally`），失败清理只记日志、不掩盖测试结论。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from flux.core.virtual_workspace.apply_engine import resolve_workspace_root, safe_relative_path
from flux.core.virtual_workspace.path_guard import resolve_within_root
from flux.core.virtual_workspace.proposal_parser import FileChange
from flux.core.virtual_workspace.test_runner import TestOutcome, TestRunner
from flux.errors import ValidationError
from flux.logging import get_logger

logger = get_logger(__name__)

#: 副本里不复制的内容：版本库元数据、依赖、缓存与构建产物。
#: 复制它们既慢又没意义——测试需要的是源码与配置，依赖用软链挂回。
COPY_IGNORES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".flux",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        ".gradle",
        ".next",
        "build",
        "dist",
    }
)

#: 以软链挂回副本的依赖目录（不复制，直接复用原目录）。
LINK_DIRS: frozenset[str] = frozenset({"node_modules", ".venv", "venv"})

#: 回给 Agent 的输出摘要上限；与 terminal.execute 保持一致，取尾部。
MAX_SUMMARY_CHARS = 4000


@dataclass(frozen=True)
class PreviewResult:
    """一次预演的结论：测试是否通过 + 可回灌的尾部输出。"""

    test: TestOutcome

    def to_dict(self) -> dict[str, object]:
        output = self.test.output
        truncated = len(output) > MAX_SUMMARY_CHARS
        return {
            "passed": self.test.passed,
            "exit_code": self.test.exit_code,
            "timed_out": self.test.timed_out,
            "duration_ms": self.test.duration_ms,
            "command": self.test.command,
            "output_summary": output[-MAX_SUMMARY_CHARS:] if truncated else output,
            "output_truncated": truncated,
            "note": (
                "预演在临时副本里执行，真实工作区未被改动；"
                "passed 只代表该提案下测试命令通过，不代表人审通过。"
                "本次预演没有创建任何提案：要让人审队列看到这份改动，"
                "必须在预演通过后调用 proposal.create 提交同一份 payload。"
            ),
        }


def run_preview(
    *,
    workspace_root: str | Path | None,
    changes: Sequence[FileChange],
    test_command: str | None,
    timeout_seconds: float = 300.0,
) -> PreviewResult:
    """把 changes 应用到临时副本并跑一次测试命令；异常路径也会清理副本。"""
    root = resolve_workspace_root(workspace_root)
    command = (test_command or "").strip()
    if not command:
        raise ValidationError(
            "未配置测试命令（FLUX_TEST_COMMAND），无法预演",
            details={"hint": "设置 FLUX_TEST_COMMAND 后重试"},
        )
    sandbox = _make_sandbox()
    logger.info(
        "preview.start root=%s files=%s command=%s",
        root,
        [change.path for change in changes],
        command,
    )
    try:
        _copy_workspace(root, sandbox)
        _link_dependency_dirs(root, sandbox)
        _apply_changes(sandbox, changes)
        test = TestRunner(timeout_seconds=timeout_seconds).run(command, cwd=sandbox)
        logger.info(
            "preview.done files=%s passed=%s exit=%d",
            [change.path for change in changes],
            test.passed,
            test.exit_code,
        )
        return PreviewResult(test=test)
    except Exception:
        # 预演失败（复制/落改动/启动命令）必须留痕，否则 Agent 只看到一句错误，
        # 无从判断是自己提案的问题还是平台环境的问题
        logger.exception("preview.failed files=%s", [change.path for change in changes])
        raise
    finally:
        _cleanup(sandbox)


def _make_sandbox() -> Path:
    """建一个权限收紧的临时目录；放在系统临时目录，生命周期只在本次预演内。"""
    return Path(tempfile.mkdtemp(prefix="flux-preview-"))


def _copy_workspace(src: Path, dst: Path) -> None:
    """复制源码树（排除依赖与构建产物）；保留软链原样，避免把根外内容复制进来。"""
    shutil.copytree(src, dst, symlinks=True, ignore=_ignore, dirs_exist_ok=True)


def _ignore(directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in COPY_IGNORES}


def _link_dependency_dirs(src: Path, dst: Path) -> None:
    """把依赖目录软链回副本对应的相对位置。

    复制依赖动辄几百 MB，而测试只需要"能 import 到"；软链让 `node_modules` 与
    项目内虚拟环境在副本里照常可用。遍历时遇到被排除的目录就剪枝，不会走进依赖树。
    """
    for current, dirs, _ in os.walk(src):
        rel = Path(current).relative_to(src)
        for name in list(dirs):
            if name in LINK_DIRS:
                link = dst / rel / name
                link.parent.mkdir(parents=True, exist_ok=True)
                if not link.is_symlink():
                    link.symlink_to(Path(current) / name, target_is_directory=True)
            if name in COPY_IGNORES:
                dirs.remove(name)


def _apply_changes(root: Path, changes: Sequence[FileChange]) -> None:
    """把提案内容写进副本；口径与 Apply 一致：字节级写入，delete 即移除文件。"""
    for change in changes:
        target = resolve_within_root(root, safe_relative_path(change.path))
        if change.op == "delete" or change.content is None:
            if target.is_file():
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # 与 ApplyEngine._write 同为字节级写入：避免 Windows 上把 LF 翻成 CRLF
        target.write_bytes(change.content.encode("utf-8"))


def _cleanup(sandbox: Path) -> None:
    try:
        shutil.rmtree(sandbox)
    except OSError as exc:  # 清理失败不能掩盖测试结论，但必须留痕
        logger.warning("preview.cleanup_failed sandbox=%s error=%s", sandbox, exc)
