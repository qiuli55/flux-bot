"""Flux MCP 能力面测试（目标架构 §3.2 / §3.3 / §3.5）。

覆盖三条硬判据：
1. `tools/list` 核心工具齐全（required ⊆ advertised），硬禁令工具不在面上；
2. 鉴权 fail-closed——无令牌、坏令牌、已撤销令牌一律 401，连 initialize 都进不来；
3. 越权不静默降级——能力不足是显式失败（isError），并留下 mcp.tool_denied 事件。

这里的令牌全部走真实签发路径（`/api/v1/agents/{id}/tokens`），不直接往库里塞哈希——
否则测的是测试自己造的假数据，而不是真实的签发/校验链路。
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient

from flux.core.agent_runtime.protocol import MODE_RULES, RUNTIME_ENTRY
from flux.core.mcp.auth import AgentIdentity
from flux.core.mcp.server import DEFAULT_PROTOCOL_VERSION, McpServer, _attach_run_id
from flux.core.mcp.tools import ALL_TOOLS, CORE_TOOL_NAMES, FORBIDDEN_TOOL_NAMES
from flux.core.mcp.tools.base import ToolContext
from flux.core.mcp.tools.write_tools import proposal_create
from flux.enums import Capability, DshRunStatus, StepStatus
from flux.errors import AuthenticationError

from .conftest import create_agent, issue_token

PROPOSAL = {
    "summary": "修复待办服务的两处缺陷",
    "changes": [
        {
            "path": "todo_service/store.py",
            "content": "def list_todos():\n    return []\n",
            "reason": "按完成状态过滤",
        }
    ],
}


def rpc(client: TestClient, token: str | None, method: str, params: dict | None = None):
    body: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return client.post("/mcp", json=body, headers=headers)


def call(client: TestClient, token: str, name: str, arguments: dict):
    response = rpc(client, token, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def payload(result: dict) -> dict:
    assert result["isError"] is False, result["content"][0]["text"]
    return json.loads(result["content"][0]["text"])


# --- 1. 工具面 ---


def test_tools_list_exposes_the_core_tools(mcp_client: TestClient) -> None:
    """核心工具必须齐全（required ⊆ advertised），但不要求精确相等：新增工具不该被误杀（§5）。"""
    token = issue_token(mcp_client)
    result = rpc(mcp_client, token, "tools/list").json()["result"]
    names = {tool["name"] for tool in result["tools"]}
    assert names >= CORE_TOOL_NAMES
    assert names & FORBIDDEN_TOOL_NAMES == set()
    for tool in result["tools"]:
        assert tool["description"] and tool["inputSchema"]["type"] == "object"


def test_forbidden_capabilities_are_absent_from_the_surface(mcp_client: TestClient) -> None:
    """§3.5 硬禁令：apply / git.push / secret / shell 不是"忘了注册"，是没有开关能打开。"""
    listed = {tool.name for tool in ALL_TOOLS}
    assert listed & FORBIDDEN_TOOL_NAMES == set()
    token = issue_token(
        mcp_client, scopes=[c for c in Capability if c is not Capability.SECRET_ACCESS]
    )
    result = rpc(mcp_client, token, "tools/list").json()["result"]
    assert {tool["name"] for tool in result["tools"]} & FORBIDDEN_TOOL_NAMES == set()


def test_every_tool_declares_a_capability_and_a_handler() -> None:
    for tool in ALL_TOOLS:
        assert isinstance(tool.capability, Capability)
        assert callable(tool.handler)


# --- 终端执行工具（Agent Terminal Console §4 / §5 / §9）---


def test_terminal_execute_runs_through_session_and_returns_summary(
    mcp_client: TestClient,
) -> None:
    """Agent 命令走 Flux 的 Terminal Session：命令与输出落成终端事件（source=ai），
    回给 Agent 的只是摘要——完整输出不回灌 Agent Context（§9）。"""
    token = issue_token(
        mcp_client,
        scopes=[Capability.FILE_READ, Capability.FILE_WRITE, Capability.TERMINAL_EXECUTE],
    )

    result = call(mcp_client, token, "terminal.execute", {"command": "echo agent-here"})
    payload = json.loads(result["content"][0]["text"])

    assert payload["exit_code"] == 0
    assert payload["succeeded"] is True
    assert "agent-here" in payload["output_summary"]
    assert payload["output_truncated"] is False

    events = mcp_client.get(f"/api/v1/terminal/sessions/{payload['session_id']}/events").json()[
        "data"
    ]
    kinds = [item["kind"] for item in events]
    assert "terminal.command.started" in kinds
    assert any(item["source"] == "ai" for item in events)
    assert any("agent-here" in (item["chunk"] or "") for item in events)


def test_terminal_execute_requires_the_capability(mcp_client: TestClient) -> None:
    """没有 terminal.execute 能力的令牌被明确拒绝（越权不静默降级，§3.5）。"""
    token = issue_token(mcp_client, scopes=[Capability.FILE_READ])

    result = call(mcp_client, token, "terminal.execute", {"command": "echo nope"})

    assert result["isError"] is True


# --- 2. 鉴权 fail-closed ---


def test_every_method_requires_a_token(mcp_client: TestClient) -> None:
    for method in ("initialize", "tools/list", "ping"):
        response = rpc(mcp_client, None, method, {})
        assert response.status_code == 401, method
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json()["code"] == AuthenticationError.code


def test_malformed_and_unknown_tokens_are_rejected(mcp_client: TestClient) -> None:
    assert rpc(mcp_client, "not-a-token", "tools/list").status_code == 401
    assert rpc(mcp_client, "fxt_" + "0" * 64, "tools/list").status_code == 401


def test_revoked_token_stops_working_on_the_next_request(mcp_client: TestClient) -> None:
    codex_id = create_agent(mcp_client, name="codex")
    token = issue_token(mcp_client, agent_id="codex")
    assert rpc(mcp_client, token, "tools/list").status_code == 200

    listed = mcp_client.get(f"/api/v1/agents/{codex_id}/tokens").json()["data"]
    assert (
        mcp_client.delete(f"/api/v1/agents/{codex_id}/tokens/{listed[0]['id']}").status_code == 200
    )

    response = rpc(mcp_client, token, "tools/list")
    assert response.status_code == 401
    assert "无效或已撤销" in response.json()["message"]


def test_token_endpoints_never_expose_the_hash(mcp_client: TestClient) -> None:
    codex_id = create_agent(mcp_client, name="codex")
    issued = mcp_client.post(
        f"/api/v1/agents/{codex_id}/tokens", json={"scopes": ["file.read"], "label": "本地调试"}
    ).json()["data"]
    assert issued["token"].startswith("fxt_")
    assert "token_hash" not in issued
    listed = mcp_client.get(f"/api/v1/agents/{codex_id}/tokens").json()["data"][0]
    assert "token_hash" not in listed and "token" not in listed
    assert listed["label"] == "本地调试" and listed["revoked"] is False


def test_secret_scope_can_never_be_granted(mcp_client: TestClient) -> None:
    codex_id = create_agent(mcp_client, name="codex")
    response = mcp_client.post(
        f"/api/v1/agents/{codex_id}/tokens", json={"scopes": ["secret.access"]}
    )
    assert response.status_code == 422
    assert "不允许授予" in response.json()["message"]


def test_unknown_scope_is_rejected(mcp_client: TestClient) -> None:
    """未知能力在入参校验层就被挡下（pydantic + service 两道都不放行）。"""
    codex_id = create_agent(mcp_client, name="codex")
    response = mcp_client.post(
        f"/api/v1/agents/{codex_id}/tokens", json={"scopes": ["file.destroy"]}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


# --- 3. 协议层 ---


def test_initialize_echoes_supported_version_and_falls_back(mcp_client: TestClient) -> None:
    token = issue_token(mcp_client)
    result = rpc(mcp_client, token, "initialize", {"protocolVersion": "2025-03-26"}).json()[
        "result"
    ]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["serverInfo"]["name"] == "flux"
    assert result["capabilities"]["tools"]["listChanged"] is False

    fresh = rpc(mcp_client, token, "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]
    assert fresh["protocolVersion"] == DEFAULT_PROTOCOL_VERSION


def test_notification_gets_no_body(mcp_client: TestClient) -> None:
    token = issue_token(mcp_client)
    response = mcp_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 202
    assert response.content == b""


def test_protocol_errors_for_unknown_method_tool_and_batch(mcp_client: TestClient) -> None:
    token = issue_token(mcp_client)
    unknown = rpc(mcp_client, token, "tools/apply_everything").json()["error"]
    assert unknown["code"] == -32601

    missing = rpc(mcp_client, token, "tools/call", {"name": "workspace.apply", "arguments": {}})
    assert missing.json()["error"]["code"] == -32602
    assert "未知工具" in missing.json()["error"]["message"]

    batch = mcp_client.post(
        "/mcp",
        json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
        headers={"Authorization": f"Bearer {token}"},
    )
    assert batch.json()["error"]["code"] == -32600

    broken = mcp_client.post(
        "/mcp", content=b"{not json", headers={"Authorization": f"Bearer {token}"}
    )
    assert broken.json()["error"]["code"] == -32700


def test_unknown_argument_is_rejected_instead_of_ignored(mcp_client: TestClient) -> None:
    token = issue_token(mcp_client, scopes=[Capability.FILE_READ])
    result = call(mcp_client, token, "workspace.read", {"path": "a.py", "sudo": True})
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "未定义的入参" in text
    # 报错要给出合法入参名，否则模型只能靠猜（基准里 `workdir` 这类拼写就反复失败）
    assert "path" in text


# --- 4. 只读工具 ---


def test_workspace_read_returns_content_and_hash(mcp_client: TestClient, workspace_root) -> None:
    (workspace_root / "hello.py").write_text("print('hi')\n", encoding="utf-8", newline="\n")
    token = issue_token(mcp_client, scopes=[Capability.FILE_READ])
    data = payload(call(mcp_client, token, "workspace.read", {"path": "hello.py"}))
    assert data["content"] == "print('hi')\n"
    assert data["truncated"] is False and data["size"] == len("print('hi')\n")
    # sha256 必须与文件真实内容一致，否则人审时拿它核验就是个摆设
    import hashlib

    assert data["sha256"] == hashlib.sha256(b"print('hi')\n").hexdigest()


def test_workspace_read_lists_directory_instead_of_rejecting_dot(
    mcp_client: TestClient, workspace_root
) -> None:
    """目录也是合法读取目标：`.` 返回条目清单，Agent 不会在这类探索上反复试错。"""
    (workspace_root / "pkg").mkdir()
    (workspace_root / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    token = issue_token(mcp_client, scopes=[Capability.FILE_READ])
    data = payload(call(mcp_client, token, "workspace.read", {"path": "."}))
    assert data["kind"] == "dir"
    assert data["path"] == "."
    names = {entry["name"] for entry in data["entries"]}
    assert "pkg" in names
    assert next(entry for entry in data["entries"] if entry["name"] == "pkg")["kind"] == "dir"


def test_workspace_tree_returns_a_compact_map(mcp_client: TestClient, workspace_root) -> None:
    """探索面：一次调用拿到多层目录地图，避免逐层 read(".") 的往返。"""
    (workspace_root / "pkg").mkdir()
    (workspace_root / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    token = issue_token(mcp_client, scopes=[Capability.FILE_READ])
    data = payload(call(mcp_client, token, "workspace.tree", {"path": ".", "depth": 2}))
    paths = [entry["path"] for entry in data["entries"]]
    assert any(path.endswith("pkg/mod.py") for path in paths)
    assert data["truncated"] is False


def test_workspace_read_many_reads_files_and_reports_per_item_errors(
    mcp_client: TestClient, workspace_root
) -> None:
    """单条失败不拖垮整批：批量读取要带回每个路径的结果/原因，Agent 才能继续。"""
    (workspace_root / "a.py").write_text("a = 1\n", encoding="utf-8", newline="\n")
    (workspace_root / "b.py").write_text("b = 2\n", encoding="utf-8", newline="\n")
    token = issue_token(mcp_client, scopes=[Capability.FILE_READ])
    data = payload(
        call(
            mcp_client,
            token,
            "workspace.read_many",
            {"paths": ["a.py", "missing.py", "b.py"], "max_chars": 1000},
        )
    )
    items = {item["path"]: item for item in data["items"]}
    assert items["a.py"]["content"] == "a = 1\n"
    assert items["b.py"]["content"] == "b = 2\n"
    assert items["missing.py"]["error"]
    assert data["count"] == 3


def test_workspace_read_many_rejects_more_than_ten_paths(mcp_client: TestClient) -> None:
    """超量不是静默截断：报错要告诉 Agent 拆成多次调用，否则它只会原地重试。"""
    token = issue_token(mcp_client, scopes=[Capability.FILE_READ])
    result = call(
        mcp_client,
        token,
        "workspace.read_many",
        {"paths": [f"f{i}.py" for i in range(11)]},
    )
    assert result["isError"] is True
    assert "拆成多次" in result["content"][0]["text"]


def test_workspace_read_many_clips_large_files(mcp_client: TestClient, workspace_root) -> None:
    (workspace_root / "big.py").write_text("x" * 5000, encoding="utf-8")
    token = issue_token(mcp_client, scopes=[Capability.FILE_READ])
    data = payload(
        call(mcp_client, token, "workspace.read_many", {"paths": ["big.py"], "max_chars": 300})
    )
    item = data["items"][0]
    assert len(item["content"]) == 300 and item["truncated"] is True
    assert "workspace.read" in item["next_hint"]


def test_workspace_search_finds_text_and_filters_secrets(
    mcp_client: TestClient, workspace_root
) -> None:
    """搜索是独立实现，不走 read 的守卫，必须在工具层补密钥过滤（fail-closed）。"""
    (workspace_root / "app.py").write_text("def needle():\n    return 1\n", encoding="utf-8")
    (workspace_root / ".env").write_text("NEEDLE=secret\n", encoding="utf-8")
    token = issue_token(mcp_client, scopes=[Capability.FILE_READ])
    data = payload(call(mcp_client, token, "workspace.search", {"query": "needle"}))
    paths = {hit["path"] for hit in data["hits"]}
    assert "app.py" in paths
    assert ".env" not in paths
    assert data["filtered_secrets"] >= 1
    assert all(hit["line"] >= 1 for hit in data["hits"])


def test_workspace_exploration_tools_refuse_internal_directories(
    mcp_client: TestClient, workspace_root
) -> None:
    internal = workspace_root / ".flux" / "backups"
    internal.mkdir(parents=True)
    (internal / "store.py.bak").write_text("x", encoding="utf-8")
    token = issue_token(mcp_client, scopes=[Capability.FILE_READ])
    for name, arguments in (
        ("workspace.tree", {"path": ".flux"}),
        ("workspace.search", {"query": "x", "path": ".flux"}),
    ):
        result = call(mcp_client, token, name, arguments)
        assert result["isError"] is True, name


def test_workspace_read_refuses_to_escape_the_root(mcp_client: TestClient, workspace_root) -> None:
    token = issue_token(mcp_client, scopes=[Capability.FILE_READ])
    for path in ("../outside.py", "/etc/passwd"):
        result = call(mcp_client, token, "workspace.read", {"path": path})
        assert result["isError"] is True, path


def test_context_get_packages_task_and_brain(mcp_client: TestClient) -> None:
    project = mcp_client.post(
        "/api/v1/projects", json={"name": "待办服务", "repository": "/opt/flux/mcp-demo-task"}
    ).json()["data"]
    mcp_client.post(
        f"/api/v1/projects/{project['id']}/memory",
        json={"section": "coding_rules", "content": "测试命令固定为 python3 -m pytest"},
    )
    task = mcp_client.post(
        "/api/v1/tasks",
        json={
            "description": "修掉 store.list 与 store.remove 的两处缺陷",
            "project_id": project["id"],
        },
    ).json()["data"]

    token = issue_token(mcp_client, scopes=[Capability.FILE_READ])
    data = payload(call(mcp_client, token, "context.get", {"task_id": task["id"]}))

    assert data["task_id"] == task["id"] and data["project_id"] == project["id"]
    assert "修掉 store.list" in data["content"]
    assert "python3 -m pytest" in data["content"]
    assert data["used"] <= data["budget"]
    assert [entry["kind"] for entry in data["entries"]] == ["task", "brain"]
    assert data["dropped_count"] == 0


def test_context_get_degrades_to_reference_when_budget_is_tight(mcp_client: TestClient) -> None:
    mcp_client.post("/api/v1/projects", json={"name": "预算紧张的项目", "repository": None}).json()[
        "data"
    ]
    token = issue_token(mcp_client, scopes=[Capability.FILE_READ])
    data = payload(call(mcp_client, token, "context.get", {"budget": 32}))
    assert data["used"] <= 32


def test_context_get_rejects_bad_uuid(mcp_client: TestClient) -> None:
    token = issue_token(mcp_client, scopes=[Capability.FILE_READ])
    result = call(mcp_client, token, "context.get", {"task_id": "12345"})
    assert result["isError"] is True
    assert "必须是合法 UUID" in result["content"][0]["text"]


# --- 5. 写入工具：proposal.create ---


def test_proposal_create_enqueues_for_review_and_stamps_the_agent(
    mcp_client: TestClient, workspace_root
) -> None:
    (workspace_root / "todo_service").mkdir()
    (workspace_root / "todo_service" / "store.py").write_text(
        "def list_todos():\n    return None\n", encoding="utf-8", newline="\n"
    )
    codex_id = create_agent(mcp_client, name="codex-minimax")
    token = issue_token(
        mcp_client, agent_id="codex-minimax", scopes=[Capability.FILE_READ, Capability.FILE_WRITE]
    )

    data = payload(call(mcp_client, token, "proposal.create", {"payload": PROPOSAL}))
    assert data["count"] == 1
    # P3-16：provenance 落的是 canonical UUID，名字只是 display_name
    assert data["agent"] == codex_id
    change = data["changes"][0]
    assert change["file_path"] == "todo_service/store.py"
    assert change["status"] == "pending"
    assert (change["added_lines"], change["removed_lines"], change["hunks"]) == (1, 1, 1)

    # 人审队列里能查到，且 provider 端盖章的 agent 与 provenance 一致
    stored = mcp_client.get(f"/api/v1/workspace/changes/{change['change_id']}").json()["data"]
    assert stored["agent_source"] == codex_id
    assert stored["original_content"] == "def list_todos():\n    return None\n"
    # 提案绝不落盘（§3.5）：磁盘上还是旧内容
    assert (workspace_root / "todo_service" / "store.py").read_text() == (
        "def list_todos():\n    return None\n"
    )

    diff = payload(call(mcp_client, token, "workspace.diff", {"change_id": change["change_id"]}))
    assert diff["diff"].startswith("---") and diff["status"] == "pending"


def test_proposal_create_accepts_a_json_string_and_new_files(
    mcp_client: TestClient, workspace_root
) -> None:
    token = issue_token(mcp_client, scopes=[Capability.FILE_READ, Capability.FILE_WRITE])
    raw = json.dumps(
        {
            "summary": "新增模块",
            "changes": [
                {"path": "todo_service/__init__.py", "content": "from .store import TodoStore\n"}
            ],
        },
        ensure_ascii=False,
    )
    data = payload(call(mcp_client, token, "proposal.create", {"payload": raw}))
    assert data["changes"][0]["file_path"] == "todo_service/__init__.py"
    assert data["changes"][0]["added_lines"] == 1


def test_proposal_create_ignores_client_supplied_agent_identity(
    mcp_client: TestClient, workspace_root
) -> None:
    """客户端不能自报身份：payload 里写 agent 也不作数，provenance 只认令牌。"""
    codex_id = create_agent(mcp_client, name="codex")
    token = issue_token(
        mcp_client, agent_id="codex", scopes=[Capability.FILE_READ, Capability.FILE_WRITE]
    )
    spoofed = {**PROPOSAL, "agent": "flux-builtin", "agent_id": "flux-builtin"}
    data = payload(call(mcp_client, token, "proposal.create", {"payload": spoofed}))
    stored = mcp_client.get(f"/api/v1/workspace/changes/{data['changes'][0]['change_id']}").json()[
        "data"
    ]
    assert stored["agent_source"] == codex_id


def test_proposal_create_rejects_declared_agent_that_is_not_the_token_identity(
    mcp_client: TestClient, workspace_root
) -> None:
    """P3-16 §4.4 / TC-16C：请求声明的 agent_id 与令牌不一致 → 直接拒绝，提案不入队。"""
    create_agent(mcp_client, name="agent-a")
    other_id = create_agent(mcp_client, name="agent-b")
    token = issue_token(
        mcp_client, agent_id="agent-a", scopes=[Capability.FILE_READ, Capability.FILE_WRITE]
    )
    result = call(
        mcp_client,
        token,
        "proposal.create",
        {"payload": PROPOSAL, "agent_id": other_id},
    )
    assert result["isError"] is True
    assert "冒充" in result["content"][0]["text"]
    assert mcp_client.get("/api/v1/workspace/changes").json()["data"] == []


def test_proposal_create_rejects_path_escape(mcp_client: TestClient) -> None:
    token = issue_token(mcp_client, scopes=[Capability.FILE_READ, Capability.FILE_WRITE])
    bad = {"summary": "越界", "changes": [{"path": "../outside.py", "content": "x\n"}]}
    result = call(mcp_client, token, "proposal.create", {"payload": bad})
    assert result["isError"] is True
    assert "不得越出项目根" in result["content"][0]["text"]


def test_proposal_create_without_change_is_reported_not_silently_accepted(
    mcp_client: TestClient, workspace_root
) -> None:
    (workspace_root / "same.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    token = issue_token(mcp_client, scopes=[Capability.FILE_READ, Capability.FILE_WRITE])
    same = {"summary": "无改动", "changes": [{"path": "same.py", "content": "x = 1\n"}]}
    result = call(mcp_client, token, "proposal.create", {"payload": same})
    assert result["isError"] is True
    assert "没有任何有效改动" in result["content"][0]["text"]


# --- 6. 越权：显式失败 + 留痕 ---


def test_read_only_token_cannot_create_proposals_and_leaves_an_event(
    mcp_client: TestClient,
) -> None:
    observer_id = create_agent(mcp_client, name="observer", role="reviewer")
    token = issue_token(mcp_client, agent_id="observer", scopes=[Capability.FILE_READ])
    result = call(mcp_client, token, "proposal.create", {"payload": PROPOSAL})
    assert result["isError"] is True
    assert "权限不足" in result["content"][0]["text"]
    assert "file.write" in result["content"][0]["text"]

    bus = mcp_client.app.state.container.bus
    denied = [payload_ for event, payload_ in bus.history if event == "mcp.tool_denied"]
    assert denied and denied[-1]["agent_id"] == observer_id
    assert denied[-1]["capability"] == "file.write"
    # 被拒的提案没有进审核队列
    assert mcp_client.get("/api/v1/workspace/changes").json()["data"] == []


# --- 7. 密钥与 Flux 内部数据（§3.5 硬禁令 / §7 安全边界）---


def test_workspace_read_never_returns_secret_files(mcp_client: TestClient, workspace_root) -> None:
    """Secret 读取是硬禁令：`.env` / 私钥一律 403，不因为"只是读一下"而放行。"""
    (workspace_root / ".env").write_text("DEEPSEEK_API_KEY=sk-真实密钥\n", encoding="utf-8")
    (workspace_root / "deploy.key").write_text("PRIVATE KEY\n", encoding="utf-8")
    (workspace_root / "tls.pem").write_text("CERT\n", encoding="utf-8")
    secrets_dir = workspace_root / "config"
    secrets_dir.mkdir()
    (secrets_dir / "id_rsa").write_text("ssh-rsa\n", encoding="utf-8")

    token = issue_token(mcp_client, scopes=[Capability.FILE_READ])
    for path in (".env", "deploy.key", "tls.pem", "config/id_rsa"):
        result = call(mcp_client, token, "workspace.read", {"path": path})
        assert result["isError"] is True, path
        text = result["content"][0]["text"]
        assert "permission_denied" in text and "密钥" in text, path


def test_workspace_read_allows_secret_templates(mcp_client: TestClient, workspace_root) -> None:
    """`.env.example` 这类模板没有真实密钥，读得到（否则新人没法照着填）。"""
    (workspace_root / ".env.example").write_text(
        "DEEPSEEK_API_KEY=\n", encoding="utf-8", newline="\n"
    )
    token = issue_token(mcp_client, scopes=[Capability.FILE_READ])
    data = payload(call(mcp_client, token, "workspace.read", {"path": ".env.example"}))
    assert data["content"] == "DEEPSEEK_API_KEY=\n"


def test_workspace_read_refuses_flux_internal_files(mcp_client: TestClient, workspace_root) -> None:
    """`.flux/` 是平台内部目录（备份/锁）：Agent 读不到，也就谈不上拿备份绕过审核。"""
    internal = workspace_root / ".flux" / "backups"
    internal.mkdir(parents=True)
    (internal / "store.py.bak").write_text("旧内容\n", encoding="utf-8")

    token = issue_token(mcp_client, scopes=[Capability.FILE_READ])
    result = call(mcp_client, token, "workspace.read", {"path": ".flux/backups/store.py.bak"})
    assert result["isError"] is True
    assert "validation_error" in result["content"][0]["text"]


def test_proposal_create_refuses_secrets_and_flux_internal_paths(
    mcp_client: TestClient, workspace_root
) -> None:
    """提案同样不得触碰密钥与 `.flux/`：前者的禁令与读取同源，后者是回滚依据。"""
    token = issue_token(mcp_client, scopes=[Capability.FILE_READ, Capability.FILE_WRITE])
    for path, code in ((".env", "permission_denied"), (".flux/fake.bak", "validation_error")):
        bad = {"summary": "越权", "changes": [{"path": path, "content": "x\n"}]}
        result = call(mcp_client, token, "proposal.create", {"payload": bad})
        assert result["isError"] is True, path
        assert code in result["content"][0]["text"], path
    # 被拒的提案一条都不进审核队列
    assert mcp_client.get("/api/v1/workspace/changes").json()["data"] == []


def test_successful_calls_are_recorded_without_arguments(
    mcp_client: TestClient, workspace_root
) -> None:
    """事件里不能出现入参——proposal 入参可能带整份文件内容。"""
    (workspace_root / "a.py").write_text("a = 1\n", encoding="utf-8")
    token = issue_token(mcp_client, scopes=[Capability.FILE_READ])
    call(mcp_client, token, "workspace.read", {"path": "a.py"})
    bus = mcp_client.app.state.container.bus
    recorded = [payload_ for event, payload_ in bus.history if event == "mcp.tool_called"]
    assert recorded[-1]["tool"] == "workspace.read"
    assert "arguments" not in recorded[-1] and "a = 1" not in json.dumps(recorded[-1])


# --- 8. 统一身份：Token 只能绑定真实 Agent（P3-16 §4 / TC-16B、TC-16D）---


def test_token_cannot_be_issued_for_an_unregistered_agent(mcp_client: TestClient) -> None:
    """TC-16B：签发边界就挡下——不存在的 Agent UUID 拿不到令牌，且不为它建档案。"""
    fake_id = "0f5b6f4c-0000-0000-0000-000000000000"
    response = mcp_client.post(f"/api/v1/agents/{fake_id}/tokens", json={"scopes": ["file.read"]})
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert all(agent["id"] != fake_id for agent in mcp_client.get("/api/v1/agents").json()["data"])


def test_token_bound_to_a_vanished_agent_is_rejected(mcp_client: TestClient) -> None:
    """TC-16B：库里存在但注册表解析不到的令牌，鉴权 fail-closed → 401，绝不降级放行。"""
    import hashlib
    import sqlite3
    import uuid

    raw = "fxt_" + "a" * 64
    db_path = mcp_client.app.state.container.settings.database_url.split("///", 1)[1]
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT INTO agent_tokens (id, agent_id, label, token_hash, scopes, revoked_at)"
            " VALUES (?, ?, ?, ?, ?, NULL)",
            (
                str(uuid.uuid4()),
                "0f5b6f4c-0000-0000-0000-000000000000",  # 合法 UUID，但注册表里没有
                "野令牌",
                hashlib.sha256(raw.encode()).hexdigest(),
                json.dumps(["file.read"]),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    response = rpc(mcp_client, raw, "tools/list")
    assert response.status_code == 401
    assert "canonical" in response.json()["message"]


def test_proposals_carry_their_own_agent_attribution(
    mcp_client: TestClient, workspace_root
) -> None:
    """TC-16D：两个 Agent 各提一个提案，provenance 各归其主（可追溯到 Agent → Proposal）。"""
    dsh_id = create_agent(mcp_client, name="dsh-agent")
    codex_id = create_agent(mcp_client, name="codex-agent")
    grants = [Capability.FILE_READ, Capability.FILE_WRITE]
    dsh_token = issue_token(mcp_client, agent_id="dsh-agent", scopes=grants)
    codex_token = issue_token(mcp_client, agent_id="codex-agent", scopes=grants)

    first = payload(
        call(
            mcp_client,
            dsh_token,
            "proposal.create",
            {
                "payload": {
                    "summary": "DSH 的提案",
                    "changes": [{"path": "a.py", "content": "a = 1\n"}],
                }
            },
        )
    )
    second = payload(
        call(
            mcp_client,
            codex_token,
            "proposal.create",
            {
                "payload": {
                    "summary": "Codex 的提案",
                    "changes": [{"path": "b.py", "content": "b = 2\n"}],
                }
            },
        )
    )

    def _stored(change_id: str) -> dict:
        return mcp_client.get(f"/api/v1/workspace/changes/{change_id}").json()["data"]

    assert _stored(first["changes"][0]["change_id"])["agent_source"] == dsh_id
    assert _stored(second["changes"][0]["change_id"])["agent_source"] == codex_id


# --- 6. flux_context（握手 v1 + 能力发现，§6 / §7 / §8）---


def test_flux_context_reports_runtime_and_capabilities(mcp_client: TestClient) -> None:
    token = issue_token(mcp_client)
    data = payload(call(mcp_client, token, "flux_context", {}))
    # 握手回执：MCP initialize + flux_context = Handshake v1（§6）
    assert data["protocol"] == {"name": "flux-agent", "version": "1"}
    assert data["platform"]["name"] == "Flux"
    assert data["platform"]["mode"] == "personal"
    # 平台规则由 Flux 下发，不是 Agent 自选（§13）
    assert data["policy"] == {"proposal_required": True, "direct_apply": False}
    # 工作区根已在 mcp_client 夹具里配置 ⇒ workspace/apply/git 可用
    capabilities = set(data["capabilities"])
    assert {"context", "workspace", "proposal", "apply", "git"} <= capabilities
    assert "rollback" not in capabilities  # 未实现的能力不虚报
    assert data["workspace"]["path"]
    # 批次①：rules / entry 与 Runtime Bootstrap 同一份真源（§4.1 契约），不是各写各的
    # 无 Run 上下文时模式按 normal 兜底，规则即 Normal 模式的完整规则段
    assert data["rules"] == list(MODE_RULES["normal"])
    assert data["entry"] == list(RUNTIME_ENTRY)


def test_flux_context_without_run_returns_null_run_and_task(mcp_client: TestClient) -> None:
    token = issue_token(mcp_client)
    data = payload(call(mcp_client, token, "flux_context", {}))
    assert data["run"] is None
    assert data["task"] is None


def test_flux_context_accepts_project_id_from_execution_prompt(mcp_client: TestClient) -> None:
    """执行指令会让 Agent 同时带上 task_id/project_id；多余的项目标识不能拒绝握手。"""
    token = issue_token(mcp_client)
    data = payload(call(mcp_client, token, "flux_context", {"project_id": str(uuid.uuid4())}))
    assert data["protocol"] == {"name": "flux-agent", "version": "1"}


def test_flux_context_rejects_malformed_task_id(mcp_client: TestClient) -> None:
    token = issue_token(mcp_client)
    result = call(mcp_client, token, "flux_context", {"task_id": "not-a-uuid"})
    assert result["isError"] is True


def test_context_boot_returns_runtime_context_and_memory_in_one_call(
    mcp_client: TestClient,
) -> None:
    """开工只调一次：runtime / context / memory 三份内容一次拿齐，且与单项工具同源。"""
    project = mcp_client.post(
        "/api/v1/projects", json={"name": "开工聚合项目", "repository": None}
    ).json()["data"]
    mcp_client.post(
        f"/api/v1/projects/{project['id']}/memory",
        json={"section": "coding_rules", "content": "提交信息用中文"},
    )
    task = mcp_client.post(
        "/api/v1/tasks",
        json={"description": "补一个开工入口", "project_id": project["id"]},
    ).json()["data"]

    token = issue_token(mcp_client, scopes=[Capability.FILE_READ])
    data = payload(call(mcp_client, token, "context.boot", {"task_id": task["id"]}))

    # runtime = flux_context：握手回执 + 任务视图
    assert data["runtime"]["protocol"] == {"name": "flux-agent", "version": "1"}
    assert data["runtime"]["task"]["id"] == task["id"]
    assert data["runtime"]["entry"] == list(RUNTIME_ENTRY)
    # context = context.get：按任务打包项目事实
    assert data["context"]["task_id"] == task["id"]
    assert data["context"]["project_id"] == project["id"]
    # memory = memory.recall：Project 层随任务推导
    assert data["memory"]["project_id"] == project["id"]
    assert [layer["layer"] for layer in data["memory"]["layers"]][-1] == "project"

    # 单份刷新仍走旧工具，内容口径一致
    single = payload(call(mcp_client, token, "context.get", {"task_id": task["id"]}))
    assert single["content"] == data["context"]["content"]


def test_context_boot_rejects_bad_budget(mcp_client: TestClient) -> None:
    token = issue_token(mcp_client)
    result = call(mcp_client, token, "context.boot", {"budget": "many"})
    assert result["isError"] is True
    assert "budget" in result["content"][0]["text"]


# --- 5. X-Flux-Run-Id：把 MCP 调用绑到发起它的 Run（FINDING-1） ---


def _header_request(run_id: str | None) -> SimpleNamespace:
    headers = {"x-flux-run-id": run_id} if run_id is not None else {}
    return SimpleNamespace(headers=headers)


def _task_header_request(task_id: str | None) -> SimpleNamespace:
    headers = {"x-flux-task-id": task_id} if task_id is not None else {}
    return SimpleNamespace(headers=headers)


async def _seed_run(container, *, run_id: str, agent_id: str) -> None:
    await container.run_repo.create(
        run_id=run_id,
        session_id="sess",
        instruction="do it",
        status=DshRunStatus.PENDING,
        agent_id=agent_id,
    )


async def test_attach_run_id_accepts_run_owned_by_identity(apply_container) -> None:
    """归属本 Agent 的 Run：run_id 被附加到身份上（终端会话才能绑到 Run）。"""
    agent_id = str(uuid.uuid4())
    run_id = uuid.uuid4().hex
    await _seed_run(apply_container, run_id=run_id, agent_id=agent_id)

    identity = AgentIdentity(agent_id=agent_id, token_id="t", scopes=frozenset())
    result = await _attach_run_id(_header_request(run_id), apply_container, identity)
    assert result.run_id == run_id


async def test_attach_run_id_ignores_foreign_unknown_or_absent_run(apply_container) -> None:
    """非权威头：归属不匹配、Run 不存在、或没带这个头，一律静默忽略（不 401）。"""
    owner = str(uuid.uuid4())
    run_id = uuid.uuid4().hex
    await _seed_run(apply_container, run_id=run_id, agent_id=owner)

    intruder = AgentIdentity(agent_id=str(uuid.uuid4()), token_id="t", scopes=frozenset())
    assert (await _attach_run_id(_header_request(run_id), apply_container, intruder)).run_id is None

    same_agent = AgentIdentity(agent_id=owner, token_id="t", scopes=frozenset())
    unknown = uuid.uuid4().hex
    assert (
        await _attach_run_id(_header_request(unknown), apply_container, same_agent)
    ).run_id is None

    assert (await _attach_run_id(_header_request(None), apply_container, same_agent)).run_id is None


async def test_attach_run_id_resolves_active_run_from_the_task_header(apply_container) -> None:
    """池化常驻进程的配置里没有本轮 run_id：按任务头解析当前活动 Run（通用回退）。

    仍按令牌身份校验归属，且同任务多轮时取最新创建的那一轮；解析不到就静默忽略。
    """
    owner = str(uuid.uuid4())
    task = await apply_container.task_repo.create(
        task_id=uuid.uuid4(), description="续聊", priority=1, project_id=None, agent_id=None
    )
    other_task = await apply_container.task_repo.create(
        task_id=uuid.uuid4(), description="另一件事", priority=1, project_id=None, agent_id=None
    )

    async def seed(run_id: str, *, agent_id: str, task_id, mode: str = "normal") -> None:
        await apply_container.run_repo.create(
            run_id=run_id,
            session_id="sess",
            instruction="do it",
            status=DshRunStatus.PENDING,
            agent_id=agent_id,
            task_id=task_id,
            permission_mode=mode,
        )

    # 真实 Run id 是 UUID；SQLite 的 CURRENT_TIMESTAMP 只有秒级精度，同一秒内创建的
    # 两轮会按 str(id) 做 tie-break。这里刻意让 id 字典序与创建顺序一致，避免用例
    # 依赖「两次 insert 跨秒」这种不稳定前提。
    await seed("run-task-a1", agent_id=owner, task_id=task.id, mode="plan")
    await seed("run-task-b2", agent_id=owner, task_id=task.id, mode="full")
    await seed("run-task-stranger", agent_id=str(uuid.uuid4()), task_id=task.id)
    await seed("run-other-task", agent_id=owner, task_id=other_task.id)

    identity = AgentIdentity(agent_id=owner, token_id="t", scopes=frozenset())
    resolved = await _attach_run_id(_task_header_request(str(task.id)), apply_container, identity)
    assert resolved.run_id == "run-task-b2"
    assert resolved.permission_mode == "full"

    # 别的 Agent 的任务：归属不符，不能借任务头升级到他人的 Run
    intruder = AgentIdentity(agent_id=str(uuid.uuid4()), token_id="t2", scopes=frozenset())
    assert (
        await _attach_run_id(_task_header_request(str(task.id)), apply_container, intruder)
    ).run_id is None

    # 解析不到（任务没有活动 Run / 没带任务头）：保持原身份
    assert (
        await _attach_run_id(_task_header_request(str(uuid.uuid4())), apply_container, identity)
    ).run_id is None
    assert (
        await _attach_run_id(_task_header_request(None), apply_container, identity)
    ).run_id is None


async def test_proposal_create_inherits_task_and_project_from_the_run(apply_container) -> None:
    """提案只传 payload 时，task/project 由发起它的 Run 补上。

    模型经常只传 payload（2026-10-23 基准 T02 实测），提案若落成 task_id=None：
    人审队列看不出属于哪个任务，Apply 后的 Brain 沉淀也没有项目可写。
    """
    agent_id = str(uuid.uuid4())
    project = await apply_container.brain.create_project(
        name="bench-app", repository="/tmp/bench-app"
    )
    task = await apply_container.task_repo.create(
        task_id=uuid.uuid4(),
        description="补输入校验",
        priority=1,
        project_id=project.id,
        agent_id=None,
    )
    run_id = uuid.uuid4().hex
    await apply_container.run_repo.create(
        run_id=run_id,
        session_id="sess",
        instruction="do it",
        status=DshRunStatus.PENDING,
        agent_id=agent_id,
        task_id=task.id,
    )
    ctx = ToolContext(
        container=apply_container,
        identity=AgentIdentity(
            agent_id=agent_id,
            token_id="t",
            scopes=frozenset({Capability.FILE_READ, Capability.FILE_WRITE}),
            run_id=run_id,
        ),
        tool_name="proposal.create",
    )

    data = await proposal_create(ctx, {"payload": PROPOSAL})

    stored = await apply_container.workspace.get(data["changes"][0]["change_id"])
    assert str(stored.task_id) == str(task.id)
    assert str(stored.project_id) == str(project.id)


# --- 6. 上下文闭环：失败也摄入、step 目录可读（机制 §3.3 / §6.3）---


async def test_failed_tool_call_is_ingested_as_a_failed_step(apply_container) -> None:
    """失败调用同样落 step（status=fail）：否则 Agent 看不到自己刚才错在哪，只能反复试错。"""
    agent_id = str(uuid.uuid4())
    run_id = uuid.uuid4().hex
    await _seed_run(apply_container, run_id=run_id, agent_id=agent_id)
    identity = AgentIdentity(
        agent_id=agent_id,
        token_id="t",
        scopes=frozenset({Capability.FILE_READ}),
        run_id=run_id,
    )
    server = McpServer(apply_container)

    response = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "workspace.read", "arguments": {"path": "missing.py"}},
        },
        identity,
    )

    assert response["result"]["isError"] is True
    steps = await apply_container.context_steps.list_by_run(run_id)
    assert len(steps) == 1
    assert steps[0].tool == "workspace.read"
    assert steps[0].status == StepStatus.FAIL.value
    # 错误码与消息进了 result_head（原文另存 CAS），下一轮可直接据此定位
    assert "not_found" in (steps[0].result_head or "")
    assert "missing.py" in (steps[0].result_head or "")


async def test_context_steps_lists_the_current_run_and_refuses_foreign_runs(
    apply_container,
) -> None:
    """step 目录入口：本 Run 可读、他人 Run 拒读；失败步带 ! 标记且摘要已脱敏。"""
    agent_id = str(uuid.uuid4())
    run_id = uuid.uuid4().hex
    await _seed_run(apply_container, run_id=run_id, agent_id=agent_id)
    identity = AgentIdentity(
        agent_id=agent_id,
        token_id="t",
        scopes=frozenset({Capability.FILE_READ}),
        run_id=run_id,
    )
    server = McpServer(apply_container)
    await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "workspace.read", "arguments": {"path": "missing.py"}},
        },
        identity,
    )

    listed = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "context.steps", "arguments": {"limit": 10}},
        },
        identity,
    )
    data = json.loads(listed["result"]["content"][0]["text"])
    assert data["run_id"] == run_id and data["count"] == 1
    assert data["steps"][0]["status"] == StepStatus.FAIL.value
    assert data["steps"][0]["catalog_line"].startswith("!")

    foreign = AgentIdentity(
        agent_id=str(uuid.uuid4()),
        token_id="t2",
        scopes=frozenset({Capability.FILE_READ}),
        run_id=None,
    )
    denied = await server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "context.steps", "arguments": {"run_id": run_id}},
        },
        foreign,
    )
    assert denied["result"]["isError"] is True
    assert "本 Agent" in denied["result"]["content"][0]["text"]
