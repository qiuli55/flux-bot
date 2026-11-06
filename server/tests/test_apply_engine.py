"""Apply Engine 测试（主规格 §7.6；实施计划 ⑦）。

这是 Flux 里唯一会写用户真实文件的组件，因此每条用例都在**真实临时目录**上跑，
并且都断言"失败之后用户原文件必须回到原样"——回滚不是可选项。
"""

from __future__ import annotations

import os
import subprocess
import threading
import uuid
from pathlib import Path

import pytest

from flux.core.virtual_workspace.apply_engine import ApplyEngine, safe_relative_path
from flux.core.virtual_workspace.backup import BackupService
from flux.core.virtual_workspace.diff_engine import content_hash
from flux.core.virtual_workspace.test_runner import TestRunner
from flux.errors import ApplyFailedError, ConflictError, ValidationError
from flux.models.workspace import VirtualChange
from tests.conftest import python_command

ORIGINAL = "def login(user):\n    return False\n"
PROPOSED = "def login(user):\n    return check_password(user)\n"


def _change(
    file_path: str,
    *,
    original: str = ORIGINAL,
    proposed: str = PROPOSED,
) -> VirtualChange:
    """构造一条"已批准"的提案。id 显式给值：脱离会话的实例拿不到 column default。"""
    return VirtualChange(
        id=uuid.uuid4(),
        file_path=file_path,
        original_hash=content_hash(original),
        original_content=original,
        proposed_content=proposed,
        status="accepted",
    )


