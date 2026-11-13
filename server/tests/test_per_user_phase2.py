"""Phase 2 按用户隔离：记忆、API Key、账号连接与用量。

身份来源统一是云 JWT 的 `sub`（测试用 `tests.cloud_jwt` 签发的假令牌）；
`FLUX_OWNER_CLOUD_USER_ID` 作为主用户兜底。历史 NULL / `'default'` 数据只属于
配置的主用户——这些用例把每条边界都真实走一遍 API / 服务层。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from flux.cloud.memory_sync import CloudMemorySync
from flux.config import Settings
from flux.container import Container
from flux.core.billing.repository import BillingRepository
from flux.core.github.service import DEFAULT_ACCOUNT, normalize_account_key
from flux.db.session import create_engine, create_session_factory
from flux.enums import MemoryLayer
from flux.main import create_app
from flux.models.api_key import ApiKey
from flux.models.github import GithubConnection
from flux.models.usage import UsageRecord
from tests.cloud_jwt import mint_cloud_token

PREFIX = "/api/v1"
OWNER = "1"
USER_B = "2"


def _auth(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_cloud_token(user)}"}


@contextmanager
def _client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        yield client


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _session_factory(settings: Settings):
    return create_session_factory(create_engine(settings.database_url))


def _insert_legacy_key(settings: Settings, name: str) -> str:
    """直接写一条 Phase 2 之前的 api_keys 行（cloud_user_id=NULL）。"""

    async def _insert() -> str:
        engine = create_engine(settings.database_url)
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                key_id = uuid.uuid4()
                session.add(
                    ApiKey(
                        id=key_id,
                        name=name,
                        provider="deepseek",
                        base_url="",
                        wire_api="",
                        models=["deepseek-chat"],
                        key_ciphertext=b"legacy-ciphertext",
                        key_hint="••••legacy",
                        enabled=True,
                        cloud_user_id=None,
                    )
                )
                await session.commit()
                return str(key_id)
        finally:
            await engine.dispose()

    return _run(_insert())


def _insert_github(container: Container, account_key: str, login: str) -> None:
    async def _insert() -> None:
        async with container.session_factory() as session:
            session.add(
                GithubConnection(
                    account_key=account_key,
                    login=login,
                    name=login,
                    avatar_url="",
                    token_ciphertext=container.key_vault.seal_text("gho_test_token"),
                    scopes="repo",
                    token_kind="bearer",
                )
            )
            await session.commit()

    _run(_insert())


def _github_rows(settings: Settings) -> dict[str, str]:
    async def _read() -> dict[str, str]:
        engine = create_engine(settings.database_url)
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                rows = list(await session.scalars(select(GithubConnection)))
                return {row.account_key: row.login for row in rows}
        finally:
            await engine.dispose()

    return _run(_read())


def _github_state(client: TestClient, user: str) -> dict[str, Any]:
    rows = client.get(f"{PREFIX}/connectors", headers=_auth(user)).json()["data"]
    return next(row for row in rows if row["id"] == "github")


# --- 1. User 层记忆按人隔离 ---


def test_user_memories_are_isolated_between_cloud_users(
    settings: Settings, db_schema: None
) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": OWNER})
    with _client(cfg) as client:
        created_a = client.post(
            f"{PREFIX}/memory/entries", json={"content": "A 的偏好：先给结论"}, headers=_auth(OWNER)
        )
        assert created_a.status_code == 200, created_a.text
        created_b = client.post(
            f"{PREFIX}/memory/entries",
            json={"content": "B 的偏好：多用示例"},
            headers=_auth(USER_B),
        )
        assert created_b.status_code == 200, created_b.text

        listed_b = client.get(
            f"{PREFIX}/memory/entries", params={"layer": "user"}, headers=_auth(USER_B)
        ).json()["data"]
        assert [entry["content"] for entry in listed_b] == ["B 的偏好：多用示例"]

        recall_b = client.get(f"{PREFIX}/memory", headers=_auth(USER_B)).json()["data"]
        assert "A 的偏好：先给结论" not in recall_b["content"]
        assert "B 的偏好：多用示例" in recall_b["content"]

        # 跨用户修改 / 删除：按不存在处理（404），不泄露条是否存在。
        a_id = created_a.json()["data"]["id"]
        update = client.put(
            f"{PREFIX}/memory/entries/{a_id}",
            json={"content": "被 B 改了"},
            headers=_auth(USER_B),
        )
        assert update.status_code == 404, update.text
        delete = client.delete(f"{PREFIX}/memory/entries/{a_id}", headers=_auth(USER_B))
        assert delete.status_code == 404, delete.text

        # A 自己仍看得到、改得动。
        listed_a = client.get(
            f"{PREFIX}/memory/entries", params={"layer": "user"}, headers=_auth(OWNER)
        ).json()["data"]
        assert [entry["content"] for entry in listed_a] == ["A 的偏好：先给结论"]


def test_environment_memories_are_global_for_every_user(
    settings: Settings, db_schema: None
) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": OWNER})
    with _client(cfg) as client:
        for user in (OWNER, USER_B):
            rows = client.get(
                f"{PREFIX}/memory/entries",
                params={"layer": "environment"},
                headers=_auth(user),
            ).json()["data"]
            assert "runtime.secrets" in {entry["key"] for entry in rows}

        # 环境层不需要登录态也能读；User 层不会因此泄露。
        anonymous = client.get(f"{PREFIX}/memory", headers=_auth(USER_B)).json()["data"]
        assert [layer["layer"] for layer in anonymous["layers"]][0] == "environment"


def test_legacy_null_user_memory_belongs_to_owner(
    settings: Settings, db_schema: None, container: Container
) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": OWNER})
    legacy = _run(
        container.memory_repo.insert(
            layer=MemoryLayer.USER, content="Phase 2 之前的老记忆", cloud_user_id=None
        )
    )
    with _client(cfg) as client:
        owner_rows = client.get(
            f"{PREFIX}/memory/entries", params={"layer": "user"}, headers=_auth(OWNER)
        ).json()["data"]
        assert [entry["id"] for entry in owner_rows] == [str(legacy.id)]

        other_rows = client.get(
            f"{PREFIX}/memory/entries", params={"layer": "user"}, headers=_auth(USER_B)
        ).json()["data"]
        assert other_rows == []
        denied = client.delete(f"{PREFIX}/memory/entries/{legacy.id}", headers=_auth(USER_B))
        assert denied.status_code == 404


def test_user_memory_without_resolvable_person_is_rejected(
    settings: Settings, db_schema: None
) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": None})
    with _client(cfg) as client:
        response = client.post(f"{PREFIX}/memory/entries", json={"content": "匿名偏好"})
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "cloud_login_required"

        # 读取仍能拿到 Environment，但不会出现任何人的 User 记忆。
        recall = client.get(f"{PREFIX}/memory").json()["data"]
        assert [layer["layer"] for layer in recall["layers"]] == ["environment"]


# --- 2. API Key 按人隔离 ---


def test_api_keys_are_per_user_and_names_can_repeat(settings: Settings, db_schema: None) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": OWNER})
    body = {
        "name": "DeepSeek 官方",
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "api_key": "sk-test-user-a",
        "models": ["deepseek-chat"],
    }
    with _client(cfg) as client:
        key_a = client.post(f"{PREFIX}/keys", json=body, headers=_auth(OWNER))
        assert key_a.status_code == 200, key_a.text
        body_b = {**body, "api_key": "sk-test-user-b"}
        key_b = client.post(f"{PREFIX}/keys", json=body_b, headers=_auth(USER_B))
        assert key_b.status_code == 200, key_b.text

        listed_b = client.get(f"{PREFIX}/keys", headers=_auth(USER_B)).json()["data"]
        assert [row["id"] for row in listed_b] == [key_b.json()["data"]["id"]]

        a_id = key_a.json()["data"]["id"]
        for response in (
            client.patch(f"{PREFIX}/keys/{a_id}", json={"enabled": False}, headers=_auth(USER_B)),
            client.post(f"{PREFIX}/keys/{a_id}/probe", headers=_auth(USER_B)),
            client.delete(f"{PREFIX}/keys/{a_id}", headers=_auth(USER_B)),
        ):
            assert response.status_code == 404, response.text


def test_legacy_null_key_belongs_to_owner_only(
    settings: Settings, db_schema: None, container: Container
) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": OWNER})
    legacy_id = _insert_legacy_key(cfg, "老密钥")
    with _client(cfg) as client:
        owner_ids = {
            row["id"] for row in client.get(f"{PREFIX}/keys", headers=_auth(OWNER)).json()["data"]
        }
        assert legacy_id in owner_ids

        assert client.get(f"{PREFIX}/keys", headers=_auth(USER_B)).json()["data"] == []
        denied = client.delete(f"{PREFIX}/keys/{legacy_id}", headers=_auth(USER_B))
        assert denied.status_code == 404

        # 运行期解析同样看不到别人的 key。
        with pytest.raises(Exception) as excinfo:
            _run(
                container.key_vault.resolve_selection(
                    runtime="claude-code",
                    model="",
                    key_id=legacy_id,
                    cloud_user_id=USER_B,
                )
            )
        assert excinfo.value.__class__.__name__ == "NotFoundError"


def test_key_without_resolvable_person_cannot_be_created(
    settings: Settings, db_schema: None
) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": None})
    with _client(cfg) as client:
        response = client.post(
            f"{PREFIX}/keys",
            json={
                "name": "匿名 key",
                "provider": "deepseek",
                "api_key": "sk-anon",
                "models": ["deepseek-chat"],
            },
        )
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "cloud_login_required"
        assert client.get(f"{PREFIX}/keys").json()["data"] == []


# --- 3. GitHub / account_key 按登录人 ---


def test_account_key_no_longer_defaults(settings: Settings) -> None:
    assert normalize_account_key(None) == ""
    assert normalize_account_key("   ") == ""
    assert normalize_account_key("user-1") == "user-1"


def test_github_connections_are_isolated_and_legacy_default_is_owner_only(
    settings: Settings, db_schema: None, container: Container
) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": OWNER})
    _insert_github(container, OWNER, "owner-login")
    _insert_github(container, USER_B, "b-login")

    with _client(cfg) as client:
        assert _github_state(client, OWNER)["account"] == "owner-login"
        assert _github_state(client, USER_B)["account"] == "b-login"

        # B 断开自己的连接，不影响 A。
        removed_b = client.delete(f"{PREFIX}/connectors/github", headers=_auth(USER_B))
        assert removed_b.status_code == 200, removed_b.text
        assert _github_state(client, OWNER)["connected"] is True
        assert _github_state(client, USER_B)["connected"] is False

    # 历史 default 行只对主用户可见。
    _run(_delete_github_rows(container))
    _insert_github(container, DEFAULT_ACCOUNT, "legacy-login")
    with _client(cfg) as client:
        assert _github_state(client, OWNER)["account"] == "legacy-login"
        assert _github_state(client, USER_B)["connected"] is False


async def _delete_github_rows(container: Container) -> None:
    async with container.session_factory() as session:
        for row in list(await session.scalars(select(GithubConnection))):
            await session.delete(row)
        await session.commit()


def test_github_login_without_account_header_stores_real_user_key(
    settings: Settings, db_schema: None
) -> None:
    """不再因为缺 X-Flux-Account 而写 account_key='default'。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/device/code":
            return httpx.Response(
                200,
                json={
                    "device_code": "device-code-1",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://github.com/login/device",
                    "expires_in": 900,
                    "interval": 5,
                },
            )
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(
                200,
                json={
                    "access_token": "gho_test_token",
                    "scope": "repo",
                    "token_type": "bearer",
                },
            )
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "octocat", "name": "Octo"})
        return httpx.Response(404)

    cfg = settings.model_copy(update={"owner_cloud_user_id": OWNER})
    app = create_app(cfg)
    app.state.container.github._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        started = client.post(f"{PREFIX}/connectors/github/login/start")
        assert started.status_code == 200, started.text
        flow_id = started.json()["data"]["flow_id"]
        polled = client.get(f"{PREFIX}/connectors/github/login/{flow_id}")
        assert polled.status_code == 200, polled.text
        assert polled.json()["data"]["status"] == "success"

    rows = _github_rows(cfg)
    assert rows == {OWNER: "octocat"}
    assert DEFAULT_ACCOUNT not in rows


