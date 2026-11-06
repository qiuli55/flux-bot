"""OpenAPI 契约测试：接口清单必须与主规格 §12 逐条对齐。"""

from __future__ import annotations

from fastapi.testclient import TestClient

# 主规格 §12.3–§12.7 定义的路径与 HTTP 方法
SPEC_ENDPOINTS: dict[str, set[str]] = {
    "/api/v1/agents": {"get", "post"},
    "/api/v1/agents/{agent_id}": {"get"},
    "/api/v1/tasks": {"post"},
    "/api/v1/tasks/{task_id}": {"get"},
    "/api/v1/tasks/{task_id}/cancel": {"post"},
    "/api/v1/workspace/changes": {"get"},
    "/api/v1/workspace/changes/{change_id}": {"get"},
    "/api/v1/workspace/accept": {"post"},
    "/api/v1/workspace/apply": {"post"},
    "/api/v1/workspace/reject": {"post"},
    "/api/v1/git/status": {"get"},
    "/api/v1/git/diff": {"post"},
    "/api/v1/git/branches": {"get"},
    "/api/v1/git/checkout": {"post"},
    "/api/v1/git/commit": {"post"},
    "/api/v1/projects": {"get", "post"},
    "/api/v1/projects/{project_id}": {"get"},
    "/api/v1/projects/{project_id}/memory": {"get", "post"},
    "/api/v1/projects/{project_id}/memory/context": {"get"},
    "/api/v1/projects/{project_id}/scan": {"post"},
    "/api/v1/models/chat": {"post"},
    "/api/v1/connectors/execute": {"post"},
    "/api/v1/terminal/sessions": {"get", "post"},
    "/api/v1/terminal/sessions/{session_id}": {"get"},
    "/api/v1/terminal/sessions/{session_id}/events": {"get"},
    "/api/v1/terminal/sessions/{session_id}/stream": {"get"},
    "/api/v1/terminal/sessions/{session_id}/commands": {"post"},
    "/api/v1/terminal/sessions/{session_id}/stop": {"post"},
    "/api/v1/terminal/pty/sessions": {"get", "post"},
    "/api/v1/terminal/pty/sessions/{session_id}": {"get"},
    "/api/v1/terminal/pty/sessions/{session_id}/stop": {"post"},
}


def _schema(client: TestClient) -> dict:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    return response.json()


def test_all_spec_endpoints_are_exposed(client: TestClient) -> None:
    paths = _schema(client)["paths"]
    for path, methods in SPEC_ENDPOINTS.items():
        assert path in paths, f"缺少主规格定义的路径 {path}"
        assert methods <= set(paths[path]), f"{path} 缺少方法 {methods - set(paths[path])}"


def test_every_operation_has_documented_response(client: TestClient) -> None:
    """§12.10：所有 API 必须具备文档（OpenAPI 中每个操作都要有 summary 与响应定义）。"""
    for path, operations in _schema(client)["paths"].items():
        for method, operation in operations.items():
            assert operation.get("summary"), f"{method.upper()} {path} 缺少 summary"
            assert operation.get("responses"), f"{method.upper()} {path} 缺少响应定义"


def test_health_endpoints_present(client: TestClient) -> None:
    paths = _schema(client)["paths"]
    assert "/api/v1/health" in paths
    assert "/api/v1/health/ready" in paths
