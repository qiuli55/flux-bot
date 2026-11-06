"""对话权限四模式（2026-10-25 三模式 + 2026-10-26 免审核）：白名单、令牌 scopes、
风险分类、审批闸门、计划文档。

四层边界各自有独立用例：

1. 模式 → 工具白名单（Plan 只读；Normal / Full / Unattended 全放行）；
2. 模式 → 令牌 scopes（Plan 不签 terminal.execute）；
3. 风险分类 fail-closed（受保护优先、复合命令、未知命令一律按高风险）；
4. 审批闸门（允许 / 拒绝 / 超时 / 取消；Full 跳过 high 但不跳过 protected；
   Unattended 连 protected 也放行且不产生审批记录）；
5. plan.write 路径固定、计划卡落库；
6. API 切换模式与 `effective_from`。
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from flux.core.mcp.auth import AgentIdentity
from flux.core.mcp.server import McpServer, _attach_run_id
from flux.core.mcp.tools.base import ToolContext
from flux.core.permissions.modes import (
    PLAN_ALLOWED_TOOLS,
    allowed_tools,
    normalize_mode,
    scopes_for_mode,
    tool_allowed,
)
from flux.core.permissions.risk import (
    LEVEL_HIGH,
    LEVEL_LOW,
    LEVEL_PROTECTED,
    classify_command,
)
from flux.core.task_engine.approvals import (
    APPROVAL_ALLOW,
    ApprovalDeniedError,
    raise_approval,
    wait_for_approval,
)
from flux.enums import (
    Capability,
    DecisionMode,
    DecisionStatus,
    DshRunStatus,
    PermissionMode,
    TaskStatus,
)
from flux.errors import ConflictError

#: `flux.core.mcp.tools` 包里的同名属性是工具工厂函数，会遮蔽子模块；按模块路径直接取模块。
terminal_tools = importlib.import_module("flux.core.mcp.tools.terminal_tools")
plan_tools = importlib.import_module("flux.core.mcp.tools.plan_tools")

PREFIX = "/api/v1"


# ---------------------------------------------------------------- 1. 模式白名单


def test_plan_whitelist_is_read_only_plus_plan_write() -> None:
    """Plan 白名单里不能出现任何写/执行/设计写入工具。"""
    assert "plan.write" in PLAN_ALLOWED_TOOLS
    for forbidden in (
        "terminal.execute",
        "proposal.create",
        "proposal.preview",
        "design.create",
        "design.update",
        "design.share",
    ):
        assert forbidden not in PLAN_ALLOWED_TOOLS


def test_every_plan_allowed_tool_exists() -> None:
    """白名单里的名字必须都是真实注册过的工具（写错名字等于悄悄禁掉一个能力）。"""
    from flux.core.mcp.tools import TOOLS

    assert set(TOOLS) >= PLAN_ALLOWED_TOOLS


def test_normal_and_full_allow_every_tool() -> None:
    assert allowed_tools(PermissionMode.NORMAL.value) is None
    assert allowed_tools(PermissionMode.FULL.value) is None
    assert allowed_tools(PermissionMode.UNATTENDED.value) is None
    assert tool_allowed(PermissionMode.FULL.value, "terminal.execute") is True
    assert tool_allowed(PermissionMode.UNATTENDED.value, "terminal.execute") is True


def test_unknown_mode_falls_back_to_normal() -> None:
    """未识别的模式按 normal 处理（宁可多问一次审批）。"""
    assert normalize_mode(None) == "normal"
    assert normalize_mode("yolo") == "normal"
    assert allowed_tools("yolo") is None
    assert tool_allowed("yolo", "terminal.execute") is True


def test_plan_token_has_no_terminal_scope() -> None:
    plan_scopes = scopes_for_mode(PermissionMode.PLAN.value)
    assert Capability.PLAN_WRITE in plan_scopes
    assert Capability.TERMINAL_EXECUTE not in plan_scopes
    assert Capability.FILE_WRITE not in plan_scopes
    assert Capability.TERMINAL_EXECUTE in scopes_for_mode(PermissionMode.NORMAL.value)
    assert Capability.TERMINAL_EXECUTE in scopes_for_mode(PermissionMode.FULL.value)
    assert Capability.TERMINAL_EXECUTE in scopes_for_mode(PermissionMode.UNATTENDED.value)


# ---------------------------------------------------------------- 2. 风险分类


@pytest.mark.parametrize(
    ("command", "level"),
    [
        ("ls -la", LEVEL_LOW),
        ("cat README.md", LEVEL_LOW),
        ("git status", LEVEL_LOW),
        ("python -m pytest tests -q", LEVEL_LOW),
        ("npm test", LEVEL_LOW),
        ("sed -n '1,20p' app.py", LEVEL_LOW),
        ("find . -name '*.py'", LEVEL_LOW),
        ("rm -rf build", LEVEL_HIGH),
        ("git push origin main", LEVEL_HIGH),
        ("npm install", LEVEL_HIGH),
        ("echo hi > out.txt", LEVEL_HIGH),
        ("ls $(pwd)", LEVEL_HIGH),
        ("totally-unknown-cmd --flag", LEVEL_HIGH),
        ("systemctl restart nginx", LEVEL_PROTECTED),
        ("service nginx stop", LEVEL_PROTECTED),
        ("reboot", LEVEL_PROTECTED),
        ("cat /root/.ssh/id_rsa", LEVEL_PROTECTED),
        ("cat /opt/flux/server/flux-server.env", LEVEL_PROTECTED),
        ("vim app.pem", LEVEL_PROTECTED),
    ],
)
def test_classify_command_levels(command: str, level: str) -> None:
    assert classify_command(command).level == level


def test_protected_wins_over_high_for_compound_command() -> None:
    """复合命令里只要有一段命中受保护对象，整条命令按 protected 走（最严格一级）。"""
    report = classify_command("ls -la && cat /root/.ssh/id_rsa")
    assert report.level == LEVEL_PROTECTED


def test_compound_with_high_risk_segment_is_high() -> None:
    assert classify_command("ls && rm -rf build").level == LEVEL_HIGH
    assert classify_command("pwd; ls").level == LEVEL_LOW


def test_write_redirect_to_dev_null_is_not_high() -> None:
    assert classify_command("ls > /dev/null").level == LEVEL_LOW
    assert classify_command("make build 2>&1 | tail -20").level in (LEVEL_LOW, LEVEL_HIGH)


def test_env_template_is_not_protected() -> None:
    """.env.example / .env.sample 是模板，不应触发受保护审批。"""
    assert classify_command("cat .env.example").level != LEVEL_PROTECTED
    assert classify_command("cat .env").level == LEVEL_PROTECTED


def test_empty_command_is_high() -> None:
    assert classify_command("   ").level == LEVEL_HIGH


# ---------------------------------------------------------------- 3. MCP 模式白名单


def _ctx(container, *, mode: str, run_id: str | None = None) -> ToolContext:
    return ToolContext(
        container=container,
        identity=AgentIdentity(
            agent_id=str(uuid.uuid4()),
            token_id="t",
            scopes=frozenset(
                {Capability.FILE_READ, Capability.FILE_WRITE, Capability.TERMINAL_EXECUTE}
            ),
            run_id=run_id,
            permission_mode=mode,
        ),
        tool_name="terminal.execute",
    )


async def test_plan_mode_tools_list_hides_write_tools(apply_container) -> None:
    server = McpServer(apply_container)
    identity = _ctx(apply_container, mode="plan").identity

    result = await server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, identity
    )
    names = {tool["name"] for tool in result["result"]["tools"]}

    assert "plan.write" in names
    assert "workspace.read" in names
    assert "terminal.execute" not in names
    assert "proposal.create" not in names
    assert "design.share" not in names


async def test_plan_mode_rejects_terminal_execute_before_capability(apply_container) -> None:
    """Plan 模式下即使令牌有 terminal.execute 能力位，也必须被模式白名单先拦下。"""
    server = McpServer(apply_container)
    ctx = _ctx(apply_container, mode="plan")

    result = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "terminal.execute", "arguments": {"command": "echo hi"}},
        },
        ctx.identity,
    )

    payload = result["result"]
    assert payload["isError"] is True
    assert "Plan 模式（只读）" in payload["content"][0]["text"]


async def test_attach_run_id_carries_run_permission_mode(apply_container) -> None:
    agent_id = str(uuid.uuid4())
    run_id = uuid.uuid4().hex
    await apply_container.run_repo.create(
        run_id=run_id,
        session_id="sess",
        instruction="plan it",
        status=DshRunStatus.PENDING,
        agent_id=agent_id,
        permission_mode=PermissionMode.PLAN.value,
    )
    identity = AgentIdentity(agent_id=agent_id, token_id="t", scopes=frozenset())

    attached = await _attach_run_id(
        SimpleNamespace(headers={"x-flux-run-id": run_id}), apply_container, identity
    )

    assert attached.run_id == run_id
    assert attached.permission_mode == "plan"


# ---------------------------------------------------------------- 4. 审批闸门


async def _seed_task_and_run(container, *, mode: str = "normal"):
    task = await container.task_repo.create(
        task_id=uuid.uuid4(),
        description="审批闸门用例",
        priority=1,
        project_id=None,
        agent_id=None,
    )
    run_id = uuid.uuid4().hex
    await container.run_repo.create(
        run_id=run_id,
        session_id="sess",
        instruction="do it",
        status=DshRunStatus.PENDING,
        agent_id=str(uuid.uuid4()),
        task_id=task.id,
        permission_mode=mode,
    )
    return task, run_id


async def test_approval_allow_runs_the_command(apply_container, monkeypatch) -> None:
    task, run_id = await _seed_task_and_run(apply_container)
    ctx = _ctx(apply_container, mode="normal", run_id=run_id)

    async def fake_raise(container, **kwargs):
        return await raise_approval(container, **kwargs)

    monkeypatch.setattr(terminal_tools, "raise_approval", fake_raise)
    monkeypatch.setattr(
        terminal_tools, "wait_for_approval", lambda *a, **k: _allow(apply_container, task.id)
    )

    await terminal_tools._approve_if_needed(ctx, "rm -rf build")

    fresh = await apply_container.task_repo.get(task.id)
    assert fresh.status == TaskStatus.RUNNING.value
    record = fresh.decisions[-1]
    assert record["chosen"] == APPROVAL_ALLOW
    assert record["status"] == DecisionStatus.RESOLVED.value


async def _allow(container, task_id) -> str:
    """替身用户：把最近一条 pending 决策置为「允许」。"""
    task = await container.task_repo.get(task_id)
    record = task.decisions[-1]
    await container.task_repo.resolve_decision(
        task_id,
        record["id"],
        status=DecisionStatus.RESOLVED,
        chosen=APPROVAL_ALLOW,
        note=None,
        resolved_at="2026-10-25T00:00:00+00:00",
    )
    # 真实用户选择走 /decisions/choose，由接口把任务从等待态放回 running
    await container.task_repo.set_status(task_id, TaskStatus.RUNNING)
    return "allowed"


async def test_approval_denied_raises_and_does_not_run(apply_container, monkeypatch) -> None:
    task, run_id = await _seed_task_and_run(apply_container)
    ctx = _ctx(apply_container, mode="normal", run_id=run_id)

    async def deny(*args, **kwargs):
        return "denied"

    monkeypatch.setattr(
        terminal_tools, "raise_approval", lambda *a, **k: _raise_real(apply_container, **k)
    )
    monkeypatch.setattr(terminal_tools, "wait_for_approval", deny)

    with pytest.raises(ApprovalDeniedError):
        await terminal_tools._approve_if_needed(ctx, "rm -rf build")


async def _raise_real(container, **kwargs):
    return await raise_approval(container, **kwargs)


async def test_full_mode_skips_high_risk_but_asks_for_protected(
    apply_container, monkeypatch
) -> None:
    task, run_id = await _seed_task_and_run(apply_container, mode="full")
    ctx = _ctx(apply_container, mode="full", run_id=run_id)
    calls: list[str] = []

    async def fake_raise(container, **kwargs):
        calls.append(kwargs["kind"])
        return {"id": "d-1"}

    monkeypatch.setattr(terminal_tools, "raise_approval", fake_raise)
    monkeypatch.setattr(terminal_tools, "wait_for_approval", lambda *a, **k: _allowed())

    await terminal_tools._approve_if_needed(ctx, "rm -rf build")
    assert calls == []

    await terminal_tools._approve_if_needed(ctx, "systemctl restart nginx")
    assert calls == ["protected_access"]


async def _allowed() -> str:
    return "allowed"


async def test_unattended_mode_never_asks_even_for_protected(apply_container, monkeypatch) -> None:
    """免审核模式：高风险与受保护对象都不落审批卡，只留审计日志。"""
    task, run_id = await _seed_task_and_run(apply_container, mode="unattended")
    ctx = _ctx(apply_container, mode="unattended", run_id=run_id)
    calls: list[str] = []

    async def fake_raise(container, **kwargs):
        calls.append(kwargs["kind"])
        return {"id": "d-1"}

    monkeypatch.setattr(terminal_tools, "raise_approval", fake_raise)
    monkeypatch.setattr(terminal_tools, "wait_for_approval", lambda *a, **k: _allowed())

    await terminal_tools._approve_if_needed(ctx, "rm -rf build")
    await terminal_tools._approve_if_needed(ctx, "systemctl restart nginx")
    await terminal_tools._approve_if_needed(ctx, "cat /root/.ssh/id_rsa")

    assert calls == []
    fresh = await apply_container.task_repo.get(task.id)
    assert (fresh.decisions or []) == []


async def test_normal_mode_asks_for_high_risk(apply_container, monkeypatch) -> None:
    task, run_id = await _seed_task_and_run(apply_container)
    ctx = _ctx(apply_container, mode="normal", run_id=run_id)
    calls: list[str] = []

    async def fake_raise(container, **kwargs):
        calls.append(kwargs["kind"])
        return {"id": "d-1"}

    monkeypatch.setattr(terminal_tools, "raise_approval", fake_raise)
    monkeypatch.setattr(terminal_tools, "wait_for_approval", lambda *a, **k: _allowed())

    await terminal_tools._approve_if_needed(ctx, "ls -la")
    assert calls == []

    await terminal_tools._approve_if_needed(ctx, "npm install")
    assert calls == ["high_risk_command"]


async def test_protected_without_run_is_fail_closed(apply_container) -> None:
    ctx = _ctx(apply_container, mode="full", run_id=None)
    with pytest.raises(ConflictError):
        await terminal_tools._approve_if_needed(ctx, "systemctl restart nginx")


async def test_approval_timeout_expires_and_returns_timeout(apply_container) -> None:
    task, _ = await _seed_task_and_run(apply_container)
    record = await raise_approval(
        apply_container,
        task_id=task.id,
        run_id=None,
        kind="high_risk_command",
        title="高风险命令 · 需要你确认",
        detail="rm -rf build",
        meta={"command": "rm -rf build", "rule": "rm"},
    )
    waiting = await apply_container.task_repo.get(task.id)
    assert waiting.status == TaskStatus.WAITING_FOR_USER_DECISION.value

    outcome = await wait_for_approval(
        apply_container,
        task_id=task.id,
        decision_id=str(record["id"]),
        run_id=None,
        timeout_seconds=0.01,
    )

    assert outcome == "timeout"
    fresh = await apply_container.task_repo.get(task.id)
    assert fresh.status == TaskStatus.RUNNING.value
    expired = [d for d in fresh.decisions if d["id"] == record["id"]][0]
    assert expired["status"] == DecisionStatus.EXPIRED.value


async def test_approval_cancelled_when_task_ends(apply_container) -> None:
    task, _ = await _seed_task_and_run(apply_container)
    record = await raise_approval(
        apply_container,
        task_id=task.id,
        run_id=None,
        kind="protected_access",
        title="受保护操作 · 需要你确认",
        detail="systemctl restart nginx",
        meta={"command": "systemctl restart nginx"},
    )
    await apply_container.task_repo.set_status(task.id, TaskStatus.CANCELLED)

    outcome = await wait_for_approval(
        apply_container,
        task_id=task.id,
        decision_id=str(record["id"]),
        run_id=None,
        timeout_seconds=5,
    )
    assert outcome == "cancelled"


async def test_approval_record_is_manual_pending_even_in_auto_mode(apply_container) -> None:
    """auto 决策模式也不许替用户批准高风险命令：审批记录必须停在 pending。"""
    task, _ = await _seed_task_and_run(apply_container)
    await apply_container.task_repo.set_decision_mode(task.id, DecisionMode.AUTO)

    record = await raise_approval(
        apply_container,
        task_id=task.id,
        run_id=None,
        kind="high_risk_command",
        title="高风险命令 · 需要你确认",
        detail="rm -rf build",
        meta={"command": "rm -rf build"},
    )

    fresh = await apply_container.task_repo.get(task.id)
    stored = [d for d in fresh.decisions if d["id"] == record["id"]][0]
    assert stored["status"] == DecisionStatus.PENDING.value


# ---------------------------------------------------------------- 5. 计划文档


async def test_plan_write_lands_in_plans_dir_and_emits_card(
    apply_container, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(plan_tools, "PLANS_DIR", tmp_path / "plans")
    task, run_id = await _seed_task_and_run(apply_container, mode="plan")
    ctx = _ctx(apply_container, mode="plan", run_id=run_id)
    ctx = dataclasses.replace(ctx, tool_name="plan.write")

    data = await plan_tools.plan_write(
        ctx,
        {
            "title": "接通手机端权限模式",
            "steps": [
                {"title": "后端落模式字段", "detail": "迁移 + 接口"},
                {"title": "手机端模式胶囊"},
            ],
            "files": ["flux/api/v1/tasks.py"],
        },
    )

    plan_path = Path(data["plan_path"])
    assert plan_path.parent == tmp_path / "plans"
    assert plan_path.name == f"{task.id}.md"
    body = plan_path.read_text(encoding="utf-8")
    assert "# 接通手机端权限模式" in body
    assert "1. **后端落模式字段**" in body

    messages, _ = await apply_container.task_repo.list_messages(task.id)
    card = [m for m in messages if m.kind == "plan_card"][-1]
    assert card.payload["title"] == "接通手机端权限模式"
    assert len(card.payload["steps"]) == 2
    assert card.payload["plan_path"] == str(plan_path)


async def test_plan_write_requires_a_run(apply_container, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(plan_tools, "PLANS_DIR", tmp_path / "plans")
    ctx = _ctx(apply_container, mode="plan", run_id=None)
    ctx = dataclasses.replace(ctx, tool_name="plan.write")

    from flux.errors import ValidationError

    with pytest.raises(ValidationError):
        await plan_tools.plan_write(ctx, {"title": "x", "steps": [{"title": "y"}]})


# ---------------------------------------------------------------- 6. API 切换


def test_create_task_with_plan_mode_and_switch(client: TestClient) -> None:
    created = client.post(
        f"{PREFIX}/tasks",
        json={"description": "只出计划", "permission_mode": "plan"},
    )
    assert created.status_code == 200, created.text
    task = created.json()["data"]
    assert task["permission_mode"] == "plan"

    switched = client.post(f"{PREFIX}/tasks/{task['id']}/permission-mode", json={"mode": "full"})
    assert switched.status_code == 200, switched.text
    data = switched.json()["data"]
    assert data["permission_mode"] == "full"
    assert data["previous_mode"] == "plan"
    # pending 任务下一轮即生效
    assert data["effective_from"] == "next_message"

    read_back = client.get(f"{PREFIX}/tasks/{task['id']}").json()["data"]
    assert read_back["permission_mode"] == "full"


def test_switch_to_unattended_mode(client: TestClient) -> None:
    """免审核模式可被 API 接受，并记录切换来源。"""
    created = client.post(f"{PREFIX}/tasks", json={"description": "无人值守跑完"})
    task = created.json()["data"]

    switched = client.post(
        f"{PREFIX}/tasks/{task['id']}/permission-mode", json={"mode": "unattended"}
    )
    assert switched.status_code == 200, switched.text
    data = switched.json()["data"]
    assert data["permission_mode"] == "unattended"
    assert data["previous_mode"] == "normal"
    assert data["effective_from"] == "next_message"


def test_permission_mode_rejects_unknown_value(client: TestClient) -> None:
    created = client.post(f"{PREFIX}/tasks", json={"description": "模式校验"})
    task = created.json()["data"]
    response = client.post(f"{PREFIX}/tasks/{task['id']}/permission-mode", json={"mode": "yolo"})
    assert response.status_code == 422


# --- 运行时级只读：CLI 原生工具不走 Flux 闸门，Plan 必须由 runtime 自己关掉 ---


def test_opencode_config_disables_native_tools_in_plan_mode() -> None:
    """opencode 的 bash / edit / write 是 CLI 原生工具，Plan 模式必须在配置里直接 deny。"""
    cli = importlib.import_module("flux.core.agent_runtime.runtimes.cli")
    plan = json.loads(
        cli._opencode_config(
            "https://example.test/mcp", "tok", "run1", uploads_dir="/opt/flux/uploads", mode="plan"
        )
    )
    assert plan["permission"]["bash"] == "deny"
    assert plan["permission"]["edit"] == "deny"
    assert plan["permission"]["write"] == "deny"
    # 附件目录仍可读：只读模式照样要能看用户传的图片
    assert plan["permission"]["external_directory"] == {"/opt/flux/uploads/*": "allow"}

    normal = json.loads(
        cli._opencode_config("https://example.test/mcp", "tok", "run1", mode="normal")
    )
    # Normal / Full 的原生 bash 退回到 Flux 审批（ask），不再是绕过闸门的自由执行
    assert normal["permission"]["bash"] == "ask"
    for key in ("edit", "write"):
        assert normal.get("permission", {}).get(key) != "deny"
    full = json.loads(cli._opencode_config("https://example.test/mcp", "tok", "run1", mode="full"))
    assert full["permission"]["bash"] == "ask"


# --- opencode 原生 bash：permission.asked 走 Flux 审批，回复 once / reject ---


class _FakeOpenCodeClient:
    """记录审批回复的假 HTTP 客户端（只实现 permission 回复的一条路径）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url: str, json: dict | None = None, timeout: float | None = None):
        self.calls.append((url, dict(json or {})))
        return SimpleNamespace(status_code=200, text="true")