def test_github_login_without_resolvable_person_is_rejected(
    settings: Settings, db_schema: None
) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": None})
    with _client(cfg) as client:
        response = client.post(f"{PREFIX}/connectors/github/login/start")
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "cloud_login_required"


# --- 4. 用量按人隔离，指纹口径不变 ---


def _seed_usage(container: Container, *, user: str | None, cost: float, tokens: int) -> None:
    _run(
        container.billing._repo.add_record(  # noqa: SLF001 - 直接造账本
            provider="opencode-go",
            model="opencode-go/gemini-4-argon",
            plan_key="opencode-go",
            account_key="fingerprint",
            cloud_user_id=user,
            input_tokens=tokens,
            cost=cost,
        )
    )


def test_usage_records_are_filtered_by_cloud_user(settings: Settings, db_schema: None) -> None:
    cfg = settings.model_copy(update={"owner_cloud_user_id": OWNER})
    app = create_app(cfg)
    container: Container = app.state.container
    # 关掉官方采集，只验证本地账本的用户过滤。
    container.billing._sources = {}  # noqa: SLF001
    _seed_usage(container, user=OWNER, cost=1.0, tokens=100)
    _seed_usage(container, user=USER_B, cost=9.0, tokens=900)
    _seed_usage(container, user=None, cost=3.0, tokens=300)  # 历史 NULL → 主用户

    with TestClient(app) as client:
        owner_data = client.get(
            f"{PREFIX}/billing/usage", params={"plan": "opencode-go"}, headers=_auth(OWNER)
        ).json()["data"]
        other_data = client.get(
            f"{PREFIX}/billing/usage", params={"plan": "opencode-go"}, headers=_auth(USER_B)
        ).json()["data"]

    def _cost(view: dict[str, Any]) -> float:
        monthly = next(w for w in view["windows"] if w["key"] == "monthly")
        return monthly["local"]["cost"]

    # 主用户看到自己 + 历史 NULL；看不到用户 B 的 9.0。
    assert _cost(owner_data) == pytest.approx(4.0)
    assert _cost(other_data) == pytest.approx(9.0)


