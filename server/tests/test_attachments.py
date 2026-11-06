"""聊天附件：上传 → 消息落库 → Agent 指令带路径。

手机端「+」传文件走的就是这条链路：先 POST /uploads 拿 upload_id（此时对话可能还不存在），
再把 id 随消息发出；Agent 拿到的指令里有附件的绝对路径。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from flux.core.agent_runtime.runtimes.service import _execution_instruction
from flux.core.attachment_store import ATTACHMENT_MAX_BYTES

PREFIX = "/api/v1"


def _upload(client: TestClient, name: str, body: bytes, mime: str = "text/plain") -> dict:
    response = client.post(
        f"{PREFIX}/uploads",
        params={"name": name, "mime": mime},
        content=body,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _create_task(client: TestClient, description: str, attachment_ids: list[str]) -> str:
    response = client.post(
        f"{PREFIX}/tasks",
        json={"description": description, "attachment_ids": attachment_ids},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["id"]


def test_upload_roundtrip_writes_file(client: TestClient) -> None:
    data = _upload(client, "测试 报告.txt", "附件正文".encode())
    assert data["name"].endswith(".txt")
    assert data["size"] == len("附件正文".encode())
    assert data["id"] in data["path"]

    from pathlib import Path

    assert Path(data["path"]).read_text(encoding="utf-8") == "附件正文"


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
    # 上传目录里不应留下任何半成品目录
    uploads = client.app.state.container.settings.uploads_dir
    assert list(Path(uploads).glob("*")) == []


def test_task_attachments_are_recorded_and_reach_instruction(client: TestClient) -> None:
    first = _upload(client, "a.txt", b"aaa")
    second = _upload(client, "b.txt", b"bbb")
    task_id = _create_task(client, "看看这两个文件", [first["id"], second["id"]])

    messages = client.get(f"{PREFIX}/tasks/{task_id}/messages").json()["data"]
    attachment_messages = [item for item in messages if item["kind"] == "attachments"]
    assert len(attachment_messages) == 1
    files = attachment_messages[0]["payload"]["files"]
    assert [item["name"] for item in files] == ["a.txt", "b.txt"]

    # Agent 指令：附件以绝对路径出现，模型可以直接读
    instruction = _execution_instruction(
        SimpleNamespace(id=task_id, description="看看这两个文件", project_id=None),
        None,
        [],
        attachments=files,
    )
    assert "## 附件" in instruction
    assert first["path"] in instruction and second["path"] in instruction


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