def _opencode_client(handler):
    server = importlib.import_module("flux.core.agent_runtime.runtimes.opencode_server")

    async def noop(*args, **kwargs):  # noqa: ANN002, ANN003 - 测试用空处理器
        return None

    client = server.OpenCodeServerClient(
        "http://127.0.0.1:1",
        on_text=noop,
        on_thinking=noop,
        on_tool=noop,
        on_step=noop,
        on_error=noop,
        approval_handler=handler,
    )
    fake = _FakeOpenCodeClient()
    client._client = fake  # noqa: SLF001 - 测试注入，不启真实 serve
    return client, fake


def _permission_event(command: str) -> dict:
    return {
        "type": "permission.asked",
        "properties": {
            "id": "per_test",
            "sessionID": "ses_test",
            "permission": "bash",
            "patterns": [command],
            "metadata": {"command": command},
        },
    }


async def test_opencode_permission_asked_allowed_replies_once() -> None:
    seen: list[str] = []

    async def handler(command: str) -> bool:
        seen.append(command)
        return True

    client, fake = _opencode_client(handler)
    await client._handle(_permission_event("echo ok"), "ses_test")  # noqa: SLF001
    assert seen == ["echo ok"]
    assert fake.calls == [
        ("/session/ses_test/permissions/per_test", {"response": "once"}),
    ]