def test_record_step_stamps_cloud_user_id_and_keeps_fingerprint(
    settings: Settings, db_schema: None, container: Container
) -> None:
    _run(
        container.billing.record_step(
            run_id="run-phase2",
            model="opencode-go/gemini-4-argon",
            input_tokens=10,
            cost=0.25,
        )
    )

    async def _read() -> tuple[str | None, str]:
        engine = create_engine(settings.database_url)
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                row = (
                    await session.execute(
                        select(UsageRecord.cloud_user_id, UsageRecord.account_key)
                    )
                ).first()
                assert row is not None
                return row[0], row[1]
        finally:
            await engine.dispose()

    user_id, account_key = _run(_read())
    assert user_id == OWNER
    # account_key 仍是凭据指纹（空 = 本机没识别到账号），没有被用户 id 覆盖。
    assert account_key == ""


def test_repository_user_scope_includes_legacy_only_for_owner(
    settings: Settings, db_schema: None
) -> None:
    repo = BillingRepository(_session_factory(settings))
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=1)

    async def seed() -> None:
        await repo.add_record(
            provider="p",
            model="m",
            plan_key="opencode-go",
            account_key="fp",
            cloud_user_id="1",
            input_tokens=1,
            cost=1.0,
        )
        await repo.add_record(
            provider="p",
            model="m",
            plan_key="opencode-go",
            account_key="fp",
            cloud_user_id="2",
            input_tokens=1,
            cost=2.0,
        )
        await repo.add_record(
            provider="p",
            model="m",
            plan_key="opencode-go",
            account_key="fp",
            cloud_user_id=None,
            input_tokens=1,
            cost=4.0,
        )

    _run(seed())

    owner = _run(
        repo.totals(
            plan_key="opencode-go",
            since=since,
            cloud_user_id="1",
            owner_cloud_user_id="1",
        )
    )
    other = _run(
        repo.totals(
            plan_key="opencode-go",
            since=since,
            cloud_user_id="2",
            owner_cloud_user_id="1",
        )
    )
    assert owner["cost"] == pytest.approx(5.0)
    assert other["cost"] == pytest.approx(2.0)


