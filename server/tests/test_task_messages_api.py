"""任务列表与任务对话 API 测试（Solo 任务执行中心增量）。

口径：
- 对话消息落 task_messages 表，刷新/重启后仍在；
- 助手回复来自平台真实模型调用（测试环境用离线回显 provider，不触网、不花钱）；
- 消息只增不改：任务终态后拒绝再追加，避免"任务已取消还在对话"这种自相矛盾的数据。
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from flux.config import Settings
from flux.core.task_engine.assistant import (
    CONFIRMATION_DIMENSIONS,
    CONFIRMATION_UNKNOWN,
    parse_turn,
)
from flux.main import create_app

PREFIX = "/api/v1"

_MESSAGE_KEYS = {"id", "task_id", "seq", "role", "kind", "content", "payload", "created_at"}


def _new_client(settings: Settings) -> TestClient:
    """全新 app 实例（模拟进程重启：容器、调度队列、数据库会话全部重建）。"""
    return TestClient(create_app(settings))


# --- 任务列表 ---


def test_list_tasks_returns_created_tasks(client: TestClient) -> None:
    """新建的任务出现在列表里，响应体与单条查询同构。"""
    first = client.post(f"{PREFIX}/tasks", json={"description": "甲任务"}).json()["data"]
    second = client.post(f"{PREFIX}/tasks", json={"description": "乙任务"}).json()["data"]

    listed = client.get(f"{PREFIX}/tasks")
    assert listed.status_code == 200
    body = listed.json()
    assert body["metadata"]["count"] == 2
    ids = [task["id"] for task in body["data"]]
    assert set(ids) == {first["id"], second["id"]}
    assert set(body["data"][0]) == set(first)


def test_list_tasks_filters_by_status_and_project(client: TestClient) -> None:
    """status / project_id 过滤生效：取消掉的任务只在对应过滤条件下出现。"""
    cancelled = client.post(f"{PREFIX}/tasks", json={"description": "待取消"}).json()["data"]
    assert client.post(f"{PREFIX}/tasks/{cancelled['id']}/cancel").status_code == 200
    pending = client.post(f"{PREFIX}/tasks", json={"description": "仍待处理"}).json()["data"]

    only_cancelled = client.get(f"{PREFIX}/tasks", params={"status": "cancelled"}).json()["data"]
    assert [task["id"] for task in only_cancelled] == [cancelled["id"]]

    only_pending = client.get(f"{PREFIX}/tasks", params={"status": "pending"}).json()["data"]
    assert [task["id"] for task in only_pending] == [pending["id"]]

    # 合法 UUID 但不是任何任务的项目 → 空列表（过滤条件本身没错，只是没数据）
    empty = client.get(f"{PREFIX}/tasks", params={"project_id": str(uuid.uuid4())}).json()
    assert empty["data"] == []
    assert empty["metadata"]["count"] == 0


def test_list_tasks_rejects_bad_filters(client: TestClient) -> None:
    """非法 status / project_id / limit 一律 400（不静默当成"没有数据"）。"""
    assert client.get(f"{PREFIX}/tasks", params={"status": "已完成"}).status_code == 400
    assert client.get(f"{PREFIX}/tasks", params={"project_id": "不是-uuid"}).status_code == 400
    assert client.get(f"{PREFIX}/tasks", params={"limit": 0}).status_code == 400
    assert client.get(f"{PREFIX}/tasks", params={"limit": 500}).status_code == 400


# --- 任务对话 ---


def test_post_message_creates_user_and_assistant_messages(client: TestClient) -> None:
    """发消息返回两条真实消息：用户那条原样落库，助手那条来自模型调用。"""
    task = client.post(f"{PREFIX}/tasks", json={"description": "实现邮箱修改"}).json()["data"]

    response = client.post(
        f"{PREFIX}/tasks/{task['id']}/messages", json={"content": "邮箱需要支持更换通知"}
    )
    assert response.status_code == 200
    body = response.json()
    user_message, reply = body["data"]["messages"]

    assert set(user_message) == _MESSAGE_KEYS
    assert user_message["role"] == "user"
    assert user_message["content"] == "邮箱需要支持更换通知"
    assert user_message["seq"] == 1
    assert reply["role"] == "assistant"
    assert reply["seq"] == 2
    # 测试环境走离线回显 provider：回复里必须带上用户原话（证明真的调到了模型层）
    assert "邮箱需要支持更换通知" in reply["content"]
    assert reply["payload"]["model"]["provider"] == "local"
    assert reply["payload"]["model"]["model"] == "local-echo"

    # 首次真实处理把任务从 pending 推进到 running
    assert body["data"]["task"]["status"] == "running"
    assert client.get(f"{PREFIX}/tasks/{task['id']}").json()["data"]["status"] == "running"


def test_messages_survive_app_restart(settings: Settings, db_schema: None) -> None:
    """聊天记录落库：换一个全新 app 实例仍能读回同一批消息。"""
    with _new_client(settings) as first:
        task_id = first.post(f"{PREFIX}/tasks", json={"description": "重启后仍在"}).json()["data"][
            "id"
        ]
        first.post(f"{PREFIX}/tasks/{task_id}/messages", json={"content": "第一句"})

    with _new_client(settings) as second:
        listed = second.get(f"{PREFIX}/tasks/{task_id}/messages")

    assert listed.status_code == 200
    messages = listed.json()["data"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "第一句"
    assert listed.json()["metadata"]["has_more"] is False


def test_second_message_includes_history(client: TestClient) -> None:
    """第二轮请求把前文作为历史带给模型（离线回显会报告收到的消息条数）。"""
    task = client.post(f"{PREFIX}/tasks", json={"description": "多轮对话"}).json()["data"]
    client.post(f"{PREFIX}/tasks/{task['id']}/messages", json={"content": "第一句"})

    second = client.post(f"{PREFIX}/tasks/{task['id']}/messages", json={"content": "第二句"})
    reply = second.json()["data"]["messages"][1]
    # system + 历史 2 条（第一轮的问与答）+ 本轮用户消息 = 4
    assert "收到 4 条消息" in reply["content"]

    listed = client.get(f"{PREFIX}/tasks/{task['id']}/messages").json()
    assert [m["seq"] for m in listed["data"]] == [1, 2, 3, 4]
    assert listed["metadata"]["has_more"] is False


def test_message_paging_by_before_cursor(client: TestClient) -> None:
    """向上懒加载：has_more + before 游标能翻出全部消息且不重不漏。"""
    task = client.post(f"{PREFIX}/tasks", json={"description": "分页"}).json()["data"]
    for index in range(3):
        client.post(f"{PREFIX}/tasks/{task['id']}/messages", json={"content": f"第 {index + 1} 轮"})

    latest = client.get(f"{PREFIX}/tasks/{task['id']}/messages", params={"limit": 4}).json()
    assert [m["seq"] for m in latest["data"]] == [3, 4, 5, 6]
    assert latest["metadata"]["has_more"] is True

    earlier = client.get(
        f"{PREFIX}/tasks/{task['id']}/messages",
        params={"limit": 4, "before": latest["data"][0]["seq"]},
    ).json()
    assert [m["seq"] for m in earlier["data"]] == [1, 2]
    assert earlier["metadata"]["has_more"] is False

    # 更早一段再往前翻是空段：游标到底，不再有更早的消息
    beyond = client.get(
        f"{PREFIX}/tasks/{task['id']}/messages",
        params={"limit": 4, "before": earlier["data"][0]["seq"]},
    ).json()
    assert beyond["data"] == []
    assert beyond["metadata"]["has_more"] is False


def test_message_to_cancelled_task_conflicts(client: TestClient) -> None:
    """终态任务不接受新消息：409，且不会写进任何一条消息。"""
    task = client.post(f"{PREFIX}/tasks", json={"description": "先取消"}).json()["data"]
    assert client.post(f"{PREFIX}/tasks/{task['id']}/cancel").status_code == 200

    response = client.post(f"{PREFIX}/tasks/{task['id']}/messages", json={"content": "还在吗"})
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert client.get(f"{PREFIX}/tasks/{task['id']}/messages").json()["data"] == []


def test_unknown_task_messages_not_found(client: TestClient) -> None:
    """未知 / 非法任务标识的消息读写返回 404；空内容按 422 拒绝。"""
    unknown = uuid.uuid4()
    assert client.get(f"{PREFIX}/tasks/{unknown}/messages").status_code == 404
    assert (
        client.post(f"{PREFIX}/tasks/{unknown}/messages", json={"content": "x"}).status_code == 404
    )
    assert client.get(f"{PREFIX}/tasks/不是-uuid/messages").status_code == 404
    assert (
        client.post(f"{PREFIX}/tasks/不是-uuid/messages", json={"content": "x"}).status_code == 404
    )

    task = client.post(f"{PREFIX}/tasks", json={"description": "空消息"}).json()["data"]
    assert (
        client.post(f"{PREFIX}/tasks/{task['id']}/messages", json={"content": ""}).status_code
        == 422
    )


# --- 助手输出解析（真实模型未必每次都按协议输出，解析必须稳）---


def test_parse_turn_reads_structured_output() -> None:
    """标准协议输出：reply + questions + confirmation（六维度）+ steps 全部取出。"""
    raw = (
        '{"reply": "我已理解需求。", "questions": ["登录方式用邮箱还是手机号？"],'
        ' "confirmation": [{"label": "目标", "value": "支持修改邮箱"},'
        ' {"label": "功能范围", "value": "只做后端接口"},'
        ' {"label": "技术方案", "value": "FastAPI + SQLAlchemy"},'
        ' {"label": "修改范围", "value": "users 模块"},'
        ' {"label": "风险", "value": "要兼容旧数据"},'
        ' {"label": "需人工审核的环节", "value": "数据库迁移"}],'
        ' "steps": ["梳理现有登录流程", "实现邮箱修改接口", "补测试"]}'
    )
    reply, questions, confirmation, steps = parse_turn(raw)
    assert reply == "我已理解需求。"
    assert questions == ["登录方式用邮箱还是手机号？"]
    assert [item["label"] for item in confirmation] == list(CONFIRMATION_DIMENSIONS)
    assert confirmation[0] == {"label": "目标", "value": "支持修改邮箱"}
    assert steps == ["梳理现有登录流程", "实现邮箱修改接口", "补测试"]


def test_parse_turn_tolerates_code_fence_and_noise() -> None:
    """```json 围栏与前后缀说明文字都能容忍；模型没说的维度补"待确认"。"""
    raw = (
        "好的，这是结果：\n```json\n"
        '{"reply": "好", "confirmation": [{"label": "目标", "value": "加导出"}], "steps": ["一步"]}'
        "\n```\n完毕"
    )
    reply, questions, confirmation, steps = parse_turn(raw)
    assert (reply, questions, steps) == ("好", [], ["一步"])
    # 只给了 1 个维度 → 其余 5 个补"待确认"，顺序固定按六维度声明顺序
    assert [item["label"] for item in confirmation] == list(CONFIRMATION_DIMENSIONS)
    assert confirmation[0] == {"label": "目标", "value": "加导出"}
    assert confirmation[1]["value"] == CONFIRMATION_UNKNOWN


