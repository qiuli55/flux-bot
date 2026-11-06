"""Flux Server CLI（§9）用例：进程内直连 Core，不依赖 MCP / HTTP。

测试都用真实签发路径造令牌、真实仓储造数据，再通过 `main([...], settings=...)`
跑一遍完整命令——CLI 的价值就在"绕过 MCP 也能操作真实状态"，mock 掉数据库就失去意义了。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest

from flux.cli.main import main
from flux.config import Settings
from flux.container import Container
from flux.core.agent_runtime.adapters import CliAgentAdapter, CliAgentProbe
from flux.core.agent_runtime.manager import AgentSpec
from flux.core.mcp.tools import CORE_TOOL_NAMES
from flux.core.virtual_workspace.proposal_parser import CodeChangeSet, FileChange
from flux.enums import (
    AgentInstallStatus,
    AgentRole,
    ApplyBatchStatus,
    Capability,
    VirtualChangeStatus,
)


def run_cli(
    settings: Settings, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict]:
    """执行一条命令，返回 (退出码, stdout 的 JSON)。"""
    code = main(argv, settings=settings)
    out = capsys.readouterr().out
    return code, json.loads(out)


def _seed_agent_token(settings: Settings, name: str = "codex") -> str:
    """建一个 Agent 档案并签发只读令牌，返回明文（走真实注册表 + 签发路径）。"""

    async def _seed() -> str:
        container = Container(settings)
        try:
            handle = await container.agents.create(
                AgentSpec(
                    name=name,
                    role=AgentRole.DEVELOPER,
                    permissions=frozenset({Capability.FILE_READ}),
                )
            )
            _, raw = await container.agent_tokens.issue(
                agent_id=handle.id_str, scopes=[Capability.FILE_READ]
            )
            return raw
        finally:
            await container.dispose()

    return asyncio.run(_seed())


def _seed_task(settings: Settings) -> uuid.UUID:
    task_id = uuid.uuid4()

    async def _seed() -> None:
        container = Container(settings)
        try:
            await container.task_repo.create(
                task_id=task_id,
                description="demo task",
                priority=100,
                project_id=None,
                agent_id=None,
            )
        finally:
            await container.dispose()

    asyncio.run(_seed())
    return task_id


def _seed_proposal(settings: Settings) -> uuid.UUID:
    change_id = uuid.uuid4()

    async def _seed() -> None:
        container = Container(settings)
        try:
            await container.proposal_repo.create(
                change_id=change_id,
                file_path="todo_service/store.py",
                original_content="a = 1\n",
                proposed_content="a = 2\n",
                original_hash="0" * 64,
                diff="--- a\n+++ b\n-a = 1\n+a = 2\n",
                added_lines=1,
                removed_lines=1,
                hunks=1,
                summary="把 a 改成 2",
            )
        finally:
            await container.dispose()

    asyncio.run(_seed())
    return change_id


def _seed_pending_recovery(settings: Settings) -> uuid.UUID:
    """造一条待决策的崩溃恢复项：提案 failed + apply_error 带恢复前缀 + needs_attention 批。"""
    change_id = uuid.uuid4()
    batch_id = uuid.uuid4()

    async def _seed() -> None:
        container = Container(settings)
        try:
            await container.proposal_repo.create(
                change_id=change_id,
                file_path="app/auth.py",
                original_content="    return False\n",
                proposed_content="    return check_password(user)\n",
                original_hash="0" * 64,
                diff="--- a\n+++ b\n",
                added_lines=1,
                removed_lines=1,
                hunks=1,
            )
            await container.proposal_repo.set_apply_result(
                change_id,
                status=VirtualChangeStatus.FAILED,
                apply_error="崩溃恢复：崩溃后检测到外部修改，未覆盖当前内容",
            )
            await container.batch_repo.create(
                batch_id=batch_id, change_ids=[str(change_id)], phase="testing"
            )
            await container.batch_repo.finish(batch_id, status=ApplyBatchStatus.NEEDS_ATTENTION)
        finally:
            await container.dispose()

    asyncio.run(_seed())
    return change_id


def test_proposal_recovery_list_and_keep_decision(
    settings: Settings, db_schema: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 闭环：list 看到挂起项（三版本）→ resolve --keep → 待决策清空。"""
    change_id = _seed_pending_recovery(settings)

    code, listing = run_cli(settings, ["proposal", "recovery", "list"], capsys)
    assert code == 0
    assert listing["data"]["count"] == 1
    item = listing["data"]["items"][0]
    assert item["change_id"] == str(change_id)
    assert item["backup_available"] is False
    assert "外部修改" in item["note"]

    code, resolved = run_cli(
        settings, ["proposal", "recovery", "resolve", str(change_id), "--keep"], capsys
    )
    assert code == 0
    assert resolved["data"]["resolution"] == "keep"
    assert resolved["data"]["status"] == VirtualChangeStatus.FAILED.value

    code, after = run_cli(settings, ["proposal", "recovery", "list"], capsys)
    assert code == 0
    assert after["data"]["count"] == 0