def _write(root: Path, relative: str, content: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


# --- 正常落盘 ---


def test_apply_writes_proposed_content_and_backs_up_original(workspace_root: Path) -> None:
    target = _write(workspace_root, "auth/login.py", ORIGINAL)
    change = _change("auth/login.py")

    outcome = ApplyEngine(workspace_root=workspace_root).apply(change, run_tests=False)

    assert target.read_text(encoding="utf-8") == PROPOSED
    assert outcome.change_id == str(change.id)
    assert outcome.file_path == "auth/login.py"
    assert outcome.test is None
    backup = Path(outcome.backup_path)
    assert backup.is_file()
    # 备份落在工作区根下的 .flux/backups/<change_id>/<相对路径>，内容为改动前的原文
    assert backup == workspace_root / ".flux" / "backups" / str(change.id) / "auth" / "login.py"
    assert backup.read_text(encoding="utf-8") == ORIGINAL


def test_apply_creates_new_file_without_backup(workspace_root: Path) -> None:
    """新建文件：原文为空串 + 磁盘上不存在，属于合法提案，且没有可备份的内容。"""
    change = _change("app/health.py", original="", proposed="def health():\n    return 'ok'\n")

    outcome = ApplyEngine(workspace_root=workspace_root).apply(change, run_tests=False)

    created = workspace_root / "app" / "health.py"
    assert created.read_text(encoding="utf-8") == "def health():\n    return 'ok'\n"
    assert outcome.backup_path is None


def test_apply_normalizes_dotted_and_backslashed_paths(workspace_root: Path) -> None:
    _write(workspace_root, "app/main.py", ORIGINAL)
    change = _change("./app\\main.py")

    ApplyEngine(workspace_root=workspace_root).apply(change, run_tests=False)

    assert (workspace_root / "app" / "main.py").read_text(encoding="utf-8") == PROPOSED
    # 不能因为路径写法不同就在工作区里留下 ./app 这样的怪目录
    assert sorted(p.name for p in workspace_root.iterdir()) == [".flux", "app"]


def test_apply_uses_override_root(workspace_root: Path, tmp_path: Path) -> None:
    """显式传入的 workspace_root 优先于构造时的配置。"""
    other = tmp_path / "other-project"
    other.mkdir()
    _write(other, "a.py", ORIGINAL)

    engine = ApplyEngine(workspace_root=workspace_root)
    engine.apply(_change("a.py"), workspace_root=other, run_tests=False)

    assert (other / "a.py").read_text(encoding="utf-8") == PROPOSED
    assert not (workspace_root / "a.py").exists()


# --- 预检失败（不落盘，不动原文件）---


def test_apply_conflicts_when_user_changed_the_file(workspace_root: Path) -> None:
    """Agent 出提案后用户又改了同一文件 → 拒绝落盘，用户的改动必须原样保留。"""
    user_version = "def login(user):\n    return user.is_active\n"
    target = _write(workspace_root, "auth/login.py", user_version)

    with pytest.raises(ConflictError) as excinfo:
        ApplyEngine(workspace_root=workspace_root).apply(_change("auth/login.py"), run_tests=False)

    assert "已被改动" in excinfo.value.message
    assert excinfo.value.details["expected_hash"] == content_hash(ORIGINAL)
    assert excinfo.value.details["actual_hash"] == content_hash(user_version)
    assert target.read_text(encoding="utf-8") == user_version
    assert not (workspace_root / ".flux").exists()


def test_apply_conflicts_when_file_was_deleted(workspace_root: Path) -> None:
    """提案基于"有原文"的文件，但文件已不在 → 冲突，而不是当成新建文件覆盖。"""
    with pytest.raises(ConflictError) as excinfo:
        ApplyEngine(workspace_root=workspace_root).apply(_change("auth/login.py"), run_tests=False)

    assert "已不存在" in excinfo.value.message
    assert not (workspace_root / "auth" / "login.py").exists()


def test_apply_rejects_directory_as_target(workspace_root: Path) -> None:
    (workspace_root / "pkg").mkdir()

    with pytest.raises(ConflictError) as excinfo:
        ApplyEngine(workspace_root=workspace_root).apply(_change("pkg"), run_tests=False)

    assert "不是普通文件" in excinfo.value.message


def test_apply_without_workspace_root_is_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ApplyEngine().apply(_change("a.py"), run_tests=False)
    assert "未配置工作区根目录" in excinfo.value.message


def test_apply_rejects_missing_workspace_root(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ApplyEngine(workspace_root=tmp_path / "不存在").apply(_change("a.py"), run_tests=False)
    assert "不存在或不是目录" in excinfo.value.message


def test_safe_relative_path_rejects_escaping_paths() -> None:
    assert safe_relative_path("./app/main.py") == Path("app") / "main.py"
    for bad in ("", "   ", "/etc/passwd", "../../secret.env", "app/../../secret.env"):
        with pytest.raises(ValidationError):
            safe_relative_path(bad)


# --- 失败回滚 ---


def test_apply_rolls_back_when_tests_fail(workspace_root: Path) -> None:
    """测试不通过 → 改回原文件、抛 ApplyFailedError，且带上测试输出。"""
    target = _write(workspace_root, "auth/login.py", ORIGINAL)
    engine = ApplyEngine(workspace_root=workspace_root, test_command="exit 3")

    with pytest.raises(ApplyFailedError) as excinfo:
        engine.apply(_change("auth/login.py"))

    assert "测试未通过" in excinfo.value.message
    assert excinfo.value.details["test"]["exit_code"] == 3
    assert excinfo.value.details["test"]["passed"] is False
    assert target.read_text(encoding="utf-8") == ORIGINAL


def test_apply_rolls_back_by_deleting_new_file_when_tests_fail(workspace_root: Path) -> None:
    """新建文件 + 测试不通过 → 没有备份可还原，必须把刚写下的文件删掉。"""
    change = _change("app/health.py", original="", proposed="def health():\n    return 'ok'\n")
    engine = ApplyEngine(workspace_root=workspace_root, test_command="exit 5")

    with pytest.raises(ApplyFailedError):
        engine.apply(change)

    assert not (workspace_root / "app" / "health.py").exists()


def test_apply_rollback_keeps_original_content_even_with_unicode(workspace_root: Path) -> None:
    """回滚必须按字节还原中文内容，不能因为编码差异改名或损坏文件。"""
    original = "# 中文注释：登录校验\n\ndef login(user):\n    return False\n"
    target = _write(workspace_root, "auth/login.py", original)

    with pytest.raises(ApplyFailedError):
        ApplyEngine(workspace_root=workspace_root, test_command="exit 1").apply(
            _change("auth/login.py", original=original)
        )

    assert target.read_text(encoding="utf-8") == original


def test_apply_skips_tests_when_run_tests_is_false(workspace_root: Path) -> None:
    """run_tests=False 时不跑测试：命令必定失败也必须成功落盘。"""
    _write(workspace_root, "auth/login.py", ORIGINAL)
    engine = ApplyEngine(workspace_root=workspace_root, test_command="exit 1")

    outcome = engine.apply(_change("auth/login.py"), run_tests=False)

    assert outcome.test is None


# --- 测试执行 ---


def test_apply_runs_configured_test_command(workspace_root: Path) -> None:
    _write(workspace_root, "auth/login.py", ORIGINAL)
    # 用 python 调用替代 `echo ... && exit 0`：cmd.exe 下 echo/&&/exit 语义与编码都不同
    command = python_command("print('apply-test-ok')")
    engine = ApplyEngine(workspace_root=workspace_root, test_command=command)

    outcome = engine.apply(_change("auth/login.py"))

    assert outcome.test is not None
    assert outcome.test.passed is True
    assert outcome.test.exit_code == 0
    assert "apply-test-ok" in outcome.test.output
    assert outcome.test.command == command


def test_apply_rolls_back_when_test_times_out(workspace_root: Path) -> None:
    target = _write(workspace_root, "auth/login.py", ORIGINAL)
    engine = ApplyEngine(
        workspace_root=workspace_root,
        # Windows 无 `sleep`：改用跨平台的 python 长跑命令，超时语义不变
        test_command=python_command("import time; time.sleep(30)"),
        test_timeout_seconds=0.2,
    )

    with pytest.raises(ApplyFailedError) as excinfo:
        engine.apply(_change("auth/login.py"))

    assert excinfo.value.details["test"]["timed_out"] is True
    assert target.read_text(encoding="utf-8") == ORIGINAL


def test_test_runner_reports_failure_and_truncates_output(tmp_path: Path) -> None:
    runner = TestRunner(timeout_seconds=10)
    assert runner.run("exit 0", cwd=tmp_path).passed is True

    # 失败命令用 python 输出（ASCII，避开 Windows 控制台编码差异），exit code 语义相同
    failed = runner.run(
        python_command("import sys; print('apply-failure-marker'); sys.exit(7)"), cwd=tmp_path
    )
    assert failed.passed is False
    assert failed.exit_code == 7
    assert "apply-failure-marker" in failed.output

    long_output = runner.run(python_command("print('x' * 30000)"), cwd=tmp_path)
    assert len(long_output.output) < 30000
    assert "输出已截断" in long_output.output


def test_test_runner_returns_outcome_as_dict(tmp_path: Path) -> None:
    payload = TestRunner().run("exit 0", cwd=tmp_path).to_dict()
    assert set(payload) == {"command", "exit_code", "output", "timed_out", "duration_ms", "passed"}


# --- 备份服务 ---


def test_backup_service_returns_none_for_missing_file(workspace_root: Path) -> None:
    backups = BackupService(workspace_root=workspace_root)
    assert backups.backup(change_id="c1", file_path="a.py", relative_target=Path("a.py")) is None
    assert backups.backup_root == workspace_root / ".flux" / "backups"


def test_backup_service_restore_overwrites_target(workspace_root: Path) -> None:
    target = _write(workspace_root, "a.py", ORIGINAL)
    backups = BackupService(workspace_root=workspace_root)
    backup_path = backups.backup(change_id="c1", file_path="a.py", relative_target=Path("a.py"))
    assert backup_path is not None

    target.write_text("被写坏的内容\n", encoding="utf-8")
    backups.restore(backup_path=backup_path, relative_target=Path("a.py"))

    assert target.read_text(encoding="utf-8") == ORIGINAL


def test_backup_directory_is_excluded_from_git_status(workspace_root: Path) -> None:
    """Apply 的备份不该出现在用户的 git status 里，也不该改用户受版本控制的 .gitignore。"""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Flux Test",
        "GIT_AUTHOR_EMAIL": "flux-test@example.com",
        "GIT_COMMITTER_NAME": "Flux Test",
        "GIT_COMMITTER_EMAIL": "flux-test@example.com",
    }

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=workspace_root, check=True, capture_output=True, text=True, env=env
        ).stdout

    git("init", "-b", "main")
    _write(workspace_root, "a.py", ORIGINAL)
    git("add", "a.py")
    git("commit", "-m", "chore: 初始化")

    backup_path = BackupService(workspace_root=workspace_root).backup(
        change_id="c1", file_path="a.py", relative_target=Path("a.py")
    )

    assert backup_path is not None and backup_path.is_file()
    exclude = (workspace_root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".flux/" in exclude
    assert git("status", "--porcelain").strip() == ""
    assert not (workspace_root / ".gitignore").exists()


def test_backup_ignores_non_repository_workspace(workspace_root: Path) -> None:
    """工作区不是 Git 仓库时安静跳过，备份照样成功。"""
    _write(workspace_root, "a.py", ORIGINAL)

    backup_path = BackupService(workspace_root=workspace_root).backup(
        change_id="c1", file_path="a.py", relative_target=Path("a.py")
    )

    assert backup_path is not None and backup_path.is_file()


# --- 软链逃逸防护（分析文档 §5.1）---


def test_apply_rejects_symlinked_file_escaping_workspace(
    workspace_root: Path, tmp_path: Path
) -> None:
    """工作区里的软链文件指向根外：apply 必须拒绝，根外文件一个字节都不能动。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.py"
    secret.write_text(ORIGINAL, encoding="utf-8")
    (workspace_root / "config.py").symlink_to(secret)

    with pytest.raises(ValidationError) as excinfo:
        ApplyEngine(workspace_root=workspace_root).apply(_change("config.py"), run_tests=False)

    assert "符号链接" in excinfo.value.message
    assert secret.read_text(encoding="utf-8") == ORIGINAL
    # 拒绝发生在备份之前：工作区里连 .flux 都不该出现
    assert not (workspace_root / ".flux").exists()


def test_apply_rejects_symlinked_directory_escape_for_new_file(
    workspace_root: Path, tmp_path: Path
) -> None:
    """目录软链指向根外：新建文件的提案同样拒绝，不在根外造出任何文件。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace_root / "evil").symlink_to(outside, target_is_directory=True)

    change = _change("evil/new.py", original="", proposed="print('boom')\n")
    with pytest.raises(ValidationError) as excinfo:
        ApplyEngine(workspace_root=workspace_root).apply(change, run_tests=False)

    assert "符号链接" in excinfo.value.message
    assert list(outside.iterdir()) == []


