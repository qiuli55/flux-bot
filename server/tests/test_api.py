"""API 层端到端测试（主规格 §12）。

所有断言都检查统一响应体（裁决 A6：success / code / message / data / metadata）。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from flux.core.virtual_workspace.repository import ProposalRepository
from flux.core.virtual_workspace.service import VirtualWorkspaceService
from flux.db.session import create_engine, create_session_factory

PREFIX = "/api/v1"


def _agent_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "测试 Agent",
        "role": "developer",
        "permissions": ["file.read", "file.write"],
    }
    payload.update(overrides)
    return payload


# --- 健康检查 ---


def test_health_is_alive(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/health")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["status"] == "ok"
    assert body["data"]["env"] == "test"


def test_ready_reports_database_and_providers(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["database"] is True
    assert body["data"]["providers"] == ["local"]


# --- Agent（§12.3）---


def test_agent_crud_flow(client: TestClient) -> None:
    created = client.post(f"{PREFIX}/agents", json=_agent_payload())
    assert created.status_code == 200
    agent = created.json()["data"]
    assert agent["state"] == "READY"
    assert agent["spec"]["role"] == "developer"

    listed = client.get(f"{PREFIX}/agents").json()
    assert listed["metadata"]["count"] == 1
    assert len(listed["data"]) == 1

    fetched = client.get(f"{PREFIX}/agents/{agent['id']}").json()
    assert fetched["data"]["id"] == agent["id"]


def test_agent_registry_has_no_execution_endpoint(client: TestClient) -> None:
    """Flux 不替 agent 执行任务：注册表只有增删查，没有模型执行入口。"""
    agent_id = client.post(f"{PREFIX}/agents", json=_agent_payload()).json()["data"]["id"]

    response = client.post(f"{PREFIX}/agents/{agent_id}/execute", json={"instruction": "x"})

    assert response.status_code == 404


def test_invalid_payload_returns_validation_error(client: TestClient) -> None:
    response = client.post(f"{PREFIX}/agents", json={"name": "", "role": "不存在的角色"})
    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "validation_error"


# --- Task（§12.4）---


def test_task_create_get_cancel(client: TestClient) -> None:
    created = client.post(f"{PREFIX}/tasks", json={"description": "修复登录缺陷", "priority": 5})
    assert created.status_code == 200
    task = created.json()["data"]
    assert task["status"] == "pending"
    assert task["priority"] == 5

    assert client.get(f"{PREFIX}/tasks/{task['id']}").json()["data"]["id"] == task["id"]

    cancelled = client.post(f"{PREFIX}/tasks/{task['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["status"] == "cancelled"


def test_cancel_twice_conflicts(client: TestClient) -> None:
    task_id = client.post(f"{PREFIX}/tasks", json={"description": "甲"}).json()["data"]["id"]
    client.post(f"{PREFIX}/tasks/{task_id}/cancel")

    response = client.post(f"{PREFIX}/tasks/{task_id}/cancel")
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"


def test_get_unknown_task_not_found(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/tasks/不存在")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


# --- Virtual Workspace（§12.5）---


def _seed_proposal(client: TestClient, *, file_path: str = "auth/login.py") -> str:
    """直接经服务层写库（提案已落 virtual_changes 表，不能用内存字典塞）。

    单独开一个短生命周期引擎：app 的容器跑在自己的事件循环里，跨循环复用它的
    连接池会踩到 aiosqlite 的连接绑定问题。
    """
    database_url = client.app.state.container.settings.database_url
    engine = create_engine(database_url)

    async def _insert() -> str:
        service = VirtualWorkspaceService(ProposalRepository(create_session_factory(engine)))
        change = await service.propose(
            file_path=file_path,
            original_content="return False\n",
            proposed_content="return check_password(user)\n",
            agent_source="developer-agent",
        )
        return str(change.id)

    try:
        return asyncio.run(_insert())
    finally:
        asyncio.run(engine.dispose())


def test_workspace_list_apply_reject(apply_client: TestClient, workspace_root) -> None:
    """经 API 走完整审查链路：apply 会真正落盘（§7.6），所以先把被改文件写进工作区。"""
    (workspace_root / "a.py").write_text("return False\n", encoding="utf-8")
    apply_id = _seed_proposal(apply_client, file_path="a.py")
    reject_id = _seed_proposal(apply_client, file_path="b.py")

    listed = apply_client.get(f"{PREFIX}/workspace/changes").json()
    assert listed["metadata"]["count"] == 2
    assert listed["data"][0]["diff"].startswith("---")
    assert listed["data"][0]["added_lines"] == 1

    detail = apply_client.get(f"{PREFIX}/workspace/changes/{apply_id}").json()["data"]
    assert detail["status"] == "pending"
    assert detail["original_hash"]

    applied = apply_client.post(f"{PREFIX}/workspace/apply", json={"change_ids": [apply_id]})
    assert applied.status_code == 200
    body = applied.json()["data"][0]
    assert body["status"] == "applied"
    assert body["backup_path"]
    assert (workspace_root / "a.py").read_text(encoding="utf-8") == "return check_password(user)\n"

    rejected = apply_client.post(
        f"{PREFIX}/workspace/reject",
        json={"change_ids": [reject_id], "reason": "改动范围过大"},
    )
    assert rejected.json()["data"][0]["status"] == "rejected"
    assert rejected.json()["metadata"]["reason"] == "改动范围过大"

    assert len(apply_client.get(f"{PREFIX}/workspace/changes?status=applied").json()["data"]) == 1


def test_workspace_accept_then_apply(apply_client: TestClient, workspace_root) -> None:
    """accept 只批准不落盘，apply 才写文件：两步之间文件内容必须保持原样。"""
    (workspace_root / "a.py").write_text("return False\n", encoding="utf-8")
    change_id = _seed_proposal(apply_client, file_path="a.py")

    accepted = apply_client.post(f"{PREFIX}/workspace/accept", json={"change_ids": [change_id]})
    assert accepted.status_code == 200
    assert accepted.json()["data"][0]["status"] == "accepted"
    assert (workspace_root / "a.py").read_text(encoding="utf-8") == "return False\n"

    applied = apply_client.post(f"{PREFIX}/workspace/apply", json={"change_ids": [change_id]})
    assert applied.json()["data"][0]["status"] == "applied"
    assert (workspace_root / "a.py").read_text(encoding="utf-8") == "return check_password(user)\n"


def test_workspace_apply_without_workspace_root_fails(client: TestClient) -> None:
    """未配置 FLUX_WORKSPACE_ROOT 时 apply 明确报错（422），状态停在 accepted 等人工处理。"""
    change_id = _seed_proposal(client, file_path="a.py")

    response = client.post(f"{PREFIX}/workspace/apply", json={"change_ids": [change_id]})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    detail = client.get(f"{PREFIX}/workspace/changes/{change_id}").json()["data"]
    assert detail["status"] == "accepted"
    assert detail["apply_error"] is None


def test_workspace_apply_conflict_when_file_changed(
    apply_client: TestClient, workspace_root
) -> None:
    """人工在提案生成后改了文件 → 409，用户改动不能被覆盖。"""
    change_id = _seed_proposal(apply_client, file_path="a.py")
    (workspace_root / "a.py").write_text("user 自己改的\n", encoding="utf-8")

    response = apply_client.post(f"{PREFIX}/workspace/apply", json={"change_ids": [change_id]})

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert (workspace_root / "a.py").read_text(encoding="utf-8") == "user 自己改的\n"


def test_workspace_apply_precheck_conflict_keeps_status_accepted(
    apply_client: TestClient, workspace_root: Path
) -> None:
    """收口方案 §3（任务 40）：目标路径被目录占位 → 预检冲突 409，提案留在 accepted。

    预检阶段还没落盘、且问题可修复（用户挪走目录后可重新 Apply），所以不能标 failed——
    failed 只该表示"已进入落盘阶段才失败"。benchmark 按这个契约判定。
    """
    change_id = _seed_proposal(apply_client, file_path="pkg/target.py")
    blocker = workspace_root / "pkg" / "target.py"
    blocker.mkdir(parents=True)
    (blocker / "占位").write_text("x", encoding="utf-8")

    response = apply_client.post(f"{PREFIX}/workspace/apply", json={"change_ids": [change_id]})

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    detail = apply_client.get(f"{PREFIX}/workspace/changes/{change_id}").json()["data"]
    assert detail["status"] == "accepted"
    assert detail["apply_error"] is None
    # 无半应用：占位目录原样保留，没有被当成文件覆盖掉
    assert blocker.is_dir()
    assert (blocker / "占位").read_text(encoding="utf-8") == "x"


def test_workspace_apply_rejects_symlink_escape(
    apply_client: TestClient, workspace_root: Path
) -> None:
    """被改文件是软链（指向工作区根之外）→ 422；根外文件保持原样，提案停在 accepted。"""
    secret = workspace_root.parent / "secret.py"
    secret.write_text("return False\n", encoding="utf-8")
    (workspace_root / "a.py").symlink_to(secret)
    change_id = _seed_proposal(apply_client, file_path="a.py")

    response = apply_client.post(f"{PREFIX}/workspace/apply", json={"change_ids": [change_id]})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert "符号链接" in body["message"]
    assert secret.read_text(encoding="utf-8") == "return False\n"
    assert not (workspace_root / ".flux").exists()
    detail = apply_client.get(f"{PREFIX}/workspace/changes/{change_id}").json()["data"]
    assert detail["status"] == "accepted"


def test_workspace_get_unknown_change(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/workspace/changes/0f5b6f4c-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_workspace_apply_unknown_change(client: TestClient) -> None:
    response = client.post(f"{PREFIX}/workspace/apply", json={"change_ids": ["不存在"]})
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_workspace_apply_batch_writes_all_files(apply_client: TestClient, workspace_root) -> None:
    """一次 apply 三条：全部落盘成功，返回的每条都带自己的备份路径。"""
    for name in ("a.py", "b.py", "c.py"):
        (workspace_root / name).write_text("return False\n", encoding="utf-8")
    ids = [_seed_proposal(apply_client, file_path=name) for name in ("a.py", "b.py", "c.py")]

    response = apply_client.post(f"{PREFIX}/workspace/apply", json={"change_ids": ids})

    assert response.status_code == 200
    body = response.json()
    assert body["metadata"]["count"] == 3
    assert [item["status"] for item in body["data"]] == ["applied"] * 3
    for name in ("a.py", "b.py", "c.py"):
        content = (workspace_root / name).read_text(encoding="utf-8")
        assert content == "return check_password(user)\n"


def test_workspace_apply_batch_has_no_partial_apply(
    apply_client: TestClient, workspace_root
) -> None:
    """P0-03 核心判据：batch 里一条过期 → 409，另外两条也不许留在盘上。"""
    for name in ("a.py", "b.py", "c.py"):
        (workspace_root / name).write_text("return False\n", encoding="utf-8")
    ids = [_seed_proposal(apply_client, file_path=name) for name in ("a.py", "b.py", "c.py")]
    # 人工在提案生成后改了中间那个文件 → 该提案过期
    (workspace_root / "b.py").write_text("user 自己改的\n", encoding="utf-8")

    response = apply_client.post(f"{PREFIX}/workspace/apply", json={"change_ids": ids})

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert (workspace_root / "a.py").read_text(encoding="utf-8") == "return False\n"
    assert (workspace_root / "b.py").read_text(encoding="utf-8") == "user 自己改的\n"
    assert (workspace_root / "c.py").read_text(encoding="utf-8") == "return False\n"
    for change_id in ids:
        detail = apply_client.get(f"{PREFIX}/workspace/changes/{change_id}").json()["data"]
        assert detail["status"] == "accepted", change_id
        assert detail["apply_error"] is None


def test_workspace_accept_batch_rejects_bad_id_without_partial_state(client: TestClient) -> None:
    """accept 整批要么全成功要么全不动：坏 ID 不能让前一条先变成 accepted。"""
    first = _seed_proposal(client, file_path="a.py")

    response = client.post(f"{PREFIX}/workspace/accept", json={"change_ids": [first, "不存在"]})

    assert response.status_code == 404
    detail = client.get(f"{PREFIX}/workspace/changes/{first}").json()["data"]
    assert detail["status"] == "pending"


def test_workspace_apply_batch_rejects_duplicate_ids(
    apply_client: TestClient, workspace_root
) -> None:
    (workspace_root / "a.py").write_text("return False\n", encoding="utf-8")
    change_id = _seed_proposal(apply_client, file_path="a.py")

    response = apply_client.post(
        f"{PREFIX}/workspace/apply", json={"change_ids": [change_id, change_id]}
    )

    assert response.status_code == 422
    assert "重复" in response.json()["message"]
    assert (workspace_root / "a.py").read_text(encoding="utf-8") == "return False\n"


# --- Git 集成（§17.6；实施计划 ⑨）---


def _init_git_repo(repo: Path, *files: str) -> None:
    """在 workspace_root 里建真实仓库并提交一次（身份写进仓库本地配置）。"""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Flux Test",
        "GIT_AUTHOR_EMAIL": "flux-test@example.com",
        "GIT_COMMITTER_NAME": "Flux Test",
        "GIT_COMMITTER_EMAIL": "flux-test@example.com",
    }

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)

    run("init", "-b", "main")
    # 产品提交路径（API → GitService → GitClient）不注入 git 身份；
    # 写 repo-local 配置，CI runner 上无全局身份也能提交
    run("config", "user.name", "Flux Test")
    run("config", "user.email", "flux-test@example.com")
    tracked = ["README.md", *files]
    for name in tracked:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("v1\n", encoding="utf-8")
    run("add", *tracked)
    run("commit", "-m", "chore: 初始化")


def test_git_status_and_diff_via_api(apply_client: TestClient, workspace_root: Path) -> None:
    _init_git_repo(workspace_root, "a.py")
    (workspace_root / "a.py").write_text("v2\n", encoding="utf-8")

    status = apply_client.get(f"{PREFIX}/git/status").json()["data"]
    assert status["branch"] == "main"
    assert status["clean"] is False
    assert status["files"][0]["path"] == "a.py"

    diff = apply_client.post(f"{PREFIX}/git/diff", json={"paths": ["a.py"]}).json()["data"]
    assert diff["empty"] is False
    assert "a.py" in diff["text"] and "-v1" in diff["text"] and "+v2" in diff["text"]


def test_git_checkout_and_branches_via_api(apply_client: TestClient, workspace_root: Path) -> None:
    _init_git_repo(workspace_root)

    created = apply_client.post(
        f"{PREFIX}/git/checkout", json={"target": "feature/closed-loop", "create": True}
    ).json()["data"]
    assert created["current"] == "feature/closed-loop"

    listed = apply_client.get(f"{PREFIX}/git/branches").json()["data"]
    assert listed["current"] == "feature/closed-loop"
    assert listed["locals"] == ["feature/closed-loop", "main"]


def test_git_commit_only_accepts_applied_changes(
    apply_client: TestClient, workspace_root: Path
) -> None:
    """闭环顺序 Approved → Tests Passed → Git Commit：pending 的提案一个提交都不许进。"""
    _init_git_repo(workspace_root)
    (workspace_root / "auth").mkdir(exist_ok=True)
    (workspace_root / "auth" / "login.py").write_text("return False\n", encoding="utf-8")
    change_id = _seed_proposal(apply_client, file_path="auth/login.py")

    too_early = apply_client.post(
        f"{PREFIX}/git/commit", json={"message": "feat: 还没批准", "change_ids": [change_id]}
    )
    assert too_early.status_code == 409
    assert too_early.json()["code"] == "invalid_state_transition"

    applied = apply_client.post(f"{PREFIX}/workspace/apply", json={"change_ids": [change_id]})
    assert applied.json()["data"][0]["status"] == "applied"

    committed = apply_client.post(
        f"{PREFIX}/git/commit", json={"message": "feat: 登录校验", "change_ids": [change_id]}
    )
    assert committed.status_code == 200
    body = committed.json()["data"]
    assert len(body["sha"]) == 40
    assert body["message"] == "feat: 登录校验"
    assert body["files"] == ["auth/login.py"]


def test_git_reports_missing_workspace_configuration(client: TestClient) -> None:
    """未配置 FLUX_WORKSPACE_ROOT 时 Git 操作明确报错，不返回空结果。"""
    response = client.get(f"{PREFIX}/git/status")

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_git_reports_directory_that_is_not_a_repository(
    apply_client: TestClient, workspace_root: Path
) -> None:
    response = apply_client.get(f"{PREFIX}/git/status")

    assert response.status_code == 409
    assert response.json()["code"] == "not_a_git_repository"


# --- Project / Project Brain（§12.12；实施计划 ⑩⑪）---


def _create_project(client: TestClient, *, name: str = "Flux 演示项目") -> str:
    response = client.post(f"{PREFIX}/projects", json={"name": name, "repository": "qiuli55/flux"})
    assert response.status_code == 200
    return response.json()["data"]["id"]


def test_project_create_list_get(client: TestClient) -> None:
    project_id = _create_project(client, name="知识库项目")
    _create_project(client, name="第二个项目")

    listed = client.get(f"{PREFIX}/projects").json()
    assert listed["metadata"]["count"] == 2

    fetched = client.get(f"{PREFIX}/projects/{project_id}").json()["data"]
    assert fetched["name"] == "知识库项目"
    assert fetched["repository"] == "qiuli55/flux"
    # 时间戳由数据库写入后读回，必须是真实值而不是 None
    assert fetched["created_at"] is not None


def test_project_scaffolding_errors(client: TestClient) -> None:
    assert client.post(f"{PREFIX}/projects", json={"name": ""}).status_code == 422

    missing = client.get(f"{PREFIX}/projects/0f5b6f4c-0000-0000-0000-000000000000")
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"

    not_uuid = client.get(f"{PREFIX}/projects/不是UUID")
    assert not_uuid.status_code == 422
    assert not_uuid.json()["code"] == "validation_error"


def test_memory_write_and_read_via_api(client: TestClient) -> None:
    project_id = _create_project(client)

    empty = client.get(f"{PREFIX}/projects/{project_id}/memory").json()
    assert set(empty["data"]) == {
        "overview",
        "tech_stack",
        "architecture",
        "coding_rules",
        "progress",
        "decisions",
        "agent_notes",
    }
    assert empty["metadata"]["count"] == 0

    written = client.post(
        f"{PREFIX}/projects/{project_id}/memory",
        json={"section": "decisions", "content": "先做结构化记忆，不上向量库"},
    )
    assert written.status_code == 200
    assert written.json()["data"]["section"] == "decisions"

    client.post(
        f"{PREFIX}/projects/{project_id}/memory",
        json={"section": "coding_rules", "content": "禁止裸 except"},
    )
    client.post(
        f"{PREFIX}/projects/{project_id}/memory",
        json={"section": "coding_rules", "content": "所有公开接口必须有错误码"},
    )

    sections = client.get(f"{PREFIX}/projects/{project_id}/memory").json()
    # 累积型分区保留两条；现状型分区只留最新一条
    assert len(sections["data"]["decisions"]) == 1
    assert len(sections["data"]["coding_rules"]) == 1
    assert sections["data"]["coding_rules"][0]["content"] == "所有公开接口必须有错误码"
    assert sections["metadata"]["count"] == 2


def test_memory_rejects_unknown_section(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.post(
        f"{PREFIX}/projects/{project_id}/memory",
        json={"section": "不存在的分区", "content": "x"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_memory_context_via_api(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"{PREFIX}/projects/{project_id}/memory",
        json={"section": "architecture", "content": "三层：api / core / models"},
    )

    response = client.get(f"{PREFIX}/projects/{project_id}/memory/context")

    assert response.status_code == 200
    text = response.json()["data"]["text"]
    assert text.startswith("# 项目：Flux 演示项目")
    assert "## Architecture\n三层：api / core / models" in text


def test_project_scan_records_profile(apply_client: TestClient, workspace_root: Path) -> None:
    """扫描工作区（⑩）→ 写入 Brain（⑪），全程真实文件系统。"""
    (workspace_root / "requirements.txt").write_text("fastapi\npytest\n", encoding="utf-8")
    (workspace_root / "main.py").write_text("app = None\n", encoding="utf-8")
    project_id = _create_project(apply_client)

    response = apply_client.post(f"{PREFIX}/projects/{project_id}/scan", json={"record": True})

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["profile"]["primary_language"] == "Python"
    assert body["profile"]["frameworks"] == ["FastAPI"]
    assert [entry["section"] for entry in body["recorded"]] == ["overview", "tech_stack"]

    sections = apply_client.get(f"{PREFIX}/projects/{project_id}/memory").json()["data"]
    assert "语言：Python" in sections["tech_stack"][0]["content"]


def test_project_scan_without_recording(client: TestClient, tmp_path: Path) -> None:
    project_id = _create_project(client)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "package.json").write_text('{"name": "web", "dependencies": {"vue": "^3.5.0"}}')
    (target / "index.js").write_text("console.log('hi')\n", encoding="utf-8")

    response = client.post(
        f"{PREFIX}/projects/{project_id}/scan",
        json={"workspace_root": str(target), "record": False},
    )

    assert response.status_code == 200
    assert response.json()["data"]["profile"]["package_manager"] == "npm"
    assert response.json()["data"]["recorded"] == []
    assert client.get(f"{PREFIX}/projects/{project_id}/memory").json()["metadata"]["count"] == 0


def test_project_scan_without_workspace_root_fails(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.post(f"{PREFIX}/projects/{project_id}/scan", json={})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


# --- Model Gateway（§12.6）---


def test_models_chat_with_local_provider(client: TestClient) -> None:
    response = client.post(
        f"{PREFIX}/models/chat",
        json={
            "messages": [{"role": "user", "content": "用一句话说明 Virtual Workspace"}],
            "task_id": "task-9",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert "Virtual Workspace" in body["data"]["content"]
    assert body["data"]["provider"] == "local"
    assert body["data"]["usage"]["total_tokens"] > 0
    assert body["metadata"]["task_id"] == "task-9"


def test_models_chat_with_unconfigured_provider(client: TestClient) -> None:
    """M0 未接入真实供应商，必须给出明确错误，而不是假装成功。"""
    response = client.post(
        f"{PREFIX}/models/chat",
        json={
            "messages": [{"role": "user", "content": "你好"}],
            "provider": "openai",
        },
    )
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "provider_not_configured"
    assert body["data"]["available"] == ["local"]


# --- Connector（§12.7）---


def test_connector_execute_unregistered(client: TestClient) -> None:
    response = client.post(
        f"{PREFIX}/connectors/execute",
        json={"connector": "github", "action": "create_pr", "parameters": {}},
    )
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "connector_not_registered"
    assert body["data"]["registered"] == []


def test_connector_execute_denied_without_capability(client: TestClient) -> None:
    """客户端不能自行声明能力：agent 没有 terminal.execute 时必须 403。"""
    from flux.connectors.base import ConnectorRegistry
    from tests.fakes import FakeTerminalConnector

    container = client.app.state.container
    registry = ConnectorRegistry(container.bus)
    registry.register(FakeTerminalConnector())
    client.app.state.container.connectors = registry

    agent_id = client.post(
        f"{PREFIX}/agents", json=_agent_payload(permissions=["file.read"])
    ).json()["data"]["id"]

    response = client.post(
        f"{PREFIX}/connectors/execute",
        json={
            "connector": "terminal",
            "action": "run",
            "parameters": {"command": "ls"},
            "agent_id": agent_id,
        },
    )
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


def test_connector_execute_succeeds_with_capability(client: TestClient) -> None:
    from flux.connectors.base import ConnectorRegistry
    from tests.fakes import FakeTerminalConnector

    container = client.app.state.container
    registry = ConnectorRegistry(container.bus)
    registry.register(FakeTerminalConnector())
    client.app.state.container.connectors = registry

    agent_id = client.post(
        f"{PREFIX}/agents", json=_agent_payload(permissions=["terminal.execute"])
    ).json()["data"]["id"]

    response = client.post(
        f"{PREFIX}/connectors/execute",
        json={
            "connector": "terminal",
            "action": "run",
            "parameters": {"command": "pytest"},
            "agent_id": agent_id,
        },
    )
    assert response.status_code == 200
    assert response.json()["data"]["result"]["exit_code"] == 0