async def test_opencode_permission_asked_rejected_replies_reject() -> None:
    async def handler(command: str) -> bool:
        return False

    client, fake = _opencode_client(handler)
    await client._handle(  # noqa: SLF001
        _permission_event("rm -rf /tmp/x"), "ses_test"
    )
    assert fake.calls == [
        ("/session/ses_test/permissions/per_test", {"response": "reject"}),
    ]


async def test_opencode_permission_without_handler_is_rejected() -> None:
    """没接审批回调时失败关闭：不能默认放行任何命令。"""
    client, fake = _opencode_client(None)
    await client._handle(_permission_event("echo ok"), "ses_test")  # noqa: SLF001
    assert fake.calls == [
        ("/session/ses_test/permissions/per_test", {"response": "reject"}),
    ]


async def test_opencode_permission_handler_error_is_rejected() -> None:
    async def handler(command: str) -> bool:
        raise RuntimeError("审批链挂了")

    client, fake = _opencode_client(handler)
    await client._handle(_permission_event("echo ok"), "ses_test")  # noqa: SLF001
    assert fake.calls == [
        ("/session/ses_test/permissions/per_test", {"response": "reject"}),
    ]


# --- CLI 原生 shell：codex 的 approvalPolicy=untrusted 把命令交上来，按同一套规则裁决 ---


