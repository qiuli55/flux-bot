"""提案预演测试（自检闭环）：改动落进临时副本，测试结果才算数。

三条底线：

1. 预演测的是**提案后**的代码；真实工作区在预演结束后一个字节都不变；
2. 依赖与构建产物不复制（否则每次自检都要搬几百 MB），依赖目录以软链挂回；
3. 无论测试通过与否，临时副本都被清理；未配置测试命令时明确报错，不静默跳过。
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from flux.config import Settings
from flux.container import Container
from flux.core.mcp.auth import AgentIdentity
from flux.core.mcp.tools.base import ToolContext
from flux.core.mcp.tools.preview_tools import proposal_preview
from flux.core.virtual_workspace.preview import (
    _copy_workspace,
    _link_dependency_dirs,
    run_preview,
)
from flux.core.virtual_workspace.proposal_parser import FileChange
from flux.enums import Capability
from flux.errors import ValidationError

from .conftest import python_command

#: 预演命令：副本里的 calc.py 内容为 ok 才算通过（只依赖标准库，跨平台一致）
_CHECK_OK = python_command(
    "import pathlib, sys; sys.exit(0 if pathlib.Path('calc.py').read_text().strip() == 'ok' else 1)"
)


def _settings(tmp: Path, *, test_command: str | None = _CHECK_OK) -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        database_url=f"sqlite+aiosqlite:///{tmp / 'preview.db'}",
        openai_api_key=None,
        anthropic_api_key=None,
        deepseek_api_key=None,
        default_provider="local",
        workspace_root=str(tmp / "project"),
        test_command=test_command,
    )


def _project(tmp: Path) -> Path:
    root = tmp / "project"
    root.mkdir(parents=True)
    (root / "calc.py").write_text("bad\n", encoding="utf-8")
    return root


def _ctx(settings: Settings) -> ToolContext:
    return ToolContext(
        container=Container(settings),
        identity=AgentIdentity(
            agent_id=str(uuid.uuid4()),
            token_id="tok-preview",
            scopes=frozenset({Capability.TERMINAL_EXECUTE}),
        ),
        tool_name="proposal.preview",
    )


@pytest.fixture()
def sandboxes(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Path]]:
    """记录每次预演建的临时目录，供断言"确实删掉了"。"""
    created: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def fake_mkdtemp(*, prefix: str) -> str:
        path = Path(real_mkdtemp(prefix=prefix))
        created.append(path)
        return str(path)

    monkeypatch.setattr("flux.core.virtual_workspace.preview.tempfile.mkdtemp", fake_mkdtemp)
    yield created


async def test_preview_tests_the_proposed_code_and_leaves_the_workspace_untouched(
    tmp_path: Path, sandboxes: list[Path]
) -> None:
    """真实工作区是旧代码（测试本会失败），预演应用提案后必须通过，且不写真实文件。"""
    root = _project(tmp_path)
    ctx = _ctx(_settings(tmp_path))

    payload = {
        "summary": "把 calc 改对",
        "changes": [{"path": "calc.py", "content": "ok\n"}],
    }

    result = await proposal_preview(ctx, {"payload": payload})

    assert result["passed"] is True
    assert result["exit_code"] == 0
    assert "proposal.create" in result["note"]
    assert (root / "calc.py").read_text(encoding="utf-8") == "bad\n"
    assert len(sandboxes) == 1 and not sandboxes[0].exists()


async def test_preview_accepts_task_and_project_ids_from_execution_prompt(
    tmp_path: Path, sandboxes: list[Path]
) -> None:
    """平台执行指令会让 Agent 一并带上 task_id/project_id；预演不能因此整次失败。"""
    root = _project(tmp_path)
    ctx = _ctx(_settings(tmp_path))

    result = await proposal_preview(
        ctx,
        {
            "payload": {"changes": [{"path": "calc.py", "content": "ok\n"}]},
            "task_id": str(uuid.uuid4()),
            "project_id": str(uuid.uuid4()),
        },
    )

    assert result["passed"] is True
    assert (root / "calc.py").read_text(encoding="utf-8") == "bad\n"
    assert not sandboxes[0].exists()


async def test_preview_reports_failure_with_tail_output(
    tmp_path: Path, sandboxes: list[Path]
) -> None:
    """提案没把测试改绿时，返回失败与输出，而不是误报通过。"""
    root = _project(tmp_path)
    ctx = _ctx(_settings(tmp_path))

    result = await proposal_preview(
        ctx,
        {"payload": {"changes": [{"path": "calc.py", "content": "still-bad\n"}]}},
    )

    assert result["passed"] is False
    assert result["exit_code"] == 1
    assert result["output_truncated"] is False
    assert (root / "calc.py").read_text(encoding="utf-8") == "bad\n"
    assert not sandboxes[0].exists()


def test_preview_supports_delete_and_create(tmp_path: Path) -> None:
    """delete 在副本里移除文件、create 补上新文件；真实工作区保持原样。"""
    root = _project(tmp_path)
    check = python_command(
        "import pathlib, sys; "
        "sys.exit(0 if not pathlib.Path('calc.py').exists() "
        "and pathlib.Path('extra.py').read_text().strip() == 'new' else 1)"
    )

    result = run_preview(
        workspace_root=str(root),
        changes=(
            # 直接用解析器产出的结构，覆盖 op 的两种显式形态
            FileChange(path="calc.py", content=None, op="delete"),
            FileChange(path="extra.py", content="new\n", op="create"),
        ),
        test_command=check,
        timeout_seconds=60,
    )

    assert result.test.passed is True
    assert (root / "calc.py").exists()
    assert not (root / "extra.py").exists()


def test_preview_without_test_command_is_rejected(tmp_path: Path) -> None:
    """未配置测试命令时明确拒绝：预演没有任何可跑的判据，不能假装通过。"""
    _project(tmp_path)
    with pytest.raises(ValidationError):
        run_preview(
            workspace_root=str(tmp_path / "project"),
            changes=(FileChange(path="calc.py", content="ok\n"),),
            test_command=None,
        )


def test_copy_excludes_dependencies_and_links_them_back(tmp_path: Path) -> None:
    """依赖与构建产物不复制，依赖目录改以软链挂回，避免每次自检搬运大目录。"""
    src = tmp_path / "src"
    (src / "node_modules" / "pkg").mkdir(parents=True)
    (src / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
    (src / "build").mkdir()
    (src / "build" / "out.bin").write_text("x", encoding="utf-8")
    (src / ".git").mkdir()
    (src / ".git" / "config").write_text("x", encoding="utf-8")
    (src / "calc.py").write_text("ok\n", encoding="utf-8")
    dst = tmp_path / "dst"
    dst.mkdir()

    _copy_workspace(src, dst)
    _link_dependency_dirs(src, dst)

    assert (dst / "calc.py").is_file()
    assert not (dst / "build").exists()
    assert not (dst / ".git").exists()
    assert (dst / "node_modules").is_symlink()
    assert (dst / "node_modules" / "pkg" / "index.js").is_file()