def test_apply_rejects_symlink_even_when_it_stays_inside_root(workspace_root: Path) -> None:
    """指向根内的软链也 fail-closed：不支持跟随软链是一条规则，不做例外判断。"""
    _write(workspace_root, "real/config.py", ORIGINAL)
    (workspace_root / "config.py").symlink_to(workspace_root / "real" / "config.py")

    with pytest.raises(ValidationError) as excinfo:
        ApplyEngine(workspace_root=workspace_root).apply(_change("config.py"), run_tests=False)

    assert "符号链接" in excinfo.value.message
    assert (workspace_root / "real" / "config.py").read_text(encoding="utf-8") == ORIGINAL


def test_backup_rejects_symlinked_backup_root(workspace_root: Path, tmp_path: Path) -> None:
    """`.flux/` 本身被软链到根外：备份先拒绝，绝不把用户文件复制到工作区之外。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(workspace_root, "a.py", ORIGINAL)
    (workspace_root / ".flux").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValidationError) as excinfo:
        BackupService(workspace_root=workspace_root).backup(
            change_id="c1", file_path="a.py", relative_target=Path("a.py")
        )

    assert "符号链接" in excinfo.value.message
    assert list(outside.iterdir()) == []


def test_backup_restore_rejects_symlinked_target(workspace_root: Path, tmp_path: Path) -> None:
    """回滚目标变成软链时同样拒绝：别把备份内容写到根外去。"""
    _write(workspace_root, "a.py", ORIGINAL)
    backups = BackupService(workspace_root=workspace_root)
    backup_path = backups.backup(change_id="c1", file_path="a.py", relative_target=Path("a.py"))
    assert backup_path is not None

    secret = tmp_path / "secret.py"
    secret.write_text("根外内容\n", encoding="utf-8")
    (workspace_root / "a.py").unlink()
    (workspace_root / "a.py").symlink_to(secret)

    with pytest.raises(ValidationError) as excinfo:
        backups.restore(backup_path=backup_path, relative_target=Path("a.py"))

    assert "符号链接" in excinfo.value.message
    assert secret.read_text(encoding="utf-8") == "根外内容\n"


# --- 多文件原子 Apply（P0-03）---
#
# 目标：A ✓ B ✓ C ✗ 之后 A/B 绝不允许留在盘上。以下用例全部在真实临时目录里
# 制造各类失败，逐个断言"用户文件回到 Apply 之前的样子"。


def _batch(workspace_root: Path, *paths: str) -> tuple[list[VirtualChange], dict[str, Path]]:
    """按 (路径 → 原文 ORIGINAL / 提案 PROPOSED) 铺好文件并构造批次。"""
    changes: list[VirtualChange] = []
    targets: dict[str, Path] = {}
    for path in paths:
        targets[path] = _write(workspace_root, path, ORIGINAL)
        changes.append(_change(path))
    return changes, targets


def test_apply_many_writes_every_file_and_returns_one_outcome_per_change(
    workspace_root: Path,
) -> None:
    changes, targets = _batch(workspace_root, "a.py", "b.py", "c.py")
    engine = ApplyEngine(workspace_root=workspace_root, test_command="exit 0")

    outcomes = engine.apply_many(changes, run_tests=True)

    assert [o.change_id for o in outcomes] == [str(c.id) for c in changes]
    for path, target in targets.items():
        assert target.read_text(encoding="utf-8") == PROPOSED, path
    # 每条改动都有独立的备份，且内容都是改动前的原文
    for outcome in outcomes:
        assert Path(outcome.backup_path).read_text(encoding="utf-8") == ORIGINAL
    # 整批只跑一次测试，结果挂在每条 outcome 上（同一份凭据）
    assert all(o.test is not None and o.test.passed for o in outcomes)
    assert len({id(o.test) for o in outcomes}) == 1


def test_apply_many_runs_the_test_command_only_once(workspace_root: Path) -> None:
    changes, _ = _batch(workspace_root, "a.py", "b.py", "c.py")
    counter = workspace_root / "runs.txt"
    engine = ApplyEngine(
        workspace_root=workspace_root,
        # 用 python 追加代替 `printf x >> <路径>`：cmd.exe 无 printf，且重定向/路径引号语义不同
        test_command=python_command("open('runs.txt', 'a').write('x')"),
    )

    engine.apply_many(changes)

    assert counter.read_text(encoding="utf-8") == "x"


def test_apply_many_creates_new_files_without_backups(workspace_root: Path) -> None:
    _write(workspace_root, "a.py", ORIGINAL)
    changes = [
        _change("a.py"),
        _change("pkg/__init__.py", original="", proposed="# 包入口\n"),
    ]

    outcomes = ApplyEngine(workspace_root=workspace_root).apply_many(changes, run_tests=False)

    assert (workspace_root / "pkg" / "__init__.py").read_text(encoding="utf-8") == "# 包入口\n"
    assert outcomes[0].backup_path is not None
    assert outcomes[1].backup_path is None


def test_apply_many_rolls_back_all_files_when_tests_fail(workspace_root: Path) -> None:
    """A 改、B 改、C 新建，测试不过 → A/B 回原文、C 删除，一个都不许留。"""
    changes, targets = _batch(workspace_root, "a.py", "b.py")
    created = _change("c.py", original="", proposed="fresh\n")
    changes.append(created)
    engine = ApplyEngine(workspace_root=workspace_root, test_command="exit 9")

    with pytest.raises(ApplyFailedError) as excinfo:
        engine.apply_many(changes)

    assert "测试未通过" in excinfo.value.message
    assert excinfo.value.details["test"]["exit_code"] == 9
    for path, target in targets.items():
        assert target.read_text(encoding="utf-8") == ORIGINAL, path
    assert not (workspace_root / "c.py").exists()


def test_apply_many_preflight_rejects_expired_proposal_before_touching_any_file(
    workspace_root: Path,
) -> None:
    """batch 里有一条已过期 → 整批拒绝：另一条合法的文件也不许被写。"""
    user_version = "user 自己改的\n"
    changes, targets = _batch(workspace_root, "a.py", "b.py")
    targets["b.py"].write_text(user_version, encoding="utf-8")

    with pytest.raises(ConflictError) as excinfo:
        ApplyEngine(workspace_root=workspace_root).apply_many(changes, run_tests=False)

    assert "已被改动" in excinfo.value.message
    assert targets["a.py"].read_text(encoding="utf-8") == ORIGINAL
    assert targets["b.py"].read_text(encoding="utf-8") == user_version
    # 预检阶段拒绝：连备份都不该产生
    assert not (workspace_root / ".flux").exists()


def test_apply_many_preflight_rejects_missing_file_batch(workspace_root: Path) -> None:
    """文件不存在（提案基于有原文的文件）→ 整批拒绝，其余文件保持原样。"""
    changes, targets = _batch(workspace_root, "a.py")
    ghost = _change("gone.py")
    changes.insert(0, ghost)

    with pytest.raises(ConflictError) as excinfo:
        ApplyEngine(workspace_root=workspace_root).apply_many(changes, run_tests=False)

    assert "已不存在" in excinfo.value.message
    assert targets["a.py"].read_text(encoding="utf-8") == ORIGINAL
    assert not (workspace_root / ".flux").exists()


def test_apply_many_rejects_duplicate_paths_and_ids(workspace_root: Path) -> None:
    _write(workspace_root, "a.py", ORIGINAL)
    same_file = [_change("a.py"), _change("./a.py")]
    with pytest.raises(ValidationError) as excinfo:
        ApplyEngine(workspace_root=workspace_root).apply_many(same_file, run_tests=False)
    assert "同一文件在同一批次里出现了多条提案" in excinfo.value.message

    one = _change("a.py")
    with pytest.raises(ValidationError) as excinfo:
        ApplyEngine(workspace_root=workspace_root).apply_many([one, one], run_tests=False)
    assert "同一提案在批次里出现了多次" in excinfo.value.message

    assert (workspace_root / "a.py").read_text(encoding="utf-8") == ORIGINAL
    assert not (workspace_root / ".flux").exists()


def test_apply_many_rejects_empty_batch() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ApplyEngine().apply_many([])
    assert "至少需要一条提案" in excinfo.value.message


def test_apply_many_rolls_back_when_write_fails_midway(
    workspace_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写到第二个文件时磁盘写入失败（模拟权限不足）→ 第一个也要恢复原样。"""
    changes, targets = _batch(workspace_root, "a.py", "b.py", "c.py")
    original_write = ApplyEngine._write
    calls: list[str] = []

    def flaky_write(target: Path, content: str) -> None:
        calls.append(str(target))
        if len(calls) == 2:
            raise PermissionError(f"权限不足，无法写入 {target}")
        original_write(target, content)

    monkeypatch.setattr(ApplyEngine, "_write", staticmethod(flaky_write))

    with pytest.raises(ApplyFailedError) as excinfo:
        ApplyEngine(workspace_root=workspace_root).apply_many(changes, run_tests=False)

    assert "权限不足" in excinfo.value.message
    for path, target in targets.items():
        assert target.read_text(encoding="utf-8") == ORIGINAL, path


