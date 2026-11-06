"""账号级用户记忆：云端合并、运行中记忆写入、执行指令注入与设置接口。"""

from __future__ import annotations

import asyncio
import types
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flux.cloud.memory_sync import load_device_credentials
from flux.container import Container
from flux.core.mcp.auth import AgentIdentity
from flux.core.mcp.tools.base import ToolContext
from flux.core.mcp.tools.memory_tools import memory_remember
from flux.enums import Capability, MemoryLayer
from flux.errors import ValidationError
from flux.main import create_app

from .conftest import issue_token


def test_load_device_credentials(tmp_path: Path) -> None:
    path = tmp_path / "creds"
    path.write_text(
        "# 设备身份\nFLUX_DEVICE_ID=dev_abc\nFLUX_DEVICE_TOKEN='tok-123'\n", encoding="utf-8"
    )
    creds = load_device_credentials(path)
    assert creds is not None
    assert creds.device_id == "dev_abc"
    assert creds.token == "tok-123"
    assert load_device_credentials(tmp_path / "missing") is None


def test_apply_cloud_snapshot_merge_update_and_remove(container: Container) -> None:
    first = asyncio.run(container.memory.write_user(content="偏好中文，结论先行", source="auto"))
    keep = asyncio.run(
        container.memory.write_user(
            content="不要自动提交 git", source="user", meta={"cloud_id": "7"}
        )
    )
    asyncio.run(container.memory.update_entry_meta(first.id, {"cloud_id": "3"}))

    result = asyncio.run(
        container.memory.apply_cloud_snapshot(
            [
                {"id": 3, "content": "偏好中文，先给结论再展开", "source": "auto"},
                {"id": 9, "content": "常用 Python 与 Kotlin", "source": "auto"},
            ]
        )
    )
    assert result["updated"] == 1
    assert result["inserted"] == 1
    # 云端快照里没有 cloud_id=7 → 该条在别处被删除，本地缓存一并清掉
    assert result["removed"] == 1

    entries = asyncio.run(container.memory.list_entries(layer=MemoryLayer.USER))
    contents = {entry.content for entry in entries}
    assert "偏好中文，先给结论再展开" in contents
    assert "常用 Python 与 Kotlin" in contents
    assert "不要自动提交 git" not in contents
    assert keep.content not in contents


def test_update_user_memory_or_marks_stale(container: Container) -> None:
    entry = asyncio.run(container.memory.write_user(content="旧的偏好"))
    updated = asyncio.run(container.memory.update_user(entry.id, content="新的偏好"))
    assert updated.content == "新的偏好"

    asyncio.run(container.memory.update_entry_meta(updated.id, {"cloud_id": "88"}))
    # 测试环境已关闭账号云：同步失败 → 记下旧行，等下一次同步重推后清掉
    same = asyncio.run(container.memory.update_user(updated.id, content="再改一次"))
    assert same.content == "再改一次"
    assert (same.meta or {}).get("cloud_id") is None
    assert (same.meta or {}).get("stale_cloud_id") == "88"


def _tool_ctx(container: Container) -> ToolContext:
    return ToolContext(
        container=container,
        identity=AgentIdentity(
            agent_id=str(uuid.uuid4()), token_id="t", scopes=frozenset({Capability.FILE_READ})
        ),
        tool_name="memory.remember",
    )


def test_memory_remember_writes_and_dedupes(container: Container) -> None:
    ctx = _tool_ctx(container)
    first = asyncio.run(memory_remember(ctx, {"content": "回答先给结论，再展开细节"}))
    assert first["saved"] is True and first["duplicate"] is False

    again = asyncio.run(memory_remember(ctx, {"content": "  回答先给结论，再展开细节 "}))
    assert again["saved"] is False and again["duplicate"] is True
    assert again["id"] == first["id"]

    entries = asyncio.run(container.memory.list_entries(layer=MemoryLayer.USER))
    assert [entry.content for entry in entries] == ["回答先给结论，再展开细节"]
    assert (entries[0].meta or {}).get("auto") is True
    assert (entries[0].meta or {}).get("origin") == "agent"