# --- doctor / status ---


def test_doctor_reports_healthy_surface(
    settings: Settings, db_schema: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = run_cli(settings, ["doctor"], capsys)
    assert code == 0
    assert payload["ok"] is True
    checks = {check["name"]: check for check in payload["data"]["checks"]}
    assert checks["database"]["ok"] is True
    assert checks["tools"]["ok"] is True
    # 未配置 workspace_root 只是 warn，不该让 doctor 失败
    assert checks["workspace_root"]["ok"] is False
    assert checks["workspace_root"]["level"] == "warn"


def test_doctor_redacts_database_password(
    settings: Settings, db_schema: None, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = settings.model_copy(
        update={"database_url": "postgresql+asyncpg://flux:s3cret@db:5432/flux"}
    )
    _, payload = run_cli(secret, ["doctor"], capsys)
    assert "s3cret" not in json.dumps(payload)


def test_status_on_unmigrated_db_gives_hint(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    # 没有 db_schema 夹具 = 结构未建，模拟"忘了 make migrate"的现场
    code, payload = run_cli(settings, ["status"], capsys)
    assert code == 1
    assert payload["error"]["code"] == "database_error"
    assert "hint" in payload["error"]["details"]


def test_status_counts_seeded_task(
    settings: Settings, db_schema: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_task(settings)
    code, payload = run_cli(settings, ["status"], capsys)
    assert code == 0
    data = payload["data"]
    assert data["tasks"]["total"] == 1
    assert data["tasks"]["by_status"]["pending"] == 1
    assert data["non_terminal_runs"] == []


# --- tools ---


def test_tools_list_exposes_registry(
    settings: Settings, db_schema: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = run_cli(settings, ["tools", "list"], capsys)
    assert code == 0
    data = payload["data"]
    # 核心工具必须齐全（required ⊆ advertised），不要求精确相等：新增工具不该被误杀（§5）。
    names = {tool["name"] for tool in data["tools"]}
    assert names >= CORE_TOOL_NAMES
    assert data["count"] == len(data["tools"])
    assert "shell.exec" in data["forbidden"]


def test_tools_call_without_token_is_rejected(
    settings: Settings, db_schema: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = run_cli(settings, ["tools", "call", "context.get"], capsys)
    assert code == 1
    assert payload["error"]["code"] == "unauthenticated"


def test_tools_call_unknown_tool(
    settings: Settings, db_schema: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = run_cli(settings, ["tools", "call", "nope.notreal"], capsys)
    assert code == 1
    assert payload["error"]["code"] == "not_found"


def test_tools_call_reads_file_in_process(
    settings: Settings,
    db_schema: None,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (workspace_root / "hello.txt").write_text("hi flux", encoding="utf-8")
    scoped = settings.model_copy(update={"workspace_root": str(workspace_root)})
    token = _seed_agent_token(scoped)

    code, payload = run_cli(
        scoped,
        ["tools", "call", "workspace.read", "--params", '{"path": "hello.txt"}', "--token", token],
        capsys,
    )
    assert code == 0
    assert payload["data"]["result"]["content"] == "hi flux"


def test_tools_call_bad_params(
    settings: Settings, db_schema: None, capsys: pytest.CaptureFixture[str]
) -> None:
    token = _seed_agent_token(settings)
    code, payload = run_cli(
        settings,
        ["tools", "call", "context.get", "--params", "[1, 2]", "--token", token],
        capsys,
    )
    assert code == 1
    assert payload["error"]["code"] == "bad_request"


# --- task / proposal ---


def test_task_list_and_show(
    settings: Settings, db_schema: None, capsys: pytest.CaptureFixture[str]
) -> None:
    task_id = _seed_task(settings)

    code, listing = run_cli(settings, ["task", "list", "--status", "pending"], capsys)
    assert code == 0
    assert listing["data"]["count"] == 1
    assert listing["data"]["tasks"][0]["description"] == "demo task"

    code, shown = run_cli(settings, ["task", "show", str(task_id), "--messages", "5"], capsys)
    assert code == 0
    assert shown["data"]["id"] == str(task_id)
    assert shown["data"]["messages"] == []


def test_proposal_list_and_show(
    settings: Settings, db_schema: None, capsys: pytest.CaptureFixture[str]
) -> None:
    change_id = _seed_proposal(settings)

    code, listing = run_cli(settings, ["proposal", "list"], capsys)
    assert code == 0
    assert listing["data"]["count"] == 1
    assert listing["data"]["proposals"][0]["file_path"] == "todo_service/store.py"

    code, shown = run_cli(settings, ["proposal", "show", str(change_id)], capsys)
    assert code == 0
    assert shown["data"]["id"] == str(change_id)
    assert shown["data"]["status"] == "pending"


def _seed_delete_proposal(settings: Settings) -> uuid.UUID:
    """造一条 delete 类提案，验证 `proposal diff` 的动作标记输出。"""
    change_id = uuid.uuid4()

    async def _seed() -> None:
        container = Container(settings)
        try:
            await container.proposal_repo.create(
                change_id=change_id,
                file_path="src/legacy.py",
                kind="delete",
                original_content="旧模块内容\n第二行\n",
                proposed_content=None,
                original_hash="0" * 64,
                diff="--- a/src/legacy.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-旧模块内容\n-第二行\n",
                added_lines=0,
                removed_lines=2,
                hunks=1,
            )
        finally:
            await container.dispose()

    asyncio.run(_seed())
    return change_id


def _seed_applied_modify(settings: Settings, workspace_root: Path) -> uuid.UUID:
    """经真实 propose → apply 造一条已落盘的修改提案（供 rollback 用例）。"""

    async def _seed() -> uuid.UUID:
        container = Container(settings)
        try:
            change_set = CodeChangeSet(
                summary="改 login",
                changes=(FileChange(path="auth/login.py", content=PROPOSED, op="modify"),),
            )
            change = (
                await container.workspace.propose_changes(
                    change_set, original_files={"auth/login.py": ORIGINAL}
                )
            )[0]
            await container.workspace.apply(str(change.id))
            return change.id
        finally:
            await container.dispose()

    return asyncio.run(_seed())


ORIGINAL = "def login(user):\n    return False\n"
PROPOSED = "def login(user):\n    return check_password(user)\n"


def test_proposal_diff_reports_kind_header(
    settings: Settings, db_schema: None, capsys: pytest.CaptureFixture[str]
) -> None:
    change_id = _seed_delete_proposal(settings)

    code, payload = run_cli(settings, ["proposal", "diff", str(change_id)], capsys)

    assert code == 0
    data = payload["data"]
    assert data["kind"] == "delete"
    assert data["header"] == "D src/legacy.py"
    assert data["added_lines"] == 0
    assert "+++ /dev/null" in data["diff"]


def test_cli_rollback_last(
    settings: Settings,
    db_schema: None,
    workspace_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scoped = settings.model_copy(update={"workspace_root": str(workspace_root)})
    root = workspace_root / "auth"
    root.mkdir()
    (root / "login.py").write_text(ORIGINAL, encoding="utf-8")
    change_id = _seed_applied_modify(scoped, workspace_root)
    assert (root / "login.py").read_text(encoding="utf-8") == PROPOSED

    code, rolled = run_cli(scoped, ["proposal", "rollback", "--last"], capsys)

    assert code == 0
    assert rolled["data"]["status"] == ApplyBatchStatus.ROLLED_BACK.value
    assert (root / "login.py").read_text(encoding="utf-8") == ORIGINAL

    # 单条入口：回滚后再次回滚同一 change 因批已 rolled_back 而 409
    code, conflict = run_cli(scoped, ["proposal", "rollback", str(change_id)], capsys)
    assert code == 1
    assert conflict["error"]["code"] == "conflict"


def test_logs_without_config_is_actionable(
    settings: Settings,
    db_schema: None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FLUX_LOG_FILE", raising=False)
    code, payload = run_cli(settings, ["logs"], capsys)
    assert code == 1
    assert payload["error"]["code"] == "configuration_error"
    assert "hint" in payload["error"]["details"]


def test_logs_tails_file(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_file = tmp_path / "flux.log"
    log_file.write_text("\n".join(f"line-{i}" for i in range(5)) + "\n", encoding="utf-8")
    code, payload = run_cli(settings, ["logs", "--file", str(log_file), "--lines", "2"], capsys)
    assert code == 0
    assert payload["data"]["lines"] == ["line-3", "line-4"]


# --- agents（发现 / 接入，P0-7）---


class _StubAdapter(CliAgentAdapter):
    """替身 Adapter：把"本机有没有装"变成构造参数，测试不碰真实 PATH。"""

    def __init__(self, name: str, *, installed: bool) -> None:
        self.name = name
        self._installed = installed

    def discover(self) -> CliAgentProbe:
        if not self._installed:
            return CliAgentProbe(
                name=self.name, adapter="stub", status=AgentInstallStatus.NOT_INSTALLED
            )
        return CliAgentProbe(
            name=self.name,
            adapter="stub",
            status=AgentInstallStatus.VERIFIED,
            executable=self.name,
            path=f"/usr/local/bin/{self.name}",
            version="1.2.3",
            auth_status="ok",
            capabilities=("mcp", "stream"),
        )

    def get_version(self) -> str | None:
        return "1.2.3" if self._installed else None

    def check_auth(self) -> str:
        return "ok" if self._installed else "unknown"


def _use_adapters(monkeypatch: pytest.MonkeyPatch, *adapters: CliAgentAdapter) -> None:
    monkeypatch.setattr("flux.container.build_default_adapters", lambda: adapters)


def test_agents_scan_list_connect_remove(
    settings: Settings,
    db_schema: None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_adapters(monkeypatch, _StubAdapter("stub-agent", installed=True))

    code, scanned = run_cli(settings, ["agents", "scan"], capsys)
    assert code == 0
    assert scanned["data"]["count"] == 1
    assert scanned["data"]["agents"][0]["status"] == "VERIFIED"
    assert scanned["data"]["agents"][0]["version"] == "1.2.3"

    code, listed = run_cli(settings, ["agents", "list"], capsys)
    assert code == 0
    assert listed["data"]["count"] == 1

    code, connected = run_cli(settings, ["agents", "connect", "stub-agent"], capsys)
    assert code == 0
    assert connected["data"]["agent"]["status"] == "READY"

    code, removed = run_cli(settings, ["agents", "remove", "stub-agent"], capsys)
    assert code == 0
    assert removed["data"]["removed"] == "stub-agent"

    code, empty = run_cli(settings, ["agents", "list"], capsys)
    assert code == 0
    assert empty["data"]["count"] == 0


def test_agents_scan_does_not_regress_connected_status(
    settings: Settings,
    db_schema: None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已接入（READY）的 Agent 再扫描时不能倒退 —— 状态机单向收敛（P0-7）。"""
    _use_adapters(monkeypatch, _StubAdapter("stub-agent", installed=True))
    assert run_cli(settings, ["agents", "connect", "stub-agent"], capsys)[0] == 0

    code, rescanned = run_cli(settings, ["agents", "scan"], capsys)
    assert code == 0
    assert rescanned["data"]["agents"][0]["status"] == "READY"


def test_agents_connect_uninstalled_is_rejected(
    settings: Settings,
    db_schema: None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_adapters(monkeypatch, _StubAdapter("ghost", installed=False))
    code, payload = run_cli(settings, ["agents", "connect", "ghost"], capsys)
    assert code == 1
    assert payload["error"]["code"] == "validation_error"


def test_agents_connect_without_name_or_all(
    settings: Settings,
    db_schema: None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_adapters(monkeypatch)
    code, payload = run_cli(settings, ["agents", "connect"], capsys)
    assert code == 1
    assert payload["error"]["code"] == "bad_request"


def test_agents_connect_all_skips_uninstalled(
    settings: Settings,
    db_schema: None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_adapters(
        monkeypatch,
        _StubAdapter("present", installed=True),
        _StubAdapter("ghost", installed=False),
    )
    code, payload = run_cli(settings, ["agents", "connect", "--all"], capsys)
    assert code == 0
    assert [row["name"] for row in payload["data"]["agents"]] == ["present"]
    assert payload["data"]["agents"][0]["status"] == "READY"
