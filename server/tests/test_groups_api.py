"""内部多 bot 群聊 API 测试（2026-11-04）。

口径：
- 群成员是常驻 Bot（bots），名称 + 「形状 × 颜色」头像都落在 bots 表；
- 群内消息按 seq 单调递增，前端靠 after_seq 增量拉取；
- 用户发言会开一个后台 epoch；本文件把编排器换成空实现，只验证接口契约
  （真实轮询/沉默/去重逻辑由编排器自身的单元行为保证，不在 REST 层重复）。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from flux.core.group.orchestrator import GroupOrchestrator

PREFIX = "/api/v1"

_GROUP_KEYS = {
    "id",
    "name",
    "project_id",
    "description",
    "max_rounds",
    "status",
    "last_message_at",
    "created_at",
    "member_count",
    "members",
}

_MESSAGE_KEYS = {
    "id",
    "group_id",
    "seq",
    "bot_id",
    "author_name",
    "role",
    "kind",
    "content",
    "payload",
    "epoch_id",
    "created_at",
}

_MEMBER_KEYS = {
    "bot_id",
    "name",
    "description",
    "avatar_shape",
    "avatar_color",
    "status",
    "role",
    "model",
    "api_key_id",
    "permission_mode",
}


@pytest.fixture(autouse=True)
def quiet_epochs(monkeypatch: pytest.MonkeyPatch) -> None:
    """把后台 epoch 换成空实现：测试不跑真实模型，也不并发写同一个 SQLite 库。"""

    async def _noop(self: GroupOrchestrator, *args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(GroupOrchestrator, "run_epoch", _noop)


def _create_bot(
    client: TestClient,
    name: str,
    *,
    shape: str | None = "hexagon",
    color: str | None = "teal",
) -> dict:
    payload: dict[str, object] = {
        "name": name,
        "description": f"{name} 的对话",
    }
    if shape is not None:
        payload["avatar_shape"] = shape
    if color is not None:
        payload["avatar_color"] = color
    response = client.post(f"{PREFIX}/bots", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _create_group(
    client: TestClient, name: str, bot_ids: list[str], *, admin_bot_id: str | None = None
) -> dict:
    body: dict[str, object] = {"name": name, "bot_ids": bot_ids, "max_rounds": 2}
    if admin_bot_id is not None:
        body["admin_bot_id"] = admin_bot_id
    response = client.post(f"{PREFIX}/groups", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


# --- bot 档案 ---


def test_create_bot_persists_name_and_avatar(client: TestClient) -> None:
    """建 Bot 时名称与头像落库，默认 active（常驻、不等首条消息）。"""
    bot = _create_bot(client, "数据统计")

    assert bot["name"] == "数据统计"
    assert bot["avatar_shape"] == "hexagon"
    assert bot["avatar_color"] == "teal"
    assert bot["status"] == "active"

    fetched = client.get(f"{PREFIX}/bots/{bot['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["data"] == bot


def test_create_bot_falls_back_to_default_avatar(client: TestClient) -> None:
    """不选头像时用默认 circle / violet，而不是空串。"""
    bot = _create_bot(client, "默认头像", shape=None, color=None)

    assert bot["avatar_shape"] == "circle"
    assert bot["avatar_color"] == "violet"


def test_create_bot_rejects_unknown_avatar(client: TestClient) -> None:
    """头像形状/颜色非法时直接 422，不静默回退。"""
    bad_shape = client.post(
        f"{PREFIX}/bots",
        json={"name": "非法形状", "avatar_shape": "blob2"},
    )
    assert bad_shape.status_code == 422

    bad_color = client.post(
        f"{PREFIX}/bots",
        json={"name": "非法颜色", "avatar_color": "pink"},
    )
    assert bad_color.status_code == 422


# --- 群 CRUD 与成员 ---


def test_group_crud_keeps_member_order(client: TestClient) -> None:
    """建群按传参顺序入群；后续加/删成员立即反映在成员列表上。"""
    first = _create_bot(client, "统计")
    second = _create_bot(client, "发布")
    third = _create_bot(client, "校对")

    group = _create_group(client, "每日运营", [first["id"], second["id"]])
    assert set(group) == _GROUP_KEYS
    assert group["member_count"] == 2
    assert set(group["members"][0]) == _MEMBER_KEYS
    assert group["members"][0]["role"] == "member"
    assert group["members"][0]["permission_mode"] == "unattended"
    assert [m["bot_id"] for m in group["members"]] == [first["id"], second["id"]]
    assert group["members"][0]["name"] == "统计"
    assert group["members"][0]["avatar_shape"] == "hexagon"

    added = client.post(f"{PREFIX}/groups/{group['id']}/members", json={"bot_id": third["id"]})
    assert added.status_code == 200
    assert [m["bot_id"] for m in added.json()["data"]] == [
        first["id"],
        second["id"],
        third["id"],
    ]

    removed = client.delete(f"{PREFIX}/groups/{group['id']}/members/{second['id']}")
    assert removed.status_code == 200
    assert removed.json()["data"]["removed"] is True
    assert [m["bot_id"] for m in removed.json()["data"]["members"]] == [first["id"], third["id"]]

    listed = client.get(f"{PREFIX}/groups").json()["data"]
    assert [(g["id"], g["member_count"]) for g in listed] == [(group["id"], 2)]

    patched = client.patch(
        f"{PREFIX}/groups/{group['id']}",
        json={"name": "每日运营 · 改", "max_rounds": 3},
    )
    assert patched.status_code == 200
    assert patched.json()["data"]["name"] == "每日运营 · 改"
    assert patched.json()["data"]["max_rounds"] == 3

    deleted = client.delete(f"{PREFIX}/groups/{group['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["data"] == {"deleted": True}
    assert client.get(f"{PREFIX}/groups/{group['id']}").status_code == 404


def test_group_rejects_unknown_member_and_group(client: TestClient) -> None:
    """成员必须是已存在的 Bot；未知群一律 404。"""
    missing = client.post(
        f"{PREFIX}/groups",
        json={"name": "空群", "bot_ids": [str(uuid.uuid4())]},
    )
    assert missing.status_code == 404

    ghost = str(uuid.uuid4())
    assert client.get(f"{PREFIX}/groups/{ghost}").status_code == 404
    assert client.get(f"{PREFIX}/groups/{ghost}/messages").status_code == 404
    assert client.post(f"{PREFIX}/groups/{ghost}/interrupt").status_code == 404


# --- 群消息 ---


def test_group_messages_paginate_by_seq(client: TestClient) -> None:
    """after_seq 游标按 seq 升序翻页：0 取最早 N 条，has_more 指出后面还有。"""
    bot = _create_bot(client, "统计")
    group = _create_group(client, "分页群", [bot["id"]])

    seqs = []
    for index in range(3):
        posted = client.post(
            f"{PREFIX}/groups/{group['id']}/messages", json={"content": f"第 {index} 条"}
        )
        assert posted.status_code == 200
        message = posted.json()["data"]["message"]
        assert set(message) == _MESSAGE_KEYS
        assert message["role"] == "user"
        assert message["author_name"] == "我"
        seqs.append(message["seq"])
    assert seqs == [1, 2, 3]

    first_page = client.get(
        f"{PREFIX}/groups/{group['id']}/messages", params={"after_seq": 0, "limit": 2}
    ).json()["data"]
    assert [m["seq"] for m in first_page["messages"]] == [1, 2]
    assert first_page["has_more"] is True

    last_page = client.get(
        f"{PREFIX}/groups/{group['id']}/messages", params={"after_seq": 2, "limit": 2}
    ).json()["data"]
    assert [m["seq"] for m in last_page["messages"]] == [3]
    assert last_page["has_more"] is False

    empty = client.get(f"{PREFIX}/groups/{group['id']}/messages", params={"after_seq": 3}).json()[
        "data"
    ]
    assert empty["messages"] == []
    assert empty["has_more"] is False


def test_user_message_opens_epoch_and_detects_mentions(client: TestClient) -> None:
    """用户发言开启 epoch 并返回被 @ 的 bot；再次打断后 epoch 不再是 running。"""
    stat = _create_bot(client, "统计")
    publish = _create_bot(client, "小红书发布")
    group = _create_group(client, "运营群", [stat["id"], publish["id"]])

    posted = client.post(
        f"{PREFIX}/groups/{group['id']}/messages",
        json={"content": "@小红书发布 今天的流量怎么样？"},
    )
    assert posted.status_code == 200
    data = posted.json()["data"]
    assert data["mentioned"] == [publish["id"]]
    assert uuid.UUID(data["epoch_id"])

    interrupted = client.post(f"{PREFIX}/groups/{group['id']}/interrupt").json()["data"]
    assert interrupted == {"interrupted": True, "epoch_id": data["epoch_id"]}

    again = client.post(f"{PREFIX}/groups/{group['id']}/interrupt").json()["data"]
    assert again == {"interrupted": False, "epoch_id": None}


def test_group_message_requires_content(client: TestClient) -> None:
    """空消息在 API 边界就被拒绝，不落一条空记录。"""
    bot = _create_bot(client, "统计")
    group = _create_group(client, "空消息群", [bot["id"]])

    rejected = client.post(f"{PREFIX}/groups/{group['id']}/messages", json={"content": ""})
    assert rejected.status_code == 422


# --- 群主、成员预设与分层记忆 ---


def _create_key(client: TestClient) -> dict:
    response = client.post(
        f"{PREFIX}/keys",
        json={
            "name": "Anthropic 官方",
            "provider": "anthropic",
            "base_url": "https://api.anthropic.com",
            "api_key": "sk-ant-test-12345678",
            "models": ["claude-sonnet-5-5"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_create_group_with_admin_and_switch(client: TestClient) -> None:
    """建群可指定群主；改群主时旧的自动降级，一个群最多一个 admin。"""
    first = _create_bot(client, "统计")
    second = _create_bot(client, "发布")

    group = _create_group(client, "运营群", [first["id"], second["id"]], admin_bot_id=first["id"])
    roles = {m["bot_id"]: m["role"] for m in group["members"]}
    assert roles == {first["id"]: "admin", second["id"]: "member"}

    patched = client.patch(
        f"{PREFIX}/groups/{group['id']}/members/{second['id']}", json={"role": "admin"}
    )
    assert patched.status_code == 200, patched.text
    roles = {m["bot_id"]: m["role"] for m in patched.json()["data"]}
    assert roles == {first["id"]: "member", second["id"]: "admin"}

    # 再次设回第一人：第二人自动降级，不会出现两个群主
    again = client.patch(
        f"{PREFIX}/groups/{group['id']}/members/{first['id']}", json={"role": "admin"}
    )
    roles = {m["bot_id"]: m["role"] for m in again.json()["data"]}
    assert roles == {first["id"]: "admin", second["id"]: "member"}

    bad = client.post(
        f"{PREFIX}/groups",
        json={"name": "坏群主", "bot_ids": [first["id"]], "admin_bot_id": second["id"]},
    )
    assert bad.status_code == 400


def test_update_member_model_and_permission(client: TestClient) -> None:
    """成员的模型/密钥/权限预设由人通过成员 PATCH 调整。"""
    bot = _create_bot(client, "统计")
    group = _create_group(client, "运营群", [bot["id"]])
    key = _create_key(client)

    patched = client.patch(
        f"{PREFIX}/groups/{group['id']}/members/{bot['id']}",
        json={"model": "claude-sonnet-5-5", "key_id": key["id"]},
    )
    assert patched.status_code == 200, patched.text
    member = patched.json()["data"][0]
    assert member["model"] == "claude-sonnet-5-5"
    assert member["api_key_id"] == key["id"]

    mode = client.patch(
        f"{PREFIX}/groups/{group['id']}/members/{bot['id']}",
        json={"permission_mode": "plan"},
    )
    assert mode.status_code == 200, mode.text
    assert mode.json()["data"][0]["permission_mode"] == "plan"
    assert client.get(f"{PREFIX}/bots/{bot['id']}").json()["data"]["permission_mode"] == "plan"


def test_group_memory_is_empty_until_orchestrator_writes(client: TestClient) -> None:
    """记忆由编排器维护，API 只读；新群返回两个 None，未知群 404。"""
    bot = _create_bot(client, "统计")
    group = _create_group(client, "记忆群", [bot["id"]])

    memory = client.get(f"{PREFIX}/groups/{group['id']}/memory")
    assert memory.status_code == 200
    assert memory.json()["data"] == {"group": None, "team": None}

    assert client.get(f"{PREFIX}/groups/{uuid.uuid4()}/memory").status_code == 404
