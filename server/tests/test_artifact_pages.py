"""制品页面（Bot 产出的可分享静态页面）测试。

覆盖：CRUD、保存即新增版本且旧版本可读、发布 / 下线与公开链接的 200/404、
列表过滤、参数校验（空标题 / 超大正文 / 非法 id）、公开面的内容类型与 CSP、
删除后公开链接失效，以及公开路由在 Bot 节点上同样免鉴权挂载。
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from flux.bot_node.main import create_app as create_bot_app
from flux.config import Settings
from flux.core.pages.service import MAX_BODY_BYTES

PREFIX = "/api/v1"
PUBLIC_PREFIX = "/api/v1/public/pages"


def _create_bot(client: TestClient, name: str = "页面 Bot") -> dict:
    response = client.post(f"{PREFIX}/bots", json={"name": name, "description": "发布页面"})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _create_page(client: TestClient, **overrides: object) -> dict:
    payload: dict[str, object] = {
        "title": "每周构建报告",
        "body": "# 构建通过\n\n共 42 个用例。",
        "content_type": "markdown",
    }
    payload.update(overrides)
    response = client.post(f"{PREFIX}/pages", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _publish(client: TestClient, page_id: str) -> dict:
    response = client.post(f"{PREFIX}/pages/{page_id}/publish")
    assert response.status_code == 200, response.text
    return response.json()["data"]


# --- CRUD 与版本 ---


def test_create_page_returns_first_revision(client: TestClient) -> None:
    bot = _create_bot(client)
    page = _create_page(client, bot_id=bot["id"])

    assert page["status"] == "draft"
    assert page["current_revision"] == 1
    assert page["revision"] == 1
    assert page["bot_id"] == bot["id"]
    assert page["content_type"] == "text/markdown"
    assert page["body"].startswith("# 构建通过")
    # 公开令牌不可猜测，且不是自增 id
    assert len(page["share_token"]) >= 32
    assert page["share_token"] != page["id"]
    assert page["public_path"].endswith(page["share_token"])


def test_update_appends_revision_and_keeps_old_body(client: TestClient) -> None:
    page = _create_page(client)
    page_id = page["id"]

    patched = client.patch(f"{PREFIX}/pages/{page_id}", json={"body": "第二版正文"})
    assert patched.status_code == 200, patched.text
    updated = patched.json()["data"]
    assert updated["current_revision"] == 2
    assert updated["body"] == "第二版正文"

    # 详情始终指向当前版本
    detail = client.get(f"{PREFIX}/pages/{page_id}").json()["data"]
    assert detail["revision"] == 2
    assert detail["body"] == "第二版正文"

    # 旧版本一字未改，仍可单独读取
    old = client.get(f"{PREFIX}/pages/{page_id}/revisions/1")
    assert old.status_code == 200, old.text
    assert old.json()["data"]["body"] == "# 构建通过\n\n共 42 个用例。"

    history = client.get(f"{PREFIX}/pages/{page_id}/revisions").json()["data"]
    assert [item["revision"] for item in history] == [1, 2]
    # 列表不带正文，只给字节数
    assert all("body" not in item for item in history)
    assert all(item["body_bytes"] > 0 for item in history)


def test_update_without_changes_does_not_bump_revision(client: TestClient) -> None:
    page = _create_page(client)
    page_id = page["id"]

    patched = client.patch(
        f"{PREFIX}/pages/{page_id}", json={"title": page["title"], "body": page["body"]}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["current_revision"] == 1


def test_title_update_creates_new_revision(client: TestClient) -> None:
    page = _create_page(client)
    patched = client.patch(f"{PREFIX}/pages/{page['id']}", json={"title": "新的标题"})
    assert patched.status_code == 200, patched.text
    data = patched.json()["data"]
    assert data["title"] == "新的标题"
    assert data["current_revision"] == 2
    # 正文没变，但标题随版本一起快照
    assert data["body"] == page["body"]
    old = client.get(f"{PREFIX}/pages/{page['id']}/revisions/1").json()["data"]
    assert old["title"] == "每周构建报告"


def test_list_filters_by_bot_and_status(client: TestClient) -> None:
    first = _create_bot(client, "一号 Bot")
    second = _create_bot(client, "二号 Bot")
    published = _create_page(client, bot_id=first["id"], title="已发布", publish=True)
    _create_page(client, bot_id=second["id"], title="草稿一")
    _create_page(client, bot_id=second["id"], title="草稿二")

    by_bot = client.get(f"{PREFIX}/pages", params={"bot_id": first["id"]})
    assert by_bot.status_code == 200, by_bot.text
    assert [row["id"] for row in by_bot.json()["data"]] == [published["id"]]

    published_only = client.get(f"{PREFIX}/pages", params={"status": "published"})
    assert [row["id"] for row in published_only.json()["data"]] == [published["id"]]

    limited = client.get(f"{PREFIX}/pages", params={"bot_id": second["id"], "limit": 1})
    assert len(limited.json()["data"]) == 1
    assert limited.json()["metadata"]["count"] == 1

    # 列表不带正文
    assert "body" not in limited.json()["data"][0]


# --- 发布 / 下线 / 公开读取 ---


def test_public_get_only_serves_published_pages(client: TestClient) -> None:
    page = _create_page(client)
    token = page["share_token"]

    draft = client.get(f"{PUBLIC_PREFIX}/{token}")
    assert draft.status_code == 404

    _publish(client, page["id"])
    served = client.get(f"{PUBLIC_PREFIX}/{token}")
    assert served.status_code == 200, served.text
    assert served.text == page["body"]
    assert served.headers["content-type"].startswith("text/plain")
    assert served.headers["etag"] == f'"{page["id"]}-r1"'
    assert served.headers["x-artifact-page-revision"] == "1"
    assert served.headers["x-content-type-options"] == "nosniff"

    unpublished = client.post(f"{PREFIX}/pages/{page['id']}/unpublish")
    assert unpublished.status_code == 200, unpublished.text
    assert unpublished.json()["data"]["status"] == "unpublished"
    assert client.get(f"{PUBLIC_PREFIX}/{token}").status_code == 404

    # 未知令牌同样是 404，不泄露存在性
    assert client.get(f"{PUBLIC_PREFIX}/does-not-exist").status_code == 404


def test_public_get_follows_current_revision(client: TestClient) -> None:
    page = _create_page(client, publish=True)
    token = page["share_token"]

    client.patch(f"{PREFIX}/pages/{page['id']}", json={"body": "更新后的正文"})
    served = client.get(f"{PUBLIC_PREFIX}/{token}")
    assert served.status_code == 200
    assert served.text == "更新后的正文"
    assert served.headers["etag"] == f'"{page["id"]}-r2"'
    assert served.headers["x-artifact-page-revision"] == "2"

    # 旧版本仍可从管理面读取
    old = client.get(f"{PREFIX}/pages/{page['id']}/revisions/1")
    assert old.json()["data"]["body"] == page["body"]


def test_html_page_is_served_with_csp(client: TestClient) -> None:
    page = _create_page(
        client,
        title="HTML 报告",
        body="<h1>报告</h1><p>正文</p><script>alert(1)</script>",
        content_type="html",
        publish=True,
    )
    served = client.get(f"{PUBLIC_PREFIX}/{page['share_token']}")
    assert served.status_code == 200, served.text
    assert served.headers["content-type"].startswith("text/html")
    csp = served.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "img-src https: data:" in csp
    assert "style-src 'unsafe-inline'" in csp
    assert "script-src" not in csp


def test_markdown_is_served_as_plain_text_not_html(client: TestClient) -> None:
    page = _create_page(
        client,
        body="# 标题\n\n<script>alert(1)</script>",
        content_type="markdown",
        publish=True,
    )
    served = client.get(f"{PUBLIC_PREFIX}/{page['share_token']}")
    assert served.status_code == 200
    # 原始标签保留为文本，但 content-type 是 text/plain，浏览器不会执行它
    assert served.headers["content-type"].startswith("text/plain")
    assert "<script>alert(1)</script>" in served.text


# --- 校验 ---


def test_empty_title_is_rejected(client: TestClient) -> None:
    response = client.post(f"{PREFIX}/pages", json={"title": "", "body": "正文"})
    assert response.status_code == 422
    assert response.json()["success"] is False

    spaced = client.post(f"{PREFIX}/pages", json={"title": "   ", "body": "正文"})
    assert spaced.status_code == 422


def test_oversized_body_is_rejected_by_bytes(client: TestClient) -> None:
    # 多字节正文：字符数低于上限，但 UTF-8 字节数超过 256KiB，必须按字节拒绝
    big = "汉" * (MAX_BODY_BYTES // 3 + 1)
    assert len(big) < MAX_BODY_BYTES
    response = client.post(f"{PREFIX}/pages", json={"title": "太大", "body": big})
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"


def test_bad_ids_and_filters_are_4xx(client: TestClient) -> None:
    assert client.get(f"{PREFIX}/pages/not-a-uuid").status_code == 422
    assert client.get(f"{PREFIX}/pages", params={"bot_id": "not-a-uuid"}).status_code == 400
    assert client.get(f"{PREFIX}/pages", params={"status": "bogus"}).status_code == 422
    assert client.get(f"{PREFIX}/pages", params={"limit": 0}).status_code == 422


def test_unknown_bot_and_bad_content_type_are_rejected(client: TestClient) -> None:
    unknown = client.post(
        f"{PREFIX}/pages",
        json={"bot_id": str(uuid.uuid4()), "title": "标题", "body": "正文"},
    )
    assert unknown.status_code == 404

    bad_type = client.post(
        f"{PREFIX}/pages",
        json={"title": "标题", "body": "正文", "content_type": "application/javascript"},
    )
    assert bad_type.status_code == 422


def test_empty_patch_and_bad_slug_are_rejected(client: TestClient) -> None:
    page = _create_page(client)
    empty = client.patch(f"{PREFIX}/pages/{page['id']}", json={})
    assert empty.status_code == 422

    bad_slug = client.post(
        f"{PREFIX}/pages",
        json={"title": "标题", "body": "正文", "slug": "Not A Slug!"},
    )
    assert bad_slug.status_code == 422

    duplicate = client.post(
        f"{PREFIX}/pages",
        json={"title": "另一页", "body": "正文", "slug": "weekly-report"},
    )
    assert duplicate.status_code == 200, duplicate.text
    conflict = client.post(
        f"{PREFIX}/pages",
        json={"title": "再来一页", "body": "正文", "slug": "weekly-report"},
    )
    assert conflict.status_code == 409


# --- 删除 ---


def test_delete_removes_page_revisions_and_public_access(client: TestClient) -> None:
    page = _create_page(client, publish=True)
    token = page["share_token"]
    assert client.get(f"{PUBLIC_PREFIX}/{token}").status_code == 200

    deleted = client.delete(f"{PREFIX}/pages/{page['id']}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["deleted"] == page["id"]

    assert client.get(f"{PUBLIC_PREFIX}/{token}").status_code == 404
    assert client.get(f"{PREFIX}/pages/{page['id']}").status_code == 404
    assert client.get(f"{PREFIX}/pages/{page['id']}/revisions").status_code == 404
    assert client.delete(f"{PREFIX}/pages/{page['id']}").status_code == 404


# --- Bot 节点挂载 ---


def test_public_route_is_mounted_unauthenticated_on_bot_node(
    settings: Settings, db_schema: None
) -> None:
    """分享链接在 Bot 节点上也要能打开：公开路由免鉴权，管理路由仍要令牌。"""
    app = create_bot_app(settings)
    with TestClient(app, client=("203.0.113.9", 41234)) as client:
        public = client.get(f"{PUBLIC_PREFIX}/unknown-token")
        assert public.status_code == 404
        assert public.json()["code"] == "not_found"

        assert client.get(f"{PREFIX}/pages").status_code == 401