async def _native(
    container, monkeypatch, *, command: str, mode: str, run_id: str
) -> tuple[bool, list[str]]:
    """跑一次原生审批；返回(是否放行, 被发起的审批 kind 列表)。"""
    cli = importlib.import_module("flux.core.agent_runtime.runtimes.cli")
    kinds: list[str] = []

    async def fake_raise(container, **kwargs):
        kinds.append(kwargs["kind"])
        return {"id": "d-native"}

    async def fake_wait(container, **kwargs):
        return "allowed"

    monkeypatch.setattr(cli, "raise_approval", fake_raise)
    monkeypatch.setattr(cli, "wait_for_approval", fake_wait)
    allowed = await cli.approve_native_command(
        container, runtime_id="codex", run_id=run_id, command=command, mode=mode
    )
    return allowed, kinds


async def test_native_low_risk_runs_without_asking(apply_container, monkeypatch) -> None:
    task, run_id = await _seed_task_and_run(apply_container)
    allowed, kinds = await _native(
        apply_container, monkeypatch, command="ls -la", mode="normal", run_id=run_id
    )
    assert allowed is True
    assert kinds == []


async def test_native_high_risk_in_full_runs_without_asking(apply_container, monkeypatch) -> None:
    task, run_id = await _seed_task_and_run(apply_container, mode="full")
    allowed, kinds = await _native(
        apply_container, monkeypatch, command="rm -rf /tmp/probe", mode="full", run_id=run_id
    )
    assert allowed is True
    assert kinds == []


