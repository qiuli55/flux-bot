"""既有消息 → 统一组件树的映射稳定性，以及对话接口的兼容性。

口径：
- 老字段（kind / content / payload 原有键）一个不动，只额外附加
  `schema_version` / `component` / `fallback_text`；
- 映射必须是纯函数：同样的输入（含 message id）得到同样的树与 action_id；
- 映射结果必须能通过 schema 校验（否则读取时自动退回纯文本）；
- `/tasks/{id}/messages` 与 `/bots/{id}/conversation` 都能拿到组件。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi.testclient import TestClient

from flux.container import Container
from flux.core.components.mapping import component_for_message, component_payload
from flux.core.components.schema import validate_component
from flux.core.task_engine.repository import MessageDraft

PREFIX = "/api/v1"

_SECURE_CARD = {
    "title": "Grok API key",
    "description": "在控制台创建的 key",
    "placeholder": "粘贴密钥",
    "name": "Grok API key",
    "provider": "openai",
    "models": ["grok-4"],
    "base_url": "",
    "wire_api": "",
}

_DECISION = {
    "id": "dec-1",
    "question": "选哪个方案？",
    "context": "两个方案都能用",
    "options": [
        {"label": "方案 A", "description": "快", "impact": "小", "recommended": True},
        {"label": "方案 B", "description": "稳", "impact": "中", "recommended": False},
    ],
    "recommendation": "方案 A",
    "mode": "manual",
    "status": "pending",
    "chosen": None,
    "note": None,
    "raised_at": "2026-11-14T00:00:00+00:00",
    "resolved_at": None,
}


def _node(
    kind: str, payload: dict[str, Any], content: str = "说明", message_id: str = "m1"
) -> dict:
    node = component_for_message(kind=kind, content=content, payload=payload, message_id=message_id)
    assert node is not None, kind
    return validate_component(node)


def test_file_card_maps_to_image_and_file_nodes() -> None:
    node = _node(
        "file_card",
        {
            "delivery_id": "delivery_1",
            "note": "报告",
            "created_at": "2026-11-14T00:00:00+00:00",
            "files": [
                {
                    "index": 0,
                    "name": "chart.png",
                    "size": 10,
                    "size_label": "10 B",
                    "mime": "image/png",
                    "kind": "image",
                },
                {
                    "index": 1,
                    "name": "report.pdf",
                    "size": 20,
                    "size_label": "20 B",
                    "mime": "application/pdf",
                    "kind": "other",
                },
            ],
        },
    )
    assert node["type"] == "card"
    assert node["title"] == "报告"
    kinds = [child["type"] for child in node["children"]]
    assert kinds == ["text", "image", "file"]
    assert node["children"][1]["delivery_id"] == "delivery_1"
    assert node["children"][2]["name"] == "report.pdf"


def test_plan_card_maps_steps_files_and_markdown() -> None:
    node = _node(
        "plan_card",
        {
            "title": "登录改造",
            "steps": [
                {"title": "改接口", "detail": "支持刷新"},
                {"title": "补测试", "detail": None},
            ],
            "files": ["server/login.py"],
            "plan_path": "/opt/flux/plans/x.md",
            "markdown": "### 注意事项",
        },
        content="实施计划：登录改造（2 步）",
    )
    assert node["title"] == "登录改造"
    lists = [child for child in node["children"] if child["type"] == "list"]
    assert len(lists) == 2
    assert lists[0]["ordered"] is True
    assert "改接口" in lists[0]["items"][0]["text"]
    assert "server/login.py" in lists[1]["items"][0]["text"]
    assert "/opt/flux/plans" not in str(node)


def test_handoff_carries_open_desktop_client_action() -> None:
    node = _node(
        "handoff",
        {
            "from_agent_id": None,
            "from_agent": "旧 Agent",
            "to_agent_id": "agent-2",
            "to_agent": "新 Agent",
            "runtime": "claude",
            "run_id": "r1",
            "status": "completed",
        },
        content="已把本轮交给新 Agent。",
    )
    buttons = [child for child in node["children"] if child["type"] == "button"]
    assert buttons[0]["action"] == {"type": "client", "name": "open_desktop"}
    assert "新 Agent" in node["title"]


def test_confirmation_maps_items() -> None:
    node = _node(
        "confirmation",
        {
            "confirmation": {
                "items": [{"label": "目标", "value": "支持修改邮箱"}],
                "actor": "user",
                "updated_at": "2026-11-14T00:00:00+00:00",
            }
        },
        content="需求确认（最终版本）",
    )
    assert node["subtitle"] == "我修改后的版本"
    text = str(node)
    assert "目标：支持修改邮箱" in text


def test_subtask_and_run_finished_map_progress() -> None:
    subtask = _node(
        "subtask",
        {"subtask_id": "s1", "status": "running", "title": "调研", "summary": "进行中"},
        content="子任务已开始：调研",
    )
    progress = next(child for child in subtask["children"] if child["type"] == "progress")
    assert progress["status"] == "running"
    assert subtask["title"] == "调研"

    finished = _node(
        "run_finished",
        {"run_id": "r1", "status": "completed", "finish_reason": "stop", "error": None},
        content="任务已完成",
    )
    progress = next(child for child in finished["children"] if child["type"] == "progress")
    assert progress["status"] == "done"
    assert finished["title"] == "执行完成"


def test_secure_input_maps_to_secure_input_without_secret() -> None:
    node = _node(
        "secure_input",
        {"secure_input": dict(_SECURE_CARD)},
        content="key 只会显示一次",
        message_id="card-123",
    )
    input_node = next(child for child in node["children"] if child["type"] == "input")
    assert input_node["input_type"] == "secure"
    assert input_node["action"] == {
        "type": "server",
        "name": "secure_input.submit",
        "action_id": "secure_input:card-123",
    }
    rendered = str(node)
    assert "sk-" not in rendered
    assert "api_key" not in rendered.lower().replace("input", "")


def test_secure_input_result_does_not_repeat_key_identifiers() -> None:
    node = _node(
        "secure_input_result",
        {
            "message_id": "card-123",
            "key_id": "key-secret-id",
            "name": "Grok API key",
            "provider": "openai",
            "key_hint": "••••1234",
            "status": "stored",
        },
        content="已通过安全输入框保存密钥「Grok API key」，密钥不会出现在对话里。",
    )
    rendered = str(node)
    assert "Grok API key" in rendered
    assert "key-secret-id" not in rendered
    assert "••••1234" not in rendered


def test_pending_decision_maps_option_buttons_with_stable_action_ids() -> None:
    payload = {"decision": dict(_DECISION)}
    first = _node("decision", payload, content="需要你决定")
    second = _node("decision", payload, content="需要你决定")
    assert first == second
    buttons = [child for child in first["children"] if child["type"] == "button"]
    assert [button["label"] for button in buttons] == ["方案 A", "方案 B", "都不合适"]
    assert buttons[0]["action"] == {
        "type": "server",
        "name": "decision.choose",
        "action_id": "decision:dec-1:0",
        "decision_id": "dec-1",
        "option": "方案 A",
    }
    assert buttons[-1]["action"]["action_id"] == "decision:dec-1:reject"


def test_resolved_and_auto_decisions_have_no_buttons() -> None:
    auto = _node(
        "decision_auto",
        {"decision": {**_DECISION, "status": "auto_resolved", "chosen": "方案 A"}},
        content="AI 已按默认方案自行决定：方案 A",
    )
    assert not [child for child in auto["children"] if child["type"] == "button"]
    assert auto["title"] == "已自动决策"

    result = _node(
        "decision_result",
        {"decision": {**_DECISION, "status": "resolved", "chosen": "方案 A"}},
        content="你的选择：方案 A",
    )
    assert not [child for child in result["children"] if child["type"] == "button"]
    assert "方案 A" in str(result)


def test_component_payload_preserves_legacy_keys() -> None:
    payload = {"decision": dict(_DECISION)}
    enriched = component_payload("decision", "需要你决定", payload, "m1")
    assert enriched is not None
    assert enriched["decision"] == payload["decision"]
    assert enriched["schema_version"] == 1
    assert enriched["fallback_text"] == "需要你决定"
    assert enriched["component"]["type"] == "card"
    # 原 payload 未被就地修改
    assert set(payload) == {"decision"}


def test_unknown_kind_and_broken_payload_return_none() -> None:
    assert component_payload("text", "普通文本", {}, "m1") is None
    assert component_payload("attachments", "a.png", {"files": []}, "m1") is None
    assert component_payload("file_card", "文件", {"files": []}, "m1") is None


# --- 附件 ---


def test_inbound_attachments_map_to_upload_id_media_nodes() -> None:
    node = _node(
        "attachments",
        {
            "files": [
                {
                    "id": "a" * 32,
                    "name": "chart.png",
                    "size": 10,
                    "mime": "image/png",
                    # 历史 payload 里可能有内部路径：组件树绝不能带出去
                    "path": "/opt/flux/uploads/legacy/chart.png",
                },
                {"id": "b" * 32, "name": "note.pdf", "size": 20, "mime": "application/pdf"},
                {"id": "c" * 32, "name": "voice.amr", "size": 30, "mime": "audio/amr"},
            ]
        },
        content="chart.png、note.pdf、voice.amr",
    )
    assert node["type"] == "card"
    assert [child["type"] for child in node["children"]] == ["text", "image", "file", "audio"]
    image, document, audio = node["children"][1:]
    assert image["upload_id"] == "a" * 32
    assert image["alt"] == "chart.png"
    assert document["name"] == "note.pdf"
    assert document["upload_id"] == "b" * 32
    assert audio["upload_id"] == "c" * 32
    assert "/opt/flux/uploads" not in str(node)
    assert "path" not in str(node)


def test_multiple_inbound_images_share_one_card_for_stacked_deck() -> None:
    node = _node(
        "attachments",
        {
            "files": [
                {"id": "a" * 32, "name": "one.png", "size": 1, "mime": "image/png"},
                {"id": "b" * 32, "name": "two.jpg", "size": 2, "mime": "image/jpeg"},
            ]
        },
        content="one.png、two.jpg",
    )
    images = [child for child in node["children"] if child["type"] == "image"]
    assert len(images) == 2
    assert all(child["upload_id"] for child in images)


def test_outbound_text_with_attachments_keeps_body_and_adds_media() -> None:
    payload = {
        "attachments": [
            {
                "upload_id": "up_image_1",
                "name": "chart.png",
                "mime": "image/png",
                "size": 10,
                "kind": "image",
            },
            {
                "upload_id": "up_file_1",
                "name": "report.pdf",
                "mime": "application/pdf",
                "size": 20,
                "kind": "file",
            },
        ]
    }
    enriched = component_payload("text", "这是结果", payload, "m1")
    assert enriched is not None
    node = validate_component(enriched["component"])
    assert node["type"] == "card"
    assert node["children"][0]["type"] == "text"
    assert node["children"][0]["text"] == "这是结果"
    assert [child["type"] for child in node["children"]] == ["text", "image", "file"]
    assert node["children"][1]["upload_id"] == "up_image_1"
    assert node["children"][2]["name"] == "report.pdf"
    # 原 payload 的附件记录保持不变（老客户端仍可读）。
    assert enriched["attachments"] == payload["attachments"]
    assert enriched["fallback_text"] == "这是结果"


def test_outbound_secure_input_keeps_input_and_appends_attachment() -> None:
    node = _node(
        "secure_input",
        {
            "secure_input": dict(_SECURE_CARD),
            "attachments": [
                {
                    "upload_id": "up_audio_1",
                    "name": "voice.mp3",
                    "mime": "audio/mpeg",
                    "size": 30,
                    "kind": "file",
                }
            ],
        },
        content="key 只会显示一次",
        message_id="card-123",
    )
    types = [child["type"] for child in node["children"]]
    assert "input" in types
    assert types[-1] == "audio"
    audio = node["children"][-1]
    assert audio["upload_id"] == "up_audio_1"


def test_attachment_records_with_unsafe_ids_are_skipped() -> None:
    assert (
        component_payload(
            "attachments",
            "x",
            {"files": [{"id": "../escape", "name": "x.png", "mime": "image/png"}]},
            "m1",
        )
        is None
    )
    assert (
        component_payload(
            "text", "x", {"attachments": [{"upload_id": "/etc/passwd", "name": "x"}]}, "m1"
        )
        is None
    )


# --- 接口兼容 ---


def _seed(container: Container, task_id: uuid.UUID, kind: str, content: str, payload: dict) -> str:
    async def _run() -> str:
        messages = await container.task_repo.add_messages(
            task_id,
            [MessageDraft(role="assistant", kind=kind, content=content, payload=payload)],
        )
        return str(messages[0].id)

    return asyncio.run(_run())


def _create_bot(client: TestClient, name: str = "组件 Bot") -> dict:
    response = client.post(f"{PREFIX}/bots", json={"name": name, "description": "负责发卡片"})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_task_messages_api_returns_component_alongside_legacy_fields(client: TestClient) -> None:
    created = client.post(f"{PREFIX}/tasks", json={"description": "发计划"})
    task_id = created.json()["data"]["id"]
    container: Container = client.app.state.container
    _seed(
        container,
        uuid.UUID(task_id),
        "plan_card",
        "实施计划：登录改造（1 步）",
        {
            "title": "登录改造",
            "steps": [{"title": "改接口", "detail": "支持刷新"}],
            "files": [],
            "plan_path": "/opt/flux/plans/x.md",
            "markdown": None,
        },
    )

    response = client.get(f"{PREFIX}/tasks/{task_id}/messages")
    assert response.status_code == 200, response.text
    message = response.json()["data"][-1]
    assert message["kind"] == "plan_card"
    assert message["content"] == "实施计划：登录改造（1 步）"
    assert message["payload"]["title"] == "登录改造"
    assert message["payload"]["schema_version"] == 1
    assert message["payload"]["component"]["type"] == "card"
    assert message["payload"]["fallback_text"] == message["content"]


def test_bot_conversation_api_returns_component(client: TestClient) -> None:
    bot = _create_bot(client)
    container: Container = client.app.state.container

    async def _seed_chat() -> uuid.UUID:
        task = await container.task_repo.create(
            task_id=uuid.uuid4(),
            description="聊天",
            priority=100,
            project_id=None,
            agent_id=None,
            bot_id=uuid.UUID(bot["id"]),
            cloud_user_id="1",
        )
        return task.id

    task_id = asyncio.run(_seed_chat())
    _seed(
        container,
        task_id,
        "confirmation",
        "需求确认（最终版本）",
        {
            "confirmation": {
                "items": [{"label": "目标", "value": "支持修改邮箱"}],
                "actor": "user",
                "updated_at": "2026-11-14T00:00:00+00:00",
            }
        },
    )

    response = client.get(f"{PREFIX}/bots/{bot['id']}/conversation")
    assert response.status_code == 200, response.text
    message = response.json()["data"]["messages"][-1]
    assert message["kind"] == "confirmation"
    assert message["payload"]["component"]["type"] == "card"
    assert message["payload"]["confirmation"]["items"][0]["value"] == "支持修改邮箱"


def test_task_messages_api_maps_inbound_attachments_without_path(client: TestClient) -> None:
    created = client.post(f"{PREFIX}/tasks", json={"description": "看图"})
    task_id = created.json()["data"]["id"]
    container: Container = client.app.state.container
    _seed(
        container,
        uuid.UUID(task_id),
        "attachments",
        "chart.png、report.pdf",
        {
            "files": [
                {
                    "id": "a" * 32,
                    "name": "chart.png",
                    "size": 10,
                    "mime": "image/png",
                    "path": "/opt/flux/uploads/a/chart.png",
                },
                {
                    "id": "b" * 32,
                    "name": "report.pdf",
                    "size": 20,
                    "mime": "application/pdf",
                    "path": "/opt/flux/uploads/b/report.pdf",
                },
            ]
        },
    )

    response = client.get(f"{PREFIX}/tasks/{task_id}/messages")
    assert response.status_code == 200, response.text
    message = response.json()["data"][-1]
    assert message["kind"] == "attachments"
    assert message["payload"]["component"]["type"] == "card"
    assert [child["type"] for child in message["payload"]["component"]["children"]] == [
        "text",
        "image",
        "file",
    ]
    assert message["payload"]["component"]["children"][1]["upload_id"] == "a" * 32
    # 客户端 JSON 不出现宿主路径；原始 payload 的展示字段仍在。
    assert all("path" not in item for item in message["payload"]["files"])
    assert "/opt/flux/uploads" not in response.text


def test_bot_conversation_api_maps_outbound_attachments(client: TestClient) -> None:
    bot = _create_bot(client, "出站附件 Bot")
    container: Container = client.app.state.container

    async def _seed_chat() -> uuid.UUID:
        task = await container.task_repo.create(
            task_id=uuid.uuid4(),
            description="聊天",
            priority=100,
            project_id=None,
            agent_id=None,
            bot_id=uuid.UUID(bot["id"]),
            cloud_user_id="1",
        )
        return task.id

    task_id = asyncio.run(_seed_chat())
    attachments = [
        {
            "upload_id": "up_image_1",
            "name": "chart.png",
            "mime": "image/png",
            "size": 10,
            "kind": "image",
        }
    ]
    _seed(
        container,
        task_id,
        "text",
        "已经生成图表",
        {"attachments": attachments},
    )

    response = client.get(f"{PREFIX}/bots/{bot['id']}/conversation")
    assert response.status_code == 200, response.text
    message = response.json()["data"]["messages"][-1]
    assert message["kind"] == "text"
    # 原有正文与 payload 不变，组件树额外带上附件节点。
    assert message["content"] == "已经生成图表"
    assert message["payload"]["attachments"] == attachments
    component = message["payload"]["component"]
    assert component["type"] == "card"
    assert [child["type"] for child in component["children"]] == ["text", "image"]
    assert component["children"][1]["upload_id"] == "up_image_1"