def test_parse_turn_falls_back_to_plain_text() -> None:
    """纯文本 / 残缺 JSON / 字段类型不符时退化为整段文本，不臆造结构化内容。"""
    assert parse_turn("我建议先做接口。") == ("我建议先做接口。", [], [], [])
    assert parse_turn('{"reply": "半截"') == ('{"reply": "半截"', [], [], [])
    # reply 缺失或不是字符串 → 整段原样返回
    assert parse_turn('{"confirmation": [{"label": "目标", "value": "导出"}]}') == (
        '{"confirmation": [{"label": "目标", "value": "导出"}]}',
        [],
        [],
        [],
    )
    # 数组里混入非法元素时只丢非法项；自由发挥的维度不进确认卡
    raw = (
        '{"reply": "r", '
        '"questions": ["还改不改前端？", "", 3], '
        '"confirmation": [{"label": "技术栈", "value": "React"},'
        ' {"label": "风险", "value": "低"}], '
        '"steps": ["a", 3]}'
    )
    reply, questions, confirmation, steps = parse_turn(raw)
    assert (reply, questions, steps) == ("r", ["还改不改前端？"], ["a"])
    assert confirmation == [
        {"label": "目标", "value": CONFIRMATION_UNKNOWN},
        {"label": "功能范围", "value": CONFIRMATION_UNKNOWN},
        {"label": "技术方案", "value": CONFIRMATION_UNKNOWN},
        {"label": "修改范围", "value": CONFIRMATION_UNKNOWN},
        {"label": "风险", "value": "低"},
        {"label": "需人工审核的环节", "value": CONFIRMATION_UNKNOWN},
    ]