async def test_native_high_risk_in_normal_asks_user(apply_container, monkeypatch) -> None:
    task, run_id = await _seed_task_and_run(apply_container)
    allowed, kinds = await _native(
        apply_container, monkeypatch, command="rm -rf /tmp/probe", mode="normal", run_id=run_id
    )
    assert allowed is True
    assert kinds == ["high_risk_command"]


async def test_native_protected_asks_even_in_full(apply_container, monkeypatch) -> None:
    task, run_id = await _seed_task_and_run(apply_container, mode="full")
    allowed, kinds = await _native(
        apply_container,
        monkeypatch,
        command="cat /root/.ssh/id_ed25519",
        mode="full",
        run_id=run_id,
    )
    assert allowed is True
    assert kinds == ["protected_access"]


async def test_native_unattended_never_asks(apply_container, monkeypatch) -> None:
    """免审核模式下 codex 原生 shell 的高风险与受保护命令都直接放行。"""
    task, run_id = await _seed_task_and_run(apply_container, mode="unattended")
    allowed, kinds = await _native(
        apply_container,
        monkeypatch,
        command="systemctl restart nginx",
        mode="unattended",
        run_id=run_id,
    )
    assert allowed is True
    assert kinds == []


async def test_native_plan_mode_never_allows(apply_container, monkeypatch) -> None:
    task, run_id = await _seed_task_and_run(apply_container, mode="plan")
    allowed, kinds = await _native(
        apply_container, monkeypatch, command="ls", mode="plan", run_id=run_id
    )
    assert allowed is False
    assert kinds == []