def test_apply_many_rolls_back_when_new_file_write_fails(
    workspace_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写到"新建文件"这一步失败 → 已写的旧文件回滚，且不留下半成品文件。"""
    _write(workspace_root, "a.py", ORIGINAL)
    changes = [
        _change("a.py"),
        _change("pkg/new.py", original="", proposed="boom\n"),
    ]
    original_write = ApplyEngine._write
    calls: list[str] = []

    def flaky_write(target: Path, content: str) -> None:
        calls.append(str(target))
        if len(calls) == 2:
            raise OSError("磁盘写满")
        original_write(target, content)

    monkeypatch.setattr(ApplyEngine, "_write", staticmethod(flaky_write))

    with pytest.raises(ApplyFailedError) as excinfo:
        ApplyEngine(workspace_root=workspace_root).apply_many(changes, run_tests=False)

    assert "磁盘写满" in excinfo.value.message
    assert (workspace_root / "a.py").read_text(encoding="utf-8") == ORIGINAL
    assert not (workspace_root / "pkg" / "new.py").exists()


def test_apply_many_backup_failure_leaves_every_file_untouched(
    workspace_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """备份阶段失败 → 一个文件都不许写（用户文件保持原样）。"""
    changes, targets = _batch(workspace_root, "a.py", "b.py")
    original_backup = BackupService.backup
    calls: list[str] = []

    def flaky_backup(self, *, change_id: str, file_path: str, relative_target: Path):
        calls.append(change_id)
        if len(calls) == 2:
            raise OSError("备份目录写不进去")
        return original_backup(
            self, change_id=change_id, file_path=file_path, relative_target=relative_target
        )

    monkeypatch.setattr(BackupService, "backup", flaky_backup)

    with pytest.raises(ApplyFailedError) as excinfo:
        ApplyEngine(workspace_root=workspace_root).apply_many(changes, run_tests=False)

    assert "备份原文件时出错" in excinfo.value.message
    for path, target in targets.items():
        assert target.read_text(encoding="utf-8") == ORIGINAL, path


def test_apply_many_surfaces_rollback_failure_instead_of_silence(
    workspace_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写入失败 + 回滚也失败：错误里必须带 rollback_errors，绝不能假装已恢复。"""
    changes, _ = _batch(workspace_root, "a.py", "b.py")
    original_write = ApplyEngine._write
    calls: list[str] = []

    def flaky_write(target: Path, content: str) -> None:
        calls.append(str(target))
        if len(calls) == 2:
            raise OSError("磁盘写入失败")
        original_write(target, content)

    def broken_restore(self, *, backup_path: Path, relative_target: Path) -> None:
        raise OSError("备份还原失败")

    monkeypatch.setattr(ApplyEngine, "_write", staticmethod(flaky_write))
    monkeypatch.setattr(BackupService, "restore", broken_restore)

    with pytest.raises(ApplyFailedError) as excinfo:
        ApplyEngine(workspace_root=workspace_root).apply_many(changes, run_tests=False)

    assert "回滚未完全成功" in excinfo.value.message
    rollback_errors = excinfo.value.details["rollback_errors"]
    # 写入失败的那条也要尝试回滚（write_text 可能已把文件截断），按逆序逐条记录
    assert [item["file_path"] for item in rollback_errors] == ["b.py", "a.py"]
    assert all("备份还原失败" in item["error"] for item in rollback_errors)


def test_concurrent_batches_do_not_interleave(workspace_root: Path) -> None:
    """两个线程同时改同一文件：一个成功、另一个按过期冲突拒绝，内容不属于双方混合。"""
    target = _write(workspace_root, "a.py", ORIGINAL)
    first = _change("a.py", proposed="first\n")
    second = _change("a.py", proposed="second\n")
    engine = ApplyEngine(workspace_root=workspace_root)
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def run(name: str, change: VirtualChange) -> None:
        barrier.wait(timeout=5)
        try:
            engine.apply(change, run_tests=False)
        except Exception as exc:  # noqa: BLE001 - 竞争失败方的异常就是断言对象
            results[name] = exc
        else:
            results[name] = "applied"

    threads = [
        threading.Thread(target=run, args=("first", first)),
        threading.Thread(target=run, args=("second", second)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    winners = [name for name, value in results.items() if value == "applied"]
    losers = [value for name, value in results.items() if value != "applied"]
    assert len(winners) == 1, results
    assert len(losers) == 1 and isinstance(losers[0], ConflictError)
    # 盘上必须是胜者提案的完整内容，不能是两段写入的混合
    expected = "first\n" if winners[0] == "first" else "second\n"
    assert target.read_text(encoding="utf-8") == expected
