"""Project Scanner 测试（主规格 §5.8；实施计划 ⑩）。

全部在**真实临时目录**上跑，不 mock 文件系统；Git 相关用例建真实仓库并跑真 git。
画像的价值就在于"与磁盘事实一致"，用假数据测等于没测。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from flux.core.git_integration.client import GitClient
from flux.core.project_scanner.scanner import ProjectScanner, parse_makefile_targets
from flux.errors import ValidationError

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Flux Test",
    "GIT_AUTHOR_EMAIL": "flux-test@example.com",
    "GIT_COMMITTER_NAME": "Flux Test",
    "GIT_COMMITTER_EMAIL": "flux-test@example.com",
}


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **GIT_ENV},
    )
    return completed.stdout


def _write(root: Path, relative: str, content: str = "") -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _python_project(tmp_path: Path) -> Path:
    root = tmp_path / "demo-service"
    _write(
        root,
        "pyproject.toml",
        "\n".join(
            [
                "[tool.poetry]",
                'name = "demo-service"',
                "",
                "[tool.pytest.ini_options]",
                'testpaths = ["tests"]',
                "",
                "[build-system]",
                'requires = ["poetry-core"]',
            ]
        ),
    )
    _write(root, "requirements.txt", "fastapi\nsqlalchemy\n")
    _write(root, "README.md", "# demo-service\n")
    _write(root, "Dockerfile", "FROM python:3.10-slim\n")
    _write(root, "Makefile", "test:\n\tpytest\n\nbuild:\n\tpython -m build\n")
    _write(root, "src/app/main.py", "def main():\n    return 0\n")
    _write(root, "src/app/__init__.py", "")
    _write(root, "tests/test_main.py", "def test_main():\n    assert True\n")
    # 噪声目录：既不进语言统计，也不出现在顶层结构里
    _write(root, ".venv/lib/site-packages/ignored.py", "")
    _write(root, "node_modules/pkg/index.js", "")
    _write(root, "__pycache__/main.cpython-310.pyc", "")
    _write(root, ".flux/backups/old.py", "")
    return root


# --- Python 项目画像 ---


def test_scan_python_project_profile(tmp_path: Path) -> None:
    root = _python_project(tmp_path)

    profile = ProjectScanner().scan(workspace_root=root)

    assert profile.root == str(root.resolve())
    assert profile.languages == ("Python",)
    assert profile.primary_language == "Python"
    assert profile.package_manager == "poetry"
    assert profile.frameworks == ("FastAPI", "SQLAlchemy")
    # 顺序：先生态系统命令，再 Makefile 目标（去重后保持稳定）
    assert profile.test_commands == ("pytest", "make test")
    assert profile.build_commands == (
        "python -m build",
        "make build",
        "docker build -t demo-service .",
    )
    assert "src/app/main.py" in profile.entry_points
    assert profile.manifests == (
        "Dockerfile",
        "Makefile",
        "README.md",
        "pyproject.toml",
        "requirements.txt",
    )
    assert profile.truncated is False


def test_scan_ignores_noise_directories(tmp_path: Path) -> None:
    root = _python_project(tmp_path)

    profile = ProjectScanner().scan(workspace_root=root)

    assert "Python" in profile.languages
    # .venv / node_modules / __pycache__ / .flux 下的 .py/.js 一律不计入
    assert profile.files_scanned == 8
    assert profile.structure == (
        "src/",
        "tests/",
        "Dockerfile",
        "Makefile",
        "README.md",
        "pyproject.toml",
        "requirements.txt",
    )


def test_scan_structure_lists_directories_with_slash(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    _write(root, "docs/guide.md")
    _write(root, "app.py")

    profile = ProjectScanner().scan(workspace_root=root)

    assert profile.structure == ("docs/", "app.py")


# --- Node 项目画像 ---


def test_scan_node_project_prefers_lockfile_manager(tmp_path: Path) -> None:
    root = tmp_path / "web-app"
    _write(
        root,
        "package.json",
        """
        {
          "name": "web-app",
          "main": "src/index.js",
          "scripts": {"test": "vitest run", "build": "vite build"},
          "dependencies": {"react": "^18.3.1", "express": "^4.19.2"},
          "devDependencies": {"vite": "^5.4.0", "typescript": "^5.6.0"}
        }
        """,
    )
    _write(root, "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
    _write(root, "src/index.js", "console.log('hi')\n")
    _write(root, "vite.config.ts", "export default {}\n")

    profile = ProjectScanner().scan(workspace_root=root)

    assert profile.package_manager == "pnpm"
    assert set(profile.frameworks) == {"React", "Express", "Vite", "TypeScript"}
    assert profile.test_commands == ("pnpm run test",)
    assert profile.build_commands == ("pnpm run build",)
    # main 字段与文件名探测指向同一个文件，去重后只留一条
    assert profile.entry_points == ("src/index.js",)


def test_scan_node_without_lockfile_defaults_to_npm(tmp_path: Path) -> None:
    root = tmp_path / "no-lock"
    _write(root, "package.json", '{"name": "no-lock", "dependencies": {"vue": "^3.5.0"}}')
    _write(root, "src/index.js", "")

    profile = ProjectScanner().scan(workspace_root=root)

    assert profile.package_manager == "npm"
    assert "Vue" in profile.frameworks


# --- 上限与裁剪 ---


def test_scan_marks_truncated_when_file_cap_hit(tmp_path: Path) -> None:
    root = tmp_path / "big"
    for index in range(10):
        _write(root, f"pkg/mod_{index}.py", "")

    profile = ProjectScanner(max_files=3).scan(workspace_root=root)

    assert profile.files_scanned == 3
    assert profile.truncated is True


def test_scan_marks_truncated_when_depth_cap_hit(tmp_path: Path) -> None:
    root = tmp_path / "deep"
    _write(root, "a/b/c/d/deep.py", "")

    profile = ProjectScanner(max_depth=1).scan(workspace_root=root)

    assert profile.truncated is True
    assert profile.files_scanned == 0


# --- Git ---


def test_scan_reports_git_repository_and_branch(tmp_path: Path) -> None:
    root = _python_project(tmp_path)
    _git(root, "init", "-b", "main")
    _git(root, "add", "pyproject.toml")
    _git(root, "commit", "-m", "chore: 初始化")

    profile = ProjectScanner(git=GitClient(workspace_root=root)).scan(workspace_root=root)

    assert profile.git_repository is True
    assert profile.git_branch == "main"


def test_scan_reports_non_repository_as_false(tmp_path: Path) -> None:
    root = _python_project(tmp_path)

    profile = ProjectScanner(git=GitClient(workspace_root=root)).scan(workspace_root=root)

    assert profile.git_repository is False
    assert profile.git_branch is None


def test_scan_without_git_client_detects_dot_git(tmp_path: Path) -> None:
    """没接 GitClient 时退化为"看有没有 .git"，不自己去猜分支。"""
    root = _python_project(tmp_path)
    (root / ".git").mkdir()

    profile = ProjectScanner().scan(workspace_root=root)

    assert profile.git_repository is True
    assert profile.git_branch is None


# --- 边界 ---


def test_scan_requires_workspace_root() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ProjectScanner().scan()
    assert "未配置工作区根目录" in excinfo.value.message


def test_scan_rejects_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        ProjectScanner().scan(workspace_root=tmp_path / "不存在")


def test_scan_empty_directory_yields_empty_profile(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()

    profile = ProjectScanner().scan(workspace_root=root)

    assert profile.languages == ()
    assert profile.primary_language is None
    assert profile.package_manager is None
    assert profile.frameworks == ()
    assert profile.manifests == ()
    assert profile.entry_points == ()
    assert profile.test_commands == ()
    assert profile.build_commands == ()
    assert profile.structure == ()


def test_scan_profile_serializes_to_dict(tmp_path: Path) -> None:
    profile = ProjectScanner().scan(workspace_root=_python_project(tmp_path))

    payload = profile.to_dict()

    assert payload["primary_language"] == "Python"
    assert isinstance(payload["languages"], list)
    assert payload["test_commands"] == ["pytest", "make test"]


def test_parse_makefile_targets_skips_recipes_and_phony() -> None:
    text = ".PHONY: test\nall: build\n\ttest:\n\nbuild:\n\tpython -m build\ntest:\n\tpytest\n"

    assert parse_makefile_targets(text) == {"all", "build", "test"}
