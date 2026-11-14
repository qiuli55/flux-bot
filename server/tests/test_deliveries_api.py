"""下发文件的按人鉴权：`GET /deliveries/{id}` 与文件字节只对归属人开放。

规则与上传读取一致：

- 新下发在 `meta.json` 里记录 `cloud_user_id`（由 `file.deliver` 从任务解析）；
- 本人可读元数据与文件流；
- 其他人、未登录、不存在的 id 统一 404，响应体不区分「不存在」与「不是你的」；
- Phase 之前没有归属人的老下发归配置的主用户（`FLUX_OWNER_CLOUD_USER_ID`）。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from flux.config import Settings
from flux.core.delivery_store import create_delivery, deliveries_root, resolve_delivery
from flux.core.mcp.auth import AgentIdentity
from flux.core.mcp.tools.base import ToolContext
from flux.core.mcp.tools.delivery_tools import file_deliver
from flux.enums import Capability, DshRunStatus
from flux.main import create_app
from tests.cloud_jwt import mint_cloud_token

PREFIX = "/api/v1"
#: 只用于「必须带令牌」用例的假令牌，绝不使用真实密钥。
ADMIN_TOKEN = "test-delivery-token-8a41"


def _auth(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_cloud_token(user)}"}


def _make_delivery(
    settings: Settings,
    tmp_path: Path,
    *,
    cloud_user_id: str | None = "1",
    name: str = "报告.txt",
    body: bytes = b"delivery body",
):
    source = tmp_path / f"source-{uuid.uuid4().hex}.txt"
    source.write_bytes(body)
    return create_delivery(
        settings,
        sources=[(name, source)],
        note="一句说明",
        cloud_user_id=cloud_user_id,
    )


def _meta_path(settings: Settings, delivery_id: str) -> Path:
    return deliveries_root(settings) / delivery_id / "meta.json"


def _rewrite_meta(settings: Settings, delivery_id: str, **changes: object) -> None:
    path = _meta_path(settings, delivery_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.update(changes)
    for key, value in list(changes.items()):
        if value is None:
            raw.pop(key, None)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")


def test_owner_reads_metadata_and_file_bytes(
    client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    delivery = _make_delivery(settings, tmp_path, cloud_user_id="1", body=b"owner only")

    response = client.get(f"{PREFIX}/deliveries/{delivery.id}", headers=_auth("1"))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["delivery_id"] == delivery.id
    assert data["files"][0]["name"] == "报告.txt"
    # 客户端 JSON 不带服务端落盘路径。
    serialized = json.dumps(data, ensure_ascii=False)
    assert "path" not in data["files"][0]
    assert str(settings.deliveries_dir) not in serialized

    file_response = client.get(f"{PREFIX}/deliveries/{delivery.id}/files/0", headers=_auth("1"))
    assert file_response.status_code == 200, file_response.text
    assert file_response.content == b"owner only"
    assert file_response.headers["x-content-type-options"] == "nosniff"

    download = client.get(
        f"{PREFIX}/deliveries/{delivery.id}/files/0",
        params={"download": "true"},
        headers=_auth("1"),
    )
    assert download.status_code == 200
    assert download.content == b"owner only"
    assert download.headers["content-disposition"].startswith("attachment;")


def test_cross_user_delivery_read_is_404_without_existence_leak(
    client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    delivery = _make_delivery(settings, tmp_path, cloud_user_id="1")

    for path in (
        f"{PREFIX}/deliveries/{delivery.id}",
        f"{PREFIX}/deliveries/{delivery.id}/files/0",
    ):
        other = client.get(path, headers=_auth("2"))
        missing = client.get(path.replace(delivery.id, uuid.uuid4().hex), headers=_auth("2"))
        assert other.status_code == 404, path
        assert missing.status_code == 404, path
        # 同一个 404 响应体：不确认 id 是否存在，也不回显 path / 归属人。
        assert other.json() == missing.json(), path
        body = json.dumps(other.json(), ensure_ascii=False)
        assert delivery.id not in body
        assert "path" not in body


def test_delivery_read_requires_rest_auth(
    settings: Settings, db_schema: None, tmp_path: Path
) -> None:
    secured = settings.model_copy(update={"auth_token": ADMIN_TOKEN})
    app = create_app(secured)
    with TestClient(app, client=("203.0.113.9", 41234)) as locked:
        delivery = _make_delivery(settings, tmp_path, cloud_user_id="1")
        for path in (
            f"{PREFIX}/deliveries/{delivery.id}",
            f"{PREFIX}/deliveries/{delivery.id}/files/0",
        ):
            assert locked.get(path).status_code == 401, path
            wrong = locked.get(path, headers={"Authorization": "Bearer wrong-token"})
            assert wrong.status_code == 401, path
            assert wrong.json()["code"] == "unauthenticated"


def test_legacy_delivery_without_owner_is_owner_only(
    client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    delivery = _make_delivery(settings, tmp_path, cloud_user_id=None)
    raw = json.loads(_meta_path(settings, delivery.id).read_text(encoding="utf-8"))
    assert "cloud_user_id" not in raw
    assert resolve_delivery(settings, delivery.id).cloud_user_id is None

    owner = client.get(f"{PREFIX}/deliveries/{delivery.id}", headers=_auth("1"))
    assert owner.status_code == 200, owner.text
    owner_file = client.get(f"{PREFIX}/deliveries/{delivery.id}/files/0", headers=_auth("1"))
    assert owner_file.status_code == 200

    other = client.get(f"{PREFIX}/deliveries/{delivery.id}", headers=_auth("2"))
    assert other.status_code == 404
    other_file = client.get(f"{PREFIX}/deliveries/{delivery.id}/files/0", headers=_auth("2"))
    assert other_file.status_code == 404


def test_legacy_null_owner_maps_to_configured_owner(
    client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    delivery = _make_delivery(settings, tmp_path, cloud_user_id="1")
    _rewrite_meta(settings, delivery.id, cloud_user_id=None)
    assert resolve_delivery(settings, delivery.id).cloud_user_id is None

    assert client.get(f"{PREFIX}/deliveries/{delivery.id}", headers=_auth("1")).status_code == 200
    assert client.get(f"{PREFIX}/deliveries/{delivery.id}", headers=_auth("2")).status_code == 404


def test_new_delivery_persists_and_enforces_cloud_user(
    client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    delivery = _make_delivery(settings, tmp_path, cloud_user_id="2")

    raw = json.loads(_meta_path(settings, delivery.id).read_text(encoding="utf-8"))
    assert raw["cloud_user_id"] == "2"
    assert resolve_delivery(settings, delivery.id).cloud_user_id == "2"

    owner = client.get(f"{PREFIX}/deliveries/{delivery.id}", headers=_auth("2"))
    assert owner.status_code == 200
    # 显式归属优先：即使主用户也不能读别人的下发。
    other = client.get(f"{PREFIX}/deliveries/{delivery.id}", headers=_auth("1"))
    assert other.status_code == 404


async def test_file_deliver_records_task_cloud_user(apply_container, workspace_root: Path) -> None:
    target = workspace_root / "成品.txt"
    target.write_text("做好的成品", encoding="utf-8")
    task = await apply_container.task_repo.create(
        task_id=uuid.uuid4(),
        description="下发用例",
        priority=1,
        project_id=None,
        agent_id=None,
        cloud_user_id="2",
    )
    agent_id = str(uuid.uuid4())
    run_id = uuid.uuid4().hex
    await apply_container.run_repo.create(
        run_id=run_id,
        session_id="sess",
        instruction="deliver the file",
        status=DshRunStatus.RUNNING,
        agent_id=agent_id,
        task_id=task.id,
        permission_mode="normal",
    )
    ctx = ToolContext(
        container=apply_container,
        identity=AgentIdentity(
            agent_id=agent_id,
            token_id="t",
            scopes=frozenset({Capability.FILE_WRITE}),
            run_id=run_id,
        ),
        tool_name="file.deliver",
    )

    result = await file_deliver(ctx, {"paths": ["成品.txt"], "note": "请查收"})

    resolved = resolve_delivery(apply_container.settings, result["delivery_id"])
    assert resolved is not None
    assert resolved.cloud_user_id == "2"
    raw = json.loads(
        _meta_path(apply_container.settings, result["delivery_id"]).read_text(encoding="utf-8")
    )
    assert raw["cloud_user_id"] == "2"
    # 文件卡片消息依旧只带清单，不带服务端路径。
    messages, _ = await apply_container.task_repo.list_messages(task.id, limit=10)
    payload = json.dumps(messages[-1].payload, ensure_ascii=False)
    assert str(apply_container.settings.deliveries_dir) not in payload
    assert "path" not in messages[-1].payload["files"][0]


async def test_file_deliver_legacy_task_falls_back_to_owner(
    apply_container, workspace_root: Path
) -> None:
    (workspace_root / "老对话.txt").write_text("老任务产出的文件", encoding="utf-8")
    task = await apply_container.task_repo.create(
        task_id=uuid.uuid4(),
        description="Phase 之前的老任务",
        priority=1,
        project_id=None,
        agent_id=None,
        cloud_user_id=None,
    )
    agent_id = str(uuid.uuid4())
    run_id = uuid.uuid4().hex
    await apply_container.run_repo.create(
        run_id=run_id,
        session_id="sess",
        instruction="deliver the file",
        status=DshRunStatus.RUNNING,
        agent_id=agent_id,
        task_id=task.id,
        permission_mode="normal",
    )
    ctx = ToolContext(
        container=apply_container,
        identity=AgentIdentity(
            agent_id=agent_id,
            token_id="t",
            scopes=frozenset({Capability.FILE_WRITE}),
            run_id=run_id,
        ),
        tool_name="file.deliver",
    )

    result = await file_deliver(ctx, {"paths": ["老对话.txt"]})

    resolved = resolve_delivery(apply_container.settings, result["delivery_id"])
    assert resolved is not None
    assert resolved.cloud_user_id == apply_container.settings.owner_cloud_user_id == "1"
