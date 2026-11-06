"""三层记忆测试（批次② §4.2 / §4.3）。

对照验收四条：

1. 跨 Workspace 隔离：Project 层只在所属项目可见，跨 Workspace 只有 Environment / User；
2. 密钥红线：sk- / gh / fxt_ / AWS / 私钥 / 赋值口令六类样本在 User、Project、Environment
   三条写入路径与 REST 入口全部拒收，且拒收后库里不留任何痕迹；
3. 容量与 TTL：写时裁掉最旧、过期条目读取不可见并在下次写入时被清扫；
4. 受控写入面：MCP 面只有 `memory.recall`（读）与 `memory.remember`（运行中写用户偏好）；
   删除只对 User 层开放。

全部走真实数据库与真实 REST / MCP 链路，不 mock 仓储。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from flux.container import Container
from flux.core.mcp.tools import ALL_TOOLS
from flux.core.memory.policies import (
    ENVIRONMENT_MEMORY_SEED,
    MEMORY_POLICIES,
    PROJECT_SECTION_MAX_ENTRIES,
    MemoryPolicy,
)
from flux.core.memory.service import MemoryService
from flux.core.project_brain.service import ProjectBrain
from flux.enums import BrainSection, Capability, MemoryLayer
from flux.errors import NotFoundError, PermissionDeniedError, ValidationError

from .conftest import issue_token
from .test_mcp_server import call, payload

#: 六类必须被拒收的凭证样本：(可读标签, 内容)
SECRET_SAMPLES = [
    ("sk- 密钥", "上线前记得把 sk-" + "a" * 32 + " 换掉"),
    ("GitHub 令牌", "远端令牌是 ghp_" + "b" * 36),
    ("Flux 接入令牌", "用 fxt_" + "c" * 64 + " 接入 MCP"),
    ("AWS Access Key", "AKIAIOSFODNN7EXAMPLE"),
    ("私钥块", "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA7Qh..."),
    ("赋值口令", "password=SuperSecret123"),
]


def _project(container: Container, name: str) -> str:
    project = asyncio.run(container.brain.create_project(name=name))
    return str(project.id)


# --- 1. 跨 Workspace 隔离 ---


def test_project_layer_is_only_visible_in_its_own_workspace(container: Container) -> None:
    alpha = _project(container, "甲工作区")
    beta = _project(container, "乙工作区")
    asyncio.run(
        container.brain.write(
            alpha, section=BrainSection.DECISIONS, content="甲项目结论：用 FastAPI"
        )
    )
    asyncio.run(container.memory.write_user(content="跨项目偏好：回归测试用 pytest"))

    alpha_recall = asyncio.run(container.memory.recall(project_id=alpha))
    beta_recall = asyncio.run(container.memory.recall(project_id=beta))

    assert [layer["layer"] for layer in alpha_recall["layers"]] == [
        "environment",
        "user",
        "project",
    ]
    assert [layer["priority"] for layer in alpha_recall["layers"]] == [0, 1, 2]
    # Project 层：只在所属 Workspace 出现
    assert "甲项目结论：用 FastAPI" in alpha_recall["content"]
    assert "甲项目结论：用 FastAPI" not in beta_recall["content"]
    assert beta_recall["layers"][2]["project_id"] == beta
    # User 层：跨 Workspace 可见
    assert "跨项目偏好：回归测试用 pytest" in beta_recall["content"]
    assert "跨项目偏好：回归测试用 pytest" in alpha_recall["content"]


def test_recall_without_project_returns_the_two_global_layers(container: Container) -> None:
    asyncio.run(container.memory.write_user(content="全局偏好：提交信息写清楚为什么"))

    data = asyncio.run(container.memory.recall())

    assert data["project_id"] is None
    assert [layer["layer"] for layer in data["layers"]] == ["environment", "user"]
    assert all(layer["scope"] != "workspace" for layer in data["layers"])
    assert data["note"]


def test_mcp_recall_scopes_project_layer_by_task_project(mcp_client: TestClient) -> None:
    """Agent 侧的 memory.recall：task 所属项目的 Project 层可见，别的项目不可见。"""
    alpha = mcp_client.post("/api/v1/projects", json={"name": "甲工作区"}).json()["data"]
    beta = mcp_client.post("/api/v1/projects", json={"name": "乙工作区"}).json()["data"]
    mcp_client.post(
        f"/api/v1/projects/{alpha['id']}/memory",
        json={"section": "decisions", "content": "甲项目结论：选 SQLite 起步"},
    )
    mcp_client.post("/api/v1/memory/entries", json={"content": "用户偏好：注释用中文"})
    task = mcp_client.post(
        "/api/v1/tasks", json={"description": "在乙工作区干活", "project_id": beta["id"]}
    ).json()["data"]

    token = issue_token(mcp_client, scopes=[Capability.FILE_READ])
    beta_data = payload(call(mcp_client, token, "memory.recall", {"task_id": task["id"]}))
    assert beta_data["project_id"] == beta["id"]
    assert "甲项目结论：选 SQLite 起步" not in beta_data["content"]
    assert "用户偏好：注释用中文" in beta_data["content"]

    alpha_data = payload(call(mcp_client, token, "memory.recall", {"project_id": alpha["id"]}))
    assert "甲项目结论：选 SQLite 起步" in alpha_data["content"]


# --- 2. 密钥红线 ---


@pytest.mark.parametrize(
    ("label", "sample"), SECRET_SAMPLES, ids=[label for label, _ in SECRET_SAMPLES]
)
def test_secret_samples_are_rejected_on_every_service_write_path(
    container: Container, label: str, sample: str
) -> None:
    project = _project(container, "密钥红线项目")

    with pytest.raises(ValidationError) as user_err:
        asyncio.run(container.memory.write_user(content=sample))
    assert "密钥" in user_err.value.message
    assert user_err.value.details["policy"] == "secrets are never stored and never enter context"

    with pytest.raises(ValidationError):
        asyncio.run(container.brain.write(project, section=BrainSection.DECISIONS, content=sample))

    with pytest.raises(ValidationError):
        asyncio.run(container.memory.write_environment(key="test.secret", content=sample))

    # 三条路径都没留下痕迹
    assert asyncio.run(container.memory_repo.list_layer(MemoryLayer.USER)) == []
    assert asyncio.run(container.memory_repo.list_layer(MemoryLayer.ENVIRONMENT)) == []
    assert asyncio.run(container.brain.sections(project))["decisions"] == []


@pytest.mark.parametrize(
    ("label", "sample"), SECRET_SAMPLES, ids=[label for label, _ in SECRET_SAMPLES]
)
def test_rest_entry_points_reject_secret_samples(
    client: TestClient, label: str, sample: str
) -> None:
    user_response = client.post("/api/v1/memory/entries", json={"content": sample})
    assert user_response.status_code == 422, label
    assert user_response.json()["code"] == "validation_error"

    project = client.post("/api/v1/projects", json={"name": "密钥红线项目"}).json()["data"]
    project_response = client.post(
        f"/api/v1/projects/{project['id']}/memory",
        json={"section": "decisions", "content": sample},
    )
    assert project_response.status_code == 422, label

    assert client.get("/api/v1/memory/entries", params={"layer": "user"}).json()["data"] == []
    sections = client.get(f"/api/v1/projects/{project['id']}/memory").json()["data"]
    assert sections["decisions"] == []


def test_placeholders_are_not_blocked(container: Container) -> None:
    """文档示例里的占位符不该被红线误杀（红线拦的是真凭证，不是写作素材）。"""
    entry = asyncio.run(
        container.memory.write_user(content="部署文档示例：api_key=YOUR_API_KEY_HERE")
    )
    assert "YOUR_API_KEY_HERE" in entry.content


# --- 3. 容量裁剪 ---


def _small_user_service(container: Container, *, max_entries: int) -> MemoryService:
    """注入小容量的 User 层服务；种子清空避免 Environment 干扰容量断言。"""
    return MemoryService(
        container.memory_repo,
        policies={MemoryLayer.USER: MemoryPolicy(max_entries=max_entries, ttl=None)},
        environment_seed=(),
    )


def test_user_layer_prunes_the_oldest_beyond_capacity(container: Container) -> None:
    service = _small_user_service(container, max_entries=3)
    for index in range(5):
        asyncio.run(service.write_user(content=f"用户偏好第 {index} 条"))

    rows = asyncio.run(container.memory_repo.list_layer(MemoryLayer.USER))

    assert [row.content for row in rows] == [
        "用户偏好第 2 条",
        "用户偏好第 3 条",
        "用户偏好第 4 条",
    ]


def test_environment_layer_respects_capacity(container: Container) -> None:
    service = MemoryService(
        container.memory_repo,
        policies={MemoryLayer.ENVIRONMENT: MemoryPolicy(max_entries=2)},
        environment_seed=(),
    )
    for index in range(3):
        asyncio.run(service.write_environment(key=f"runtime.k{index}", content=f"平台规则 {index}"))

    entries = asyncio.run(service.list_entries(layer=MemoryLayer.ENVIRONMENT))

    assert [entry.key for entry in entries] == ["runtime.k1", "runtime.k2"]


def test_project_section_prunes_the_oldest_beyond_capacity(container: Container) -> None:
    project = _project(container, "容量可裁剪的项目")
    brain = ProjectBrain(container.brain_repo, section_max_entries=2)
    for index in range(4):
        asyncio.run(brain.write(project, section=BrainSection.DECISIONS, content=f"决策 {index}"))

    sections = asyncio.run(brain.sections(project))

    assert [entry["content"] for entry in sections["decisions"]] == ["决策 2", "决策 3"]


# --- 4. TTL ---


def test_expired_entries_are_hidden_and_swept_on_next_write(container: Container) -> None:
    service = MemoryService(
        container.memory_repo,
        policies={MemoryLayer.USER: MemoryPolicy(max_entries=200, ttl=timedelta(seconds=0))},
        environment_seed=(),
    )
    first = asyncio.run(service.write_user(content="很快过期的偏好 A"))
    # 库里有原始行，但读取路径已把它排除
    assert len(asyncio.run(container.memory_repo.list_layer(MemoryLayer.USER))) == 1
    assert asyncio.run(service.list_entries(layer=MemoryLayer.USER)) == []

    second = asyncio.run(service.write_user(content="很快过期的偏好 B"))

    # 写 B 时顺手清扫了 A：库里只剩 B
    rows = asyncio.run(container.memory_repo.list_layer(MemoryLayer.USER))
    assert [row.id for row in rows] == [second.id]
    assert first.id != second.id


def test_default_policies_pin_capacity_and_ttl() -> None:
    """§4.2：每层都有容量上限；User 层 180 天 TTL，规则与项目沉淀不设过期。"""
    assert MEMORY_POLICIES[MemoryLayer.USER].max_entries == 200
    assert MEMORY_POLICIES[MemoryLayer.USER].ttl == timedelta(days=180)
    assert MEMORY_POLICIES[MemoryLayer.ENVIRONMENT].max_entries == 50
    assert MEMORY_POLICIES[MemoryLayer.ENVIRONMENT].ttl is None
    assert MEMORY_POLICIES[MemoryLayer.PROJECT].max_entries == PROJECT_SECTION_MAX_ENTRIES
    assert MEMORY_POLICIES[MemoryLayer.PROJECT].ttl is None


def test_user_write_sets_expiry_from_policy(container: Container) -> None:
    entry = asyncio.run(container.memory.write_user(content="半年内有效的偏好"))
    assert entry.expires_at is not None


# --- 5. 受控写入面 ---


def test_environment_seed_is_idempotent(container: Container) -> None:
    first = asyncio.run(container.memory.list_entries(layer=MemoryLayer.ENVIRONMENT))
    second = asyncio.run(container.memory.list_entries(layer=MemoryLayer.ENVIRONMENT))

    assert len(first) == len(ENVIRONMENT_MEMORY_SEED)
    assert [entry.id for entry in first] == [entry.id for entry in second]
    assert "runtime.secrets" in {entry.key for entry in first}
    # 种子内容里不许出现任何密钥字面量（红线自证）
    assert all("sk-" not in entry.content for entry in first)


def test_only_user_memory_can_be_deleted(container: Container) -> None:
    user_entry = asyncio.run(container.memory.write_user(content="可删除的用户偏好"))
    asyncio.run(container.memory.delete(user_entry.id))
    assert asyncio.run(container.memory.list_entries(layer=MemoryLayer.USER)) == []

    environment_entry = asyncio.run(container.memory.list_entries(layer=MemoryLayer.ENVIRONMENT))[0]
    with pytest.raises(PermissionDeniedError):
        asyncio.run(container.memory.delete(environment_entry.id))
    with pytest.raises(NotFoundError):
        asyncio.run(container.memory.delete(uuid.uuid4()))


def test_project_layer_listing_is_directed_to_the_project_api(container: Container) -> None:
    with pytest.raises(ValidationError) as excinfo:
        asyncio.run(container.memory.list_entries(layer=MemoryLayer.PROJECT))
    assert "/projects/" in excinfo.value.message


def test_mcp_surface_exposes_recall_and_remember() -> None:
    """Agent 面只有受控的两个记忆工具：读取 + 运行中记录用户偏好。"""
    names = {tool.name for tool in ALL_TOOLS}
    assert {name for name in names if name.startswith("memory.")} == {
        "memory.recall",
        "memory.remember",
    }


# --- 6. REST 面 ---


def test_rest_memory_roundtrip(client: TestClient) -> None:
    created = client.post("/api/v1/memory/entries", json={"content": "用户偏好：提交信息用中文"})
    assert created.status_code == 200
    entry = created.json()["data"]
    assert entry["layer"] == "user" and entry["source"] == "user"
    assert entry["expires_at"] is not None

    listed = client.get("/api/v1/memory/entries", params={"layer": "user"}).json()
    assert listed["metadata"]["count"] == 1

    # 缺省列出 Environment + User 两层
    combined = client.get("/api/v1/memory/entries").json()
    assert combined["metadata"]["count"] == 1 + len(ENVIRONMENT_MEMORY_SEED)

    recall = client.get("/api/v1/memory").json()["data"]
    assert [layer["layer"] for layer in recall["layers"]] == ["environment", "user"]
    assert "用户偏好：提交信息用中文" in recall["content"]

    deleted = client.delete(f"/api/v1/memory/entries/{entry['id']}")
    assert deleted.status_code == 200
    assert client.get("/api/v1/memory/entries", params={"layer": "user"}).json()["data"] == []
    assert client.delete(f"/api/v1/memory/entries/{entry['id']}").status_code == 404


def test_rest_recall_includes_project_layer_only_when_asked(client: TestClient) -> None:
    project = client.post("/api/v1/projects", json={"name": "REST 记忆项目"}).json()["data"]
    client.post(
        f"/api/v1/projects/{project['id']}/memory",
        json={"section": "coding_rules", "content": "提交前必须跑 make verify"},
    )
    client.post("/api/v1/memory/entries", json={"content": "用户偏好：交付要有证据"})

    data = client.get("/api/v1/memory", params={"project_id": project["id"]}).json()["data"]

    assert [layer["layer"] for layer in data["layers"]] == ["environment", "user", "project"]
    assert "提交前必须跑 make verify" in data["content"]


def test_rest_entries_reject_project_layer(client: TestClient) -> None:
    response = client.get("/api/v1/memory/entries", params={"layer": "project"})
    assert response.status_code == 422
    assert "/projects/" in response.json()["message"]