def test_memory_remember_rejects_secrets_and_long_content(container: Container) -> None:
    ctx = _tool_ctx(container)
    with pytest.raises(ValidationError):
        asyncio.run(memory_remember(ctx, {"content": "密钥 sk-abcdefghijklmnopqrstuvwx1234"}))
    with pytest.raises(ValidationError):
        asyncio.run(memory_remember(ctx, {"content": "啰" * 201}))
    assert asyncio.run(container.memory.list_entries(layer=MemoryLayer.USER)) == []


def test_execution_instruction_injects_user_memory() -> None:
    from flux.core.agent_runtime.runtimes.service import _execution_instruction

    task = types.SimpleNamespace(id=uuid.uuid4(), project_id=None, description="写一个接口")
    text = _execution_instruction(
        task,
        None,
        [],
        user_memory="## 用户长期记忆（账号级，跨设备；仅作背景，不要逐条复述）\n- 偏好中文",
    )
    assert "用户长期记忆" in text
    assert "偏好中文" in text


def test_instruction_asks_agent_to_remember_only_when_enabled(container: Container) -> None:
    from flux.core.agent_runtime.runtimes.service import _user_memory_instruction

    container.memory_prefs.set_auto_capture(True)
    text = asyncio.run(_user_memory_instruction(container))
    assert "memory.remember" in text

    container.memory_prefs.set_auto_capture(False)
    text = asyncio.run(_user_memory_instruction(container))
    assert "memory.remember" not in text


def test_cloud_sync_push_and_pull(container: Container, tmp_path: Path) -> None:
    from flux.cloud.memory_sync import CloudMemorySync, DeviceCredentials

    class FakeSync(CloudMemorySync):
        def credentials(self) -> Any:
            return DeviceCredentials("dev_test", "token")

    settings = types.SimpleNamespace(
        memory_cloud_sync=True,
        cloud_base_url="https://cloud.invalid",
        device_credentials_path=tmp_path / "creds",
    )
    sync = FakeSync(settings, container.memory)  # type: ignore[arg-type]
    calls: list[tuple[str, str]] = []

    async def fake_request(method: str, path: str, creds: Any, *, body: dict | None = None) -> Any:
        calls.append((method, path))
        if method == "POST":
            return {"items": [{"id": 41}], "count": 1, "created": 1}
        if method == "GET":
            return [
                {"id": 41, "content": "偏好中文，结论先行（云端）", "source": "auto"},
                {"id": 42, "content": "常用 Python", "source": "auto"},
            ]
        return {"ok": True}

    sync._request = fake_request  # type: ignore[method-assign]

    entry = asyncio.run(container.memory.write_user(content="偏好中文，结论先行"))
    pushed = asyncio.run(sync.push_entry(entry))
    assert pushed is not None
    assert (pushed.meta or {}).get("cloud_id") == "41"

    result = asyncio.run(sync.pull())
    assert result == {"inserted": 1, "updated": 1, "removed": 0}
    entries = asyncio.run(container.memory.list_entries(layer=MemoryLayer.USER))
    assert {item.content for item in entries} == {
        "偏好中文，结论先行（云端）",
        "常用 Python",
    }
    assert ("GET", "/api/devices/dev_test/memory") in calls
    assert asyncio.run(sync.delete_remote("41")) is True


def test_memory_settings_api(container: Container) -> None:
    client = TestClient(create_app(container.settings))
    headers = {"Authorization": f"Bearer {issue_token(client)}"}
    got = client.get("/api/v1/memory/settings", headers=headers)
    assert got.status_code == 200
    assert got.json()["data"]["auto_capture"] is True
    assert got.json()["data"]["cloud_ready"] is False  # 测试环境显式关闭账号云

    put = client.put("/api/v1/memory/settings", json={"auto_capture": False}, headers=headers)
    assert put.status_code == 200
    assert (
        client.get("/api/v1/memory/settings", headers=headers).json()["data"]["auto_capture"]
        is False
    )