# ---------------------------------------------------------------- 7. 默认模式边界


def test_default_mode_has_no_direct_write_or_push_tools() -> None:
    """默认（normal）模式：Agent 没有直接写盘 / 提交 / 推送的工具；
    `git push` 与重定向写文件都属于高风险，必须走审批。"""
    from flux.core.mcp.tools import FORBIDDEN_TOOL_NAMES, TOOLS

    assert normalize_mode(None) == PermissionMode.NORMAL.value
    for name in ("workspace.apply", "git.commit", "git.push", "shell.exec", "secret.read"):
        assert name in FORBIDDEN_TOOL_NAMES
        assert name not in TOOLS
    assert classify_command("git push origin main").level == LEVEL_HIGH
    assert classify_command("echo hi > app.py").level == LEVEL_HIGH


async def test_default_mode_push_stops_for_approval(apply_container, monkeypatch) -> None:
    """默认模式下 `git push` 不会直接执行：先落审批卡，等用户点「允许」。"""
    task, run_id = await _seed_task_and_run(apply_container)
    ctx = _ctx(apply_container, mode=PermissionMode.NORMAL.value, run_id=run_id)
    calls: list[str] = []

    async def fake_raise(container, **kwargs):
        calls.append(kwargs["kind"])
        return {"id": "d-1"}

    monkeypatch.setattr(terminal_tools, "raise_approval", fake_raise)
    monkeypatch.setattr(terminal_tools, "wait_for_approval", lambda *a, **k: _allowed())

    await terminal_tools._approve_if_needed(ctx, "git push origin main")
    assert calls == ["high_risk_command"]


async def test_default_mode_cannot_call_forbidden_write_tools(apply_container) -> None:
    """即便令牌带全部能力位，默认模式也调不到落盘/推送工具（工具面根本不存在）。"""
    server = McpServer(apply_container)
    identity = _ctx(apply_container, mode=PermissionMode.NORMAL.value).identity
    result = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "workspace.apply", "arguments": {"change_id": "c-1"}},
        },
        identity,
    )
    payload = result["error"]
    assert payload["message"] == "未知工具：workspace.apply"
