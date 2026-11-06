"""设计工作室代理 API 测试。

上游 Luma Studio 用 httpx.MockTransport 顶替，不依赖本机 8091 是否在跑。
断言重点：统一响应体、按项目过滤、错误码翻译、AI 请求原样转发。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from flux.api.v1 import designs
from flux.config import Settings
from flux.main import create_app

PREFIX = "/api/v1"


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]):
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, base_url="http://design.test")


@pytest.fixture()
def design_app(settings: Settings, db_schema: None):
    """返回 (app, 安装上游 handler 的函数)。"""
    app = create_app(settings)
    holder: dict[str, Callable[[httpx.Request], httpx.Response]] = {}

    def override():
        return _mock_client(holder["handler"])

    app.dependency_overrides[designs.design_client] = override
    yield app, holder
    app.dependency_overrides.clear()


@pytest.fixture()
def design_client_app(design_app) -> Iterator[tuple[TestClient, dict]]:
    app, holder = design_app
    with TestClient(app) as test_client:
        yield test_client, holder


def test_list_designs_filters_by_project(design_client_app) -> None:
    test_client, holder = design_client_app
    holder["handler"] = lambda request: httpx.Response(
        200,
        json={
            "ok": True,
            "designs": [
                {"id": "a-one", "name": "A", "project_id": "p-a"},
                {"id": "b-two", "name": "B", "project_id": "p-b"},
            ],
        },
    )
    response = test_client.get(f"{PREFIX}/designs", params={"project_id": "p-a"})
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"] == [{"id": "a-one", "name": "A", "project_id": "p-a"}]
    assert body["metadata"]["count"] == 1


def test_get_design_missing_maps_to_not_found(design_client_app) -> None:
    test_client, holder = design_client_app
    holder["handler"] = lambda request: httpx.Response(
        404, json={"ok": False, "error": "设计稿不存在"}
    )
    response = test_client.get(f"{PREFIX}/designs/missing-one")
    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "not_found"
    assert body["message"] == "设计稿不存在"


def test_save_design_conflict_keeps_current_version(design_client_app) -> None:
    test_client, holder = design_client_app
    holder["handler"] = lambda request: httpx.Response(
        409,
        json={"ok": False, "error": "设计稿已在别处更新，请刷新后再改", "version": 7},
    )
    response = test_client.put(
        f"{PREFIX}/designs/luma-home",
        json={"base_version": 3, "design": {"screens": []}},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "design_conflict"
    assert body["data"] == {"version": 7}


def test_save_design_forwards_base_version(design_client_app) -> None:
    test_client, holder = design_client_app
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/luma-home") and request.method == "PUT":
            import json

            seen.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={"ok": True, "design": {"id": "luma-home", "version": 4}},
            )
        return httpx.Response(404, json={"ok": False, "error": "unexpected"})

    holder["handler"] = handler
    response = test_client.put(
        f"{PREFIX}/designs/luma-home",
        json={"base_version": 3, "design": {"name": "Luma", "screens": [{"id": "s1"}]}},
    )
    assert response.status_code == 200
    assert response.json()["data"]["version"] == 4
    assert response.json()["metadata"]["version"] == 4
    assert seen["base_version"] == 3
    assert seen["design"]["name"] == "Luma"


def test_create_design_forwards_project(design_client_app) -> None:
    test_client, holder = design_client_app
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(
            201,
            json={
                "ok": True,
                "design": {"id": "draft-abc12345", "version": 1},
                "summary": {"screen_count": 1},
            },
        )

    holder["handler"] = handler
    response = test_client.post(
        f"{PREFIX}/designs",
        json={"name": "新页面", "project_id": "p-a", "project": "Flux 移动端"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["id"] == "draft-abc12345"
    assert body["metadata"]["summary"] == {"screen_count": 1}
    assert seen["project_id"] == "p-a"


def test_ask_ai_forwards_node_and_prompt(design_client_app) -> None:
    test_client, holder = design_client_app
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        if request.method == "GET":
            # 转发前会补画板底色：查一次设计稿元信息
            return httpx.Response(
                200, json={"ok": True, "design": {"id": "luma-home", "bg": "#101418"}}
            )
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "reply": "圆角调大到 22",
                "ops": [{"op": "set", "target": "n1", "set": {"radius": 22}}],
                "model": "deepseek-flash",
                "mode": "node",
            },
        )

    holder["handler"] = handler
    response = test_client.post(
        f"{PREFIX}/designs/luma-home/ai",
        json={
            "nodes": [{"id": "n1", "type": "button"}],
            "screen": {"id": "s1", "nodes": []},
            "mode": "node",
            "prompt": "圆角调大",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["reply"] == "圆角调大到 22"
    assert body["data"]["ops"] == [{"op": "set", "target": "n1", "set": {"radius": 22}}]
    assert body["data"]["mode"] == "node"
    assert body["metadata"]["ops"] == 1
    assert seen["mode"] == "node"
    assert seen["nodes"][0]["id"] == "n1"
    assert seen["screen"]["id"] == "s1"
    assert seen["prompt"] == "圆角调大"
    # 转发增强：自动补 design id（多轮历史键）与画板底色（深/浅基调判定）
    assert seen["design"] == "luma-home"
    assert seen["screen"]["bg"] == "#101418"


def test_delete_design_proxies_and_returns_id(design_client_app) -> None:
    test_client, holder = design_client_app
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"ok": True, "id": "luma-home"})

    holder["handler"] = handler
    response = test_client.delete(f"{PREFIX}/designs/luma-home")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"] == {"id": "luma-home"}
    assert seen == {"method": "DELETE", "path": "/api/designs/luma-home"}


def test_delete_design_missing_maps_to_not_found(design_client_app) -> None:
    test_client, holder = design_client_app
    holder["handler"] = lambda request: httpx.Response(
        404, json={"ok": False, "error": "设计稿不存在"}
    )
    response = test_client.delete(f"{PREFIX}/designs/missing-one")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_upstream_connection_error_maps_to_503(design_client_app) -> None:
    test_client, holder = design_client_app

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    holder["handler"] = handler
    response = test_client.get(f"{PREFIX}/designs")
    assert response.status_code == 503
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "design_studio_unavailable"


def test_invalid_design_id_is_rejected_before_upstream(design_client_app) -> None:
    test_client, holder = design_client_app
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"ok": True, "design": {}})

    holder["handler"] = handler
    response = test_client.get(f"{PREFIX}/designs/Bad_ID")
    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"
    assert called is False


def test_list_revisions_proxies_summary(design_client_app) -> None:
    test_client, holder = design_client_app
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "ok": True,
                "revisions": [
                    {"version": 3, "updated_at": "2026-10-27T10:00:00+00:00", "layer_count": 12},
                    {"version": 2, "updated_at": "2026-10-27T09:00:00+00:00", "layer_count": 9},
                ],
            },
        )

    holder["handler"] = handler
    response = test_client.get(f"{PREFIX}/designs/luma-home/revisions")
    assert response.status_code == 200
    body = response.json()
    assert [item["version"] for item in body["data"]] == [3, 2]
    assert body["metadata"]["count"] == 2
    assert seen["path"] == "/api/designs/luma-home/revisions"


def test_get_revision_and_missing(design_client_app) -> None:
    test_client, holder = design_client_app

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/revisions/2"):
            return httpx.Response(
                200,
                json={"ok": True, "version": 2, "design": {"id": "luma-home", "version": 2}},
            )
        return httpx.Response(404, json={"ok": False, "error": "该历史版本不存在"})

    holder["handler"] = handler
    ok_response = test_client.get(f"{PREFIX}/designs/luma-home/revisions/2")
    assert ok_response.status_code == 200
    assert ok_response.json()["data"]["version"] == 2

    missing = test_client.get(f"{PREFIX}/designs/luma-home/revisions/99")
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"


def test_revert_design_forwards_version(design_client_app) -> None:
    test_client, holder = design_client_app
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["method"] = request.method
        seen["path"] = request.url.path
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "design": {"id": "luma-home", "version": 8, "screens": []}},
        )

    holder["handler"] = handler
    response = test_client.post(
        f"{PREFIX}/designs/luma-home/revert",
        json={"version": 5, "base_version": 7},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["version"] == 8
    assert body["metadata"]["version"] == 8
    assert seen == {
        "method": "POST",
        "path": "/api/designs/luma-home/revert",
        "version": 5,
        "base_version": 7,
    }


def test_revert_conflict_keeps_current_version(design_client_app) -> None:
    test_client, holder = design_client_app
    holder["handler"] = lambda request: httpx.Response(
        409,
        json={"ok": False, "error": "设计稿已在别处更新，请刷新后再恢复", "version": 9},
    )
    response = test_client.post(
        f"{PREFIX}/designs/luma-home/revert",
        json={"version": 5, "base_version": 7},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "design_conflict"
    assert body["data"] == {"version": 9}


def test_import_design_streams_raw_body(design_client_app) -> None:
    test_client, holder = design_client_app
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["filename"] = request.url.params.get("filename")
        seen["project"] = request.url.params.get("project")
        seen["body"] = request.content
        return httpx.Response(
            201,
            json={
                "ok": True,
                "design": {"id": "draft-1a2b3c4d", "name": "首页", "screens": [{"id": "s1"}]},
                "summary": {"screen_count": 1, "layer_count": 12},
                "warnings": ["1 个矢量图层已退化为色块"],
            },
        )

    holder["handler"] = handler
    response = test_client.post(
        f"{PREFIX}/designs/import",
        params={"filename": "首页.fig", "project": "Flux 移动端"},
        content=b"PK\x03\x04fake-fig",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["id"] == "draft-1a2b3c4d"
    assert body["metadata"]["summary"]["screen_count"] == 1
    assert body["metadata"]["warnings"] == ["1 个矢量图层已退化为色块"]
    assert seen == {
        "method": "POST",
        "path": "/api/import",
        "filename": "首页.fig",
        "project": "Flux 移动端",
        "body": b"PK\x03\x04fake-fig",
    }


def test_import_design_rejects_unsupported_suffix(design_client_app) -> None:
    test_client, holder = design_client_app
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(201, json={"ok": True, "design": {"id": "draft-x"}})

    holder["handler"] = handler
    response = test_client.post(
        f"{PREFIX}/designs/import",
        params={"filename": "旧稿.xd"},
        content=b"whatever",
    )
    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"
    assert "SVG" in response.json()["message"]
    assert called is False


def test_import_design_translates_parse_error(design_client_app) -> None:
    test_client, holder = design_client_app
    holder["handler"] = lambda request: httpx.Response(
        400, json={"ok": False, "error": "SVG 解析失败：unclosed token"}
    )
    response = test_client.post(
        f"{PREFIX}/designs/import",
        params={"filename": "broken.svg"},
        content=b"<svg><rect>",
    )
    assert response.status_code == 400
    assert response.json()["message"] == "SVG 解析失败：unclosed token"


def test_export_svg_returns_raw_bytes(design_client_app) -> None:
    test_client, holder = design_client_app
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["screen"] = request.url.params.get("screen")
        return httpx.Response(
            200,
            content=b'<svg xmlns="http://www.w3.org/2000/svg"></svg>',
            headers={"Content-Type": "image/svg+xml; charset=utf-8"},
        )

    holder["handler"] = handler
    response = test_client.get(f"{PREFIX}/designs/luma-home/export.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.content.startswith(b"<svg")
    assert seen == {"path": "/api/designs/luma-home/export.svg", "screen": "all"}


def test_export_svg_missing_maps_to_not_found(design_client_app) -> None:
    test_client, holder = design_client_app
    holder["handler"] = lambda request: httpx.Response(
        404, json={"ok": False, "error": "画板不存在"}
    )
    response = test_client.get(f"{PREFIX}/designs/luma-home/export.svg", params={"screen": "99"})
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
