"""对话上下文上云（第二阶段）测试。

盯四件事：

1. 只传隐私输入以外的可回看类型：`secure_input_result`（含 key_id / key_hint）不过网；
2. 已处理的消息打上 `cloud_uploaded_at`，不再重复进队列；
3. 聊天里手滑贴的密钥命中本地红线，不上传但同样出队；
4. 云端失败只记账，队列原样保留等下一轮。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from flux.cloud.context_sync import CONTEXT_UPLOAD_KINDS, CloudContextSync, looks_secret
from flux.config import Settings
from flux.core.task_engine.repository import MessageDraft, TaskRepository
from flux.db.session import create_engine, create_session_factory


@pytest.fixture()
async def repo(settings: Settings, db_schema: None):
    """复用测试库；用完显式 dispose，避免 aiosqlite 线程活过事件循环。"""
    engine = create_engine(settings.database_url)
    try:
        yield TaskRepository(create_session_factory(engine))
    finally:
        await engine.dispose()


def _creds_file(tmp_path: Path) -> Path:
    path = tmp_path / "device-credentials"
    path.write_text("FLUX_DEVICE_ID=dev-1\nFLUX_DEVICE_TOKEN=tok-1\n", encoding="utf-8")
    return path


def _sync(settings: Settings, repo: TaskRepository, tmp_path: Path) -> CloudContextSync:
    # 公共夹具默认关掉云同步；这些用例要验证上传逻辑本身，显式打开
    enabled = settings.model_copy(update={"context_cloud_sync": True})
    return CloudContextSync(enabled, repo, credentials_path=_creds_file(tmp_path))


async def _seed(repo: TaskRepository) -> None:
    task = await repo.create(
        task_id=uuid.uuid4(), description="重写登录页", priority=1, project_id=None, agent_id=None
    )
    await repo.add_messages(
        task.id,
        [
            MessageDraft(role="user", content="帮我把登录页拆一下"),
            MessageDraft(role="assistant", kind="summary", content="已拆成三个组件"),
            MessageDraft(
                role="assistant",
                kind="secure_input",
                content="请输入你的 Grok API key",
                payload={"title": "Grok API key", "provider": "openai"},
            ),
            MessageDraft(
                role="user",
                kind="secure_input_result",
                content="已安全存储",
                payload={"message_id": "m-1", "key_id": "k-1", "key_hint": "sk-…abcd"},
            ),
        ],
    )


def test_looks_secret_matches_known_token_shapes() -> None:
    assert looks_secret("token: sk-abcdefghijklmnop123456")
    assert looks_secret("ghp_" + "a" * 30)
    assert looks_secret("-----BEGIN OPENSSH PRIVATE KEY-----")
    # 正常技术讨论不能被误伤
    assert not looks_secret("把 api_key 从环境变量读出来")
    assert not looks_secret("model = gpt-5.1")


async def test_push_uploads_context_but_not_secure_result(
    settings: Settings, repo: TaskRepository, tmp_path: Path
) -> None:
    await _seed(repo)
    sync = _sync(settings, repo, tmp_path)
    sent: list[dict[str, Any]] = []

    async def fake_request(method: str, path: str, creds: Any, *, body=None):
        sent.append(body)
        return {"accepted": len(body["items"]), "skipped": 0}

    sync._request = fake_request  # type: ignore[method-assign]

    result = await sync.push()

    assert result == {"ok": True, "pushed": 3, "skipped": 0}
    kinds = [item["kind"] for item in sent[0]["items"]]
    assert kinds == ["text", "summary", "secure_input"]
    assert all(item["task_uid"] for item in sent[0]["items"])
    # 队列排空：再推一次不会再发请求
    sent.clear()
    again = await sync.push()
    assert again == {"ok": True, "pushed": 0, "skipped": 0}
    assert sent == []


async def test_secret_in_chat_is_skipped_but_dequeued(
    settings: Settings, repo: TaskRepository, tmp_path: Path
) -> None:
    task = await repo.create(
        task_id=uuid.uuid4(), description="配 key", priority=1, project_id=None, agent_id=None
    )
    await repo.add_messages(
        task.id,
        [MessageDraft(role="user", content="我的 key 是 sk-abcdefghijklmnop123456")],
    )
    sync = _sync(settings, repo, tmp_path)
    sent: list[dict[str, Any]] = []

    async def fake_request(method: str, path: str, creds: Any, *, body=None):
        sent.append(body)
        return {"accepted": len(body["items"]), "skipped": 0}

    sync._request = fake_request  # type: ignore[method-assign]

    result = await sync.push()

    assert result == {"ok": True, "pushed": 0, "skipped": 1}
    assert sent == []
    assert await repo.pending_context_messages(CONTEXT_UPLOAD_KINDS) == []


async def test_cloud_failure_keeps_queue(
    settings: Settings, repo: TaskRepository, tmp_path: Path
) -> None:
    await _seed(repo)
    sync = _sync(settings, repo, tmp_path)

    async def failing_request(method: str, path: str, creds: Any, *, body=None):
        raise RuntimeError("账号云返回 500：boom")

    sync._request = failing_request  # type: ignore[method-assign]

    result = await sync.push()

    assert result["ok"] is False
    assert "500" in str(result["error"])
    # 失败不丢消息：下一轮还会重试这 3 条可上传消息
    assert len(await repo.pending_context_messages(CONTEXT_UPLOAD_KINDS)) == 3


async def test_disabled_without_credentials(
    settings: Settings, repo: TaskRepository, tmp_path: Path
) -> None:
    enabled = settings.model_copy(update={"context_cloud_sync": True})
    sync = CloudContextSync(enabled, repo, credentials_path=tmp_path / "missing")
    result = await sync.push()
    assert result["ok"] is False
    assert result["pushed"] == 0