# --- 5. memory_sync 只同步主用户的 User 层 ---


class _FakeMemorySync(CloudMemorySync):
    def credentials(self):  # type: ignore[override]
        from flux.cloud.memory_sync import DeviceCredentials

        return DeviceCredentials("dev_test", "token")


def test_memory_sync_only_pushes_and_pulls_owner_entries(
    settings: Settings, db_schema: None, container: Container, tmp_path
) -> None:
    sync_settings = settings.model_copy(
        update={
            "memory_cloud_sync": True,
            "cloud_base_url": "https://cloud.invalid",
            "device_credentials_path": tmp_path / "creds",
            "owner_cloud_user_id": OWNER,
        }
    )
    sync = _FakeMemorySync(sync_settings, container.memory)
    pushed: list[str] = []

    async def fake_request(method: str, path: str, creds: Any, *, body: dict | None = None) -> Any:
        if method == "POST":
            pushed.extend(item["content"] for item in body["items"])
            return {"items": [{"id": 100 + index} for index, _ in enumerate(body["items"])]}
        if method == "GET":
            return [{"id": 100, "content": "主人的记忆（云端）", "source": "auto"}]
        return {"ok": True}

    sync._request = fake_request  # type: ignore[method-assign]
    container.memory.attach_cloud(sync)

    # 主用户写入会立即上云；另一个用户的写入留在本机，绝不推到主人的云账号。
    _run(container.memory.write_user(content="主人的记忆", cloud_user_id=OWNER))
    _run(container.memory.write_user(content="B 的记忆", cloud_user_id=USER_B))
    assert pushed == ["主人的记忆"]
    assert _run(sync.push_pending()) == 0

    applied = _run(sync.pull())
    assert applied == {"inserted": 0, "updated": 1, "removed": 0}

    # B 的记忆没有被推上云，也没有被云端快照覆盖 / 删除。
    b_entries = _run(container.memory.list_entries(layer=MemoryLayer.USER, cloud_user_id=USER_B))
    assert [entry.content for entry in b_entries] == ["B 的记忆"]
    owner_entries = _run(container.memory.list_entries(layer=MemoryLayer.USER, cloud_user_id=OWNER))
    assert {entry.content for entry in owner_entries} == {"主人的记忆（云端）"}
