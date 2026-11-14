"""聊天附件：上传 → 归属 → 鉴权读取 → 消息落库 → Agent 指令带路径。

手机端「+」传文件走的就是这条链路：先 POST /uploads 拿 upload_id（此时对话可能还不存在），
再把 id 随消息发出。上传时记录当前登录人，GET /uploads/{id} 只允许归属人读取；
Agent 仍从服务端内部拿到绝对路径。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from flux.config import Settings
from flux.core.agent_runtime.runtimes.service import _execution_instruction
from flux.core.attachment_store import (
    ATTACHMENT_MAX_BYTES,
    resolve_upload,
    uploads_root,
)
from flux.main import create_app
from tests.cloud_jwt import mint_cloud_token

PREFIX = "/api/v1"


def _auth(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_cloud_token(user)}"}


def _upload(
    client: TestClient,
    name: str,
    body: bytes,
    mime: str = "text/plain",
    *,
    user: str | None = None,
) -> dict:
    response = client.post(
        f"{PREFIX}/uploads",
        params={"name": name, "mime": mime},
        content=body,
        headers={"Content-Type": "application/octet-stream", **(_auth(user) if user else {})},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _create_task(
    client: TestClient,
    description: str,
    attachment_ids: list[str],
    *,
    user: str | None = None,
) -> str:
    response = client.post(
        f"{PREFIX}/tasks",
        json={"description": description, "attachment_ids": attachment_ids},
        headers=_auth(user) if user else {},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["id"]


def _meta_path(settings: Settings, upload_id: str) -> Path:
    return uploads_root(settings) / upload_id / "meta.json"


def _rewrite_meta(settings: Settings, upload_id: str, **changes: object) -> None:
    path = _meta_path(settings, upload_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.update(changes)
    for key, value in list(changes.items()):
        if value is None:
            raw.pop(key, None)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")


# --- 上传 ---


def test_upload_roundtrip_writes_file_without_leaking_path(
    client: TestClient, settings: Settings
) -> None:
    data = _upload(client, "测试 报告.txt", "附件正文".encode())
    assert data["name"].endswith(".txt")
    assert data["size"] == len("附件正文".encode())
    assert set(data) == {"id", "name", "size", "mime"}
    assert "path" not in data
    assert str(uploads_root(settings)) not in json.dumps(data, ensure_ascii=False)

    # 服务端内部仍可按 id 还原出落盘路径（Agent 原生读文件用）。
    uploaded = resolve_upload(settings, data["id"])
    assert uploaded is not None
    assert Path(uploaded.path).read_text(encoding="utf-8") == "附件正文"


def test_upload_rejects_empty_body(client: TestClient) -> None:
    response = client.post(f"{PREFIX}/uploads", params={"name": "empty.txt"}, content=b"")
    assert response.status_code >= 400
    assert "空" in response.json()["message"]


def test_upload_rejects_oversize_chunk(client: TestClient) -> None:
    """超限在上传时就拒绝，不等人发消息；半成品文件不留在磁盘。"""
    oversized = b"x" * (ATTACHMENT_MAX_BYTES + 1)
    response = client.post(f"{PREFIX}/uploads", params={"name": "big.bin"}, content=oversized)
    assert response.status_code >= 400
    assert "50MB" in response.json()["message"]
    uploads = client.app.state.container.settings.uploads_dir
    assert list(Path(uploads).glob("*")) == []


# --- 鉴权读取 ---


def test_owner_can_read_upload_bytes(client: TestClient) -> None:
    data = _upload(client, "chart.png", b"\x89PNG\r\n", mime="image/png")

    response = client.get(f"{PREFIX}/uploads/{data['id']}")
    assert response.status_code == 200, response.text
    assert response.content == b"\x89PNG\r\n"
    assert response.headers["content-type"].startswith("image/png")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "private" in response.headers["cache-control"]
    assert response.headers["content-disposition"].startswith("inline;")
    assert "filename*=UTF-8''" in response.headers["content-disposition"]


def test_cross_user_upload_read_is_404_without_existence_leak(client: TestClient) -> None:
    data = _upload(client, "secret.txt", b"owner one only", user="1")

    owner = client.get(f"{PREFIX}/uploads/{data['id']}", headers=_auth("1"))
    assert owner.status_code == 200

    other = client.get(f"{PREFIX}/uploads/{data['id']}", headers=_auth("2"))
    assert other.status_code == 404
    assert "不存在" in other.json()["message"]
    # 不确认 id 是否存在：不存在的 id 与别人的 id 同一错误体。
    missing = client.get(f"{PREFIX}/uploads/{uuid.uuid4().hex}", headers=_auth("2"))
    assert missing.status_code == 404
    assert missing.json()["code"] == other.json()["code"]


def test_upload_read_requires_rest_auth(settings: Settings, db_schema: None) -> None:
    locked = settings.model_copy(update={"auth_token": "dummy-rest-token", "allow_no_auth": False})
    app = create_app(locked)
    with TestClient(app) as locked_client:
        missing_token = locked_client.get(f"{PREFIX}/uploads/{uuid.uuid4().hex}")
        assert missing_token.status_code == 401
        bad_token = locked_client.get(
            f"{PREFIX}/uploads/{uuid.uuid4().hex}",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert bad_token.status_code == 401


def test_cross_user_cannot_attach_someone_elses_upload(client: TestClient) -> None:
    uploaded = _upload(client, "mine.txt", b"mine", user="1")
    response = client.post(
        f"{PREFIX}/tasks",
        json={"description": "偷用", "attachment_ids": [uploaded["id"]]},
        headers=_auth("2"),
    )
    assert response.status_code == 422
    assert "不存在" in response.json()["message"]


# --- 老无主附件 ---


def test_legacy_upload_without_owner_needs_a_visible_message_reference(
    client: TestClient, settings: Settings
) -> None:
    data = _upload(client, "legacy.txt", b"old data")
    _rewrite_meta(settings, data["id"], cloud_user_id=None)

    # 还没有任何消息引用它：即使是主用户也不能读。
    before = client.get(f"{PREFIX}/uploads/{data['id']}", headers=_auth("1"))
    assert before.status_code == 404

    _create_task(client, "看看老附件", [data["id"]], user="1")

    owner = client.get(f"{PREFIX}/uploads/{data['id']}", headers=_auth("1"))
    assert owner.status_code == 200
    assert owner.content == b"old data"

    # 别人即使知道 id 也不能借用主用户消息里的引用。
    other = client.get(f"{PREFIX}/uploads/{data['id']}", headers=_auth("2"))
    assert other.status_code == 404


# --- 路径 / 大小 / 类型护栏 ---


def test_tampered_meta_path_or_id_is_not_served(
    client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    data = _upload(client, "safe.txt", b"safe")
    outside = tmp_path / "outside.txt"
    outside.write_text("must not leak", encoding="utf-8")

    _rewrite_meta(settings, data["id"], path=str(outside))
    assert client.get(f"{PREFIX}/uploads/{data['id']}").status_code == 404

    _rewrite_meta(settings, data["id"], path=None, id="deadbeef")
    assert client.get(f"{PREFIX}/uploads/{data['id']}").status_code == 404


def test_oversize_upload_is_refused(client: TestClient, settings: Settings) -> None:
    data = _upload(client, "large.bin", b"x")
    uploaded = resolve_upload(settings, data["id"])
    assert uploaded is not None
    with Path(uploaded.path).open("r+b") as handle:
        handle.truncate(ATTACHMENT_MAX_BYTES + 1)

    response = client.get(f"{PREFIX}/uploads/{data['id']}")
    assert response.status_code == 422
    assert "最大大小" in response.json()["message"]


def test_active_content_is_forced_to_download_as_octet_stream(client: TestClient) -> None:
    data = _upload(client, "evil.html", b"<script>alert(1)</script>", mime="text/html")

    response = client.get(f"{PREFIX}/uploads/{data['id']}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/octet-stream")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"

    # 即使客户端显式要求内联，主动内容也不内联。
    forced = client.get(f"{PREFIX}/uploads/{data['id']}", params={"download": "false"})
    assert forced.headers["content-disposition"].startswith("attachment;")


def test_download_true_forces_attachment_for_images(client: TestClient) -> None:
    data = _upload(client, "photo.jpg", b"\xff\xd8\xff", mime="image/jpeg")
    response = client.get(f"{PREFIX}/uploads/{data['id']}", params={"download": "true"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.headers["content-disposition"].startswith("attachment;")


# --- 消息 / Agent ---


def test_task_attachments_are_recorded_and_reach_instruction(
    client: TestClient, settings: Settings
) -> None:
    first = _upload(client, "a.txt", b"aaa")
    second = _upload(client, "b.txt", b"bbb")
    task_id = _create_task(client, "看看这两个文件", [first["id"], second["id"]])

    messages = client.get(f"{PREFIX}/tasks/{task_id}/messages").json()["data"]
    attachment_messages = [item for item in messages if item["kind"] == "attachments"]
    assert len(attachment_messages) == 1
    files = attachment_messages[0]["payload"]["files"]
    assert [item["name"] for item in files] == ["a.txt", "b.txt"]
    # 客户端 JSON 不能出现宿主路径。
    assert all("path" not in item for item in files)
    assert str(uploads_root(settings)) not in json.dumps(messages, ensure_ascii=False)

    # Agent 指令：服务端内部仍用绝对路径，模型可以直接读。
    first_path = resolve_upload(settings, first["id"])
    second_path = resolve_upload(settings, second["id"])
    assert first_path is not None and second_path is not None
    instruction = _execution_instruction(
        SimpleNamespace(id=task_id, description="看看这两个文件", project_id=None),
        None,
        [],
        attachments=[
            {"name": "a.txt", "path": first_path.path},
            {"name": "b.txt", "path": second_path.path},
        ],
    )
    assert "## 附件" in instruction
    assert first_path.path in instruction and second_path.path in instruction


def test_message_requires_content_or_attachments(client: TestClient) -> None:
    task_id = _create_task(client, "随便", [])
    response = client.post(f"{PREFIX}/tasks/{task_id}/messages", json={"content": "  "})
    assert response.status_code == 422
    assert "不能为空" in response.json()["message"]


def test_message_with_text_and_attachment_tells_assistant_files(client: TestClient) -> None:
    """文字 + 附件一起发：助手这一轮也要知道附件的存在（附件消息不进助手历史）。"""
    uploaded = _upload(client, "合同.pdf", b"%PDF-1.4 test")
    task_id = _create_task(client, "看下合同", [])
    response = client.post(
        f"{PREFIX}/tasks/{task_id}/messages",
        json={"content": "这份合同有问题吗", "attachment_ids": [uploaded["id"]]},
    )
    assert response.status_code == 200, response.text
    reply = response.json()["data"]["messages"][-1]
    assert "合同.pdf" in reply["content"]


def test_unknown_attachment_id_is_rejected(client: TestClient) -> None:
    response = client.post(
        f"{PREFIX}/tasks",
        json={"description": "看看", "attachment_ids": [uuid.uuid4().hex]},
    )
    assert response.status_code >= 400
    assert "不存在" in response.json()["message"]


def test_more_than_five_attachments_rejected(client: TestClient) -> None:
    ids = [_upload(client, f"{index}.txt", b"x")["id"] for index in range(5)]
    response = client.post(f"{PREFIX}/uploads", params={"name": "extra.txt"}, content=b"y")
    assert response.status_code == 200
    extra = response.json()["data"]["id"]
    response = client.post(
        f"{PREFIX}/tasks", json={"description": "六个", "attachment_ids": ids + [extra]}
    )
    assert response.status_code == 422  # pydantic 的 max_length 先拦下
