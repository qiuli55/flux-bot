"""Agent Installation REST API 测试（UI「外部 Agent」区块的后端）。

用 stub adapter 替换容器里的真实探测：用例不依赖"本机装没装 codex/opencode"，
在 CI 的两个平台上结果一致。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from flux.container import Container
from flux.core.agent_runtime.adapters.base import CliAgentAdapter, CliAgentProbe
from flux.core.agent_runtime.installation import InstallationRepository, InstallationService
from flux.enums import AgentInstallStatus


class _StubAdapter(CliAgentAdapter):
    def __init__(self, name: str, probe: CliAgentProbe) -> None:
        self.name = name
        self.probe = probe

    def discover(self) -> CliAgentProbe:
        return self.probe

    def verify(self) -> CliAgentProbe:
        return self.probe

    def get_version(self) -> str | None:
        return self.probe.version

    def check_auth(self) -> str:
        return self.probe.auth_status


def _probe(name: str, status: AgentInstallStatus) -> CliAgentProbe:
    return CliAgentProbe(
        name=name,
        adapter=name,
        status=status,
        executable=name,
        path=f"/usr/bin/{name}",
        version="1.2.3",
        auth_status="ok" if status is not AgentInstallStatus.NOT_INSTALLED else "missing",
        capabilities=("mcp",),
    )


@pytest.fixture()
def inst_client(client: TestClient) -> TestClient:
    """把容器里的 installations 换成"事实可控"的 stub：alpha 已装、beta 未装。"""
    container: Container = client.app.state.container
    alpha = _StubAdapter("alpha", _probe("alpha", AgentInstallStatus.DISCOVERED))
    beta = _StubAdapter("beta", _probe("beta", AgentInstallStatus.NOT_INSTALLED))
    container.installations = InstallationService(
        InstallationRepository(container.session_factory), adapters=[alpha, beta]
    )
    return client


def test_scan_records_real_facts(inst_client: TestClient) -> None:
    resp = inst_client.post("/api/v1/installations/scan")
    assert resp.status_code == 200
    body = resp.json()
    by_name = {row["name"]: row for row in body["data"]}
    assert by_name["alpha"]["status"] == "DISCOVERED"
    assert by_name["alpha"]["path"] == "/usr/bin/alpha"
    assert by_name["beta"]["status"] == "NOT_INSTALLED"
    assert body["metadata"]["count"] == 2


def test_list_is_empty_before_scan(inst_client: TestClient) -> None:
    resp = inst_client.get("/api/v1/installations")
    assert resp.status_code == 200
    assert resp.json()["metadata"]["count"] == 0


def test_connect_single_advances_to_ready(inst_client: TestClient) -> None:
    inst_client.post("/api/v1/installations/scan")
    resp = inst_client.post("/api/v1/installations/connect", json={"agent": "alpha"})
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "READY"
    # 再 list 一次：状态已落库
    rows = inst_client.get("/api/v1/installations").json()["data"]
    assert {row["name"]: row["status"] for row in rows}["alpha"] == "READY"


def test_connect_all_skips_not_installed(inst_client: TestClient) -> None:
    inst_client.post("/api/v1/installations/scan")
    resp = inst_client.post("/api/v1/installations/connect", json={"all": True})
    assert resp.status_code == 200
    assert [row["name"] for row in resp.json()["data"]] == ["alpha"]
    assert resp.json()["metadata"]["mode"] == "all"


def test_connect_requires_explicit_target(inst_client: TestClient) -> None:
    resp = inst_client.post("/api/v1/installations/connect", json={})
    assert resp.status_code == 400
    assert resp.json()["code"] == "bad_request"


def test_connect_unknown_agent_is_422(inst_client: TestClient) -> None:
    resp = inst_client.post("/api/v1/installations/connect", json={"agent": "nope"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


def test_connect_not_installed_agent_is_422(inst_client: TestClient) -> None:
    inst_client.post("/api/v1/installations/scan")
    resp = inst_client.post("/api/v1/installations/connect", json={"agent": "beta"})
    assert resp.status_code == 422


def test_remove_then_second_remove_is_404(inst_client: TestClient) -> None:
    inst_client.post("/api/v1/installations/scan")
    assert inst_client.delete("/api/v1/installations/alpha").status_code == 200
    assert inst_client.get("/api/v1/installations").json()["metadata"]["count"] == 1
    second = inst_client.delete("/api/v1/installations/alpha")
    assert second.status_code == 404
    assert second.json()["code"] == "not_found"


def test_agent_spec_exposes_runtime(inst_client: TestClient) -> None:
    """Agent 档案的响应体要带 runtime——UI 靠它确认任务会走哪条执行路径。"""
    created = inst_client.post(
        "/api/v1/agents",
        json={
            "name": "codex-dev",
            "role": "developer",
            "description": "走 codex",
            "permissions": ["file.read"],
            "runtime": "codex",
        },
    )
    assert created.status_code == 200
    assert created.json()["data"]["spec"]["runtime"] == "codex"
    listed = inst_client.get("/api/v1/agents").json()["data"]
    assert {a["spec"]["name"]: a["spec"]["runtime"] for a in listed}["codex-dev"] == "codex"


def test_agent_create_rejects_unknown_runtime(inst_client: TestClient) -> None:
    resp = inst_client.post(
        "/api/v1/agents",
        json={"name": "bad", "role": "developer", "runtime": "not-a-runtime"},
    )
    assert resp.status_code == 422
