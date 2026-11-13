"""技能库契约测试：格式解析、CRUD、Bot 分配与对话上下文注入。

真实模型一律替换成记录型助手替身，不触网。技能正文只用于断言是否进入
`context`，用例本身不依赖任何具体提示词。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flux.core.skills.package import (
    MAX_BODY_CHARS,
    build_package,
    parse_package_document,
)
from flux.core.task_engine.assistant import AssistantTurn
from flux.errors import ValidationError

PREFIX = "/api/v1"


class _FakeAssistant:
    """记录每次调用的助手替身。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def respond(self, **kwargs: Any) -> AssistantTurn:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return AssistantTurn(
            reply=f"echo: {kwargs['message']}",
            provider="fake",
            model="fake-model",
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )


def _fake(client: TestClient) -> _FakeAssistant:
    fake = _FakeAssistant()
    client.app.state.container.assistant = fake  # type: ignore[attr-defined]
    return fake


def _create_bot(client: TestClient, *, name: str = "小技") -> dict:
    response = client.post(f"{PREFIX}/bots", json={"name": name, "description": "日常问答"})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _create_skill(
    client: TestClient,
    *,
    name: str = "disk-check",
    body: str = "先运行 df，再汇报磁盘占用。",
    **extra: Any,
) -> dict:
    payload = {"name": name, "description": f"{name} 的说明", "body": body, **extra}
    response = client.post(f"{PREFIX}/skills", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _assign(client: TestClient, bot_id: str, entries: list[dict]) -> list[dict]:
    response = client.put(f"{PREFIX}/bots/{bot_id}/skills", json={"skills": entries})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _chat(client: TestClient, bot_id: str, content: str = "今天做什么") -> str | None:
    created = client.post(f"{PREFIX}/bots/{bot_id}/turns", json={"content": content})
    assert created.status_code == 202, created.text
    turn_id = created.json()["data"]["turn"]["id"]
    done = client.get(f"{PREFIX}/bots/{bot_id}/turns/{turn_id}", params={"wait": 5}).json()["data"][
        "turn"
    ]
    assert done["status"] == "completed", done
    return client.app.state.container.assistant.calls[-1]["context"]  # type: ignore[attr-defined]


# --- 包格式解析 ---


def test_parse_valid_skill_md_document() -> None:
    document = """---
name: disk-check
description: 巡检磁盘
version: "1.2"
entry: SKILL.md
requires_bins: [df]
permissions: [shell]
network: false
files: [notes/checklist.md]
---
# 步骤
先运行 df，再汇报。
"""
    package = parse_package_document(document)

    assert package.name == "disk-check"
    assert package.description == "巡检磁盘"
    assert package.version == "1.2"
    assert package.entry == "SKILL.md"
    assert package.requires_bins == ("df",)
    assert package.permissions == ("shell",)
    assert package.files == ("notes/checklist.md",)
    assert "先运行 df" in package.body
    assert package.to_spec()["source"] == "skill-library"


def test_parse_accepts_unquoted_numeric_version() -> None:
    package = parse_package_document("---\nname: numeric-version\nversion: 1.0\n---\n正文")
    assert package.version == "1.0"


def test_parse_skill_md_supports_nested_metadata() -> None:
    document = """---
name: nested-skill
metadata:
  requires:
    bins: [git]
    skills: [base]
  permissions: [read]
  network: true
---
正文
"""
    package = parse_package_document(document)
    assert package.requires_bins == ("git",)
    assert package.requires_skills == ("base",)
    assert package.permissions == ("read",)
    assert package.network is True


def test_parse_rejects_missing_frontmatter() -> None:
    with pytest.raises(ValidationError):
        parse_package_document("没有 frontmatter 的正文")


def test_parse_rejects_path_escape() -> None:
    with pytest.raises(ValidationError):
        parse_package_document("---\nname: evil\nentry: ../escape.md\n---\n正文")
    with pytest.raises(ValidationError):
        parse_package_document("---\nname: evil\nfiles: [ok.md, /etc/passwd]\n---\n正文")


def test_parse_rejects_bad_name_and_oversized_body() -> None:
    with pytest.raises(ValidationError):
        build_package({"name": "../escape", "body": "正文"})
    with pytest.raises(ValidationError):
        build_package({"name": "too-large", "body": "x" * (MAX_BODY_CHARS + 1)})
    with pytest.raises(ValidationError):
        build_package({"name": "empty-body", "body": "  "})


def test_document_round_trip() -> None:
    package = parse_package_document("---\nname: round-trip\ndescription: 说明\n---\n正文")
    again = parse_package_document(package.to_document())
    assert again.to_spec() == package.to_spec()
    assert again.body == package.body


# --- CRUD ---


def test_create_and_get_skill_from_json(client: TestClient) -> None:
    created = _create_skill(client, name="json-skill", body="按步骤执行。", network=True)
    assert created["name"] == "json-skill"
    assert created["body"] == "按步骤执行。"
    assert created["body_chars"] == len("按步骤执行。")
    assert created["spec"]["network"] is True
    assert created["enabled"] is True

    detail = client.get(f"{PREFIX}/skills/{created['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["body"] == "按步骤执行。"

    listed = client.get(f"{PREFIX}/skills").json()
    assert listed["metadata"]["count"] == 1
    # 列表不带正文，避免一次吐出大量文本
    assert "body" not in listed["data"][0]
    assert listed["data"][0]["body_chars"] == len("按步骤执行。")


def test_create_skill_from_skill_md_content(client: TestClient) -> None:
    document = "---\nname: md-skill\ndescription: 来自文档\nrequires_bins: [df]\n---\n执行正文。"
    response = client.post(f"{PREFIX}/skills", json={"content": document})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["name"] == "md-skill"
    assert data["body"] == "执行正文。"
    assert data["spec"]["requires_bins"] == ["df"]


def test_create_skill_validation_errors(client: TestClient) -> None:
    missing_name = client.post(f"{PREFIX}/skills", json={"body": "正文"})
    assert missing_name.status_code == 422, missing_name.text

    empty_body = client.post(f"{PREFIX}/skills", json={"name": "abc", "body": " "})
    assert empty_body.status_code == 422, empty_body.text

    bad_doc = client.post(f"{PREFIX}/skills", json={"content": "没有 frontmatter"})
    assert bad_doc.status_code == 422, bad_doc.text

    bad_path = client.post(
        f"{PREFIX}/skills",
        json={"name": "abc", "body": "正文", "entry": "../escape.md"},
    )
    assert bad_path.status_code == 422, bad_path.text

    both = client.post(
        f"{PREFIX}/skills",
        json={"content": "---\nname: abc\n---\n正文", "name": "abc"},
    )
    assert both.status_code == 422, both.text


def test_duplicate_skill_name_conflicts(client: TestClient) -> None:
    _create_skill(client, name="dup")
    response = client.post(f"{PREFIX}/skills", json={"name": "dup", "body": "另一份"})
    assert response.status_code == 409, response.text


def test_unknown_skill_returns_404(client: TestClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"{PREFIX}/skills/{missing}").status_code == 404
    assert client.patch(f"{PREFIX}/skills/{missing}", json={"name": "x"}).status_code == 404
    assert client.delete(f"{PREFIX}/skills/{missing}").status_code == 404


def test_patch_skill_updates_fields_and_can_clear_version(client: TestClient) -> None:
    created = _create_skill(client, name="patch-me", version="1.0")
    response = client.patch(
        f"{PREFIX}/skills/{created['id']}",
        json={"description": "新说明", "body": "新正文", "version": None},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["description"] == "新说明"
    assert data["body"] == "新正文"
    assert data["version"] is None

    renamed = client.patch(f"{PREFIX}/skills/{created['id']}", json={"name": "renamed"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["data"]["name"] == "renamed"


def test_patch_skill_from_document_rejects_extra_fields(client: TestClient) -> None:
    created = _create_skill(client, name="patch-doc")
    response = client.patch(
        f"{PREFIX}/skills/{created['id']}",
        json={"content": "---\nname: next\n---\n正文", "description": "冲突"},
    )
    assert response.status_code == 422, response.text

    updated = client.patch(
        f"{PREFIX}/skills/{created['id']}",
        json={"content": "---\nname: next\n---\n新正文"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["name"] == "next"
    assert updated.json()["data"]["body"] == "新正文"


def test_create_does_not_log_skill_body(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "NEVER_LOG_THIS_INSTRUCTION_TEXT"
    with caplog.at_level(logging.INFO, logger="flux.core.skills.service"):
        _create_skill(client, name="quiet", body=marker)
    assert marker not in caplog.text
    assert "技能包创建" in caplog.text


# --- 分配 ---


def test_assign_list_and_replace_bot_skills(client: TestClient) -> None:
    bot = _create_bot(client)
    first = _create_skill(client, name="first")
    second = _create_skill(client, name="second")

    rows = _assign(
        client,
        bot["id"],
        [
            {"skill_id": second["id"], "position": 0},
            {"skill_id": first["id"], "position": 1},
        ],
    )
    assert [row["skill_id"] for row in rows] == [second["id"], first["id"]]
    assert [row["position"] for row in rows] == [0, 1]

    listed = client.get(f"{PREFIX}/bots/{bot['id']}/skills").json()
    assert listed["metadata"]["count"] == 2
    assert [row["name"] for row in listed["data"]] == ["second", "first"]

    # skill_ids 简写：position 按数组顺序
    replaced = client.put(
        f"{PREFIX}/bots/{bot['id']}/skills",
        json={"skill_ids": [first["id"], second["id"]]},
    )
    assert replaced.status_code == 200, replaced.text
    assert [row["skill_id"] for row in replaced.json()["data"]] == [first["id"], second["id"]]

    # 空列表 = 全部解除
    cleared = client.put(f"{PREFIX}/bots/{bot['id']}/skills", json={"skills": []})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["data"] == []


def test_assignment_unknown_bot_or_skill(client: TestClient) -> None:
    missing_bot = "00000000-0000-0000-0000-000000000001"
    assert client.get(f"{PREFIX}/bots/{missing_bot}/skills").status_code == 404
    assert client.put(f"{PREFIX}/bots/{missing_bot}/skills", json={"skills": []}).status_code == 404

    bot = _create_bot(client)
    missing_skill = "00000000-0000-0000-0000-000000000002"
    response = client.put(
        f"{PREFIX}/bots/{bot['id']}/skills",
        json={"skills": [{"skill_id": missing_skill}]},
    )
    assert response.status_code == 404, response.text

    bad_uuid = client.put(
        f"{PREFIX}/bots/{bot['id']}/skills",
        json={"skills": [{"skill_id": "not-a-uuid"}]},
    )
    assert bad_uuid.status_code == 404, bad_uuid.text


def test_assignment_rejects_duplicates_and_mixed_shapes(client: TestClient) -> None:
    bot = _create_bot(client)
    skill = _create_skill(client, name="only-one")

    duplicate = client.put(
        f"{PREFIX}/bots/{bot['id']}/skills",
        json={
            "skills": [
                {"skill_id": skill["id"]},
                {"skill_id": skill["id"], "position": 1},
            ]
        },
    )
    assert duplicate.status_code == 400, duplicate.text

    mixed = client.put(
        f"{PREFIX}/bots/{bot['id']}/skills",
        json={"skills": [{"skill_id": skill["id"]}], "skill_ids": [skill["id"]]},
    )
    assert mixed.status_code == 400, mixed.text


def test_delete_assignment_then_package(client: TestClient) -> None:
    bot = _create_bot(client)
    skill = _create_skill(client, name="assigned")
    _assign(client, bot["id"], [{"skill_id": skill["id"]}])

    conflict = client.delete(f"{PREFIX}/skills/{skill['id']}")
    assert conflict.status_code == 409, conflict.text
    assert skill["id"] in conflict.text

    unassigned = client.delete(f"{PREFIX}/bots/{bot['id']}/skills/{skill['id']}")
    assert unassigned.status_code == 200, unassigned.text

    repeated = client.delete(f"{PREFIX}/bots/{bot['id']}/skills/{skill['id']}")
    assert repeated.status_code == 404, repeated.text

    deleted = client.delete(f"{PREFIX}/skills/{skill['id']}")
    assert deleted.status_code == 200, deleted.text
    assert client.get(f"{PREFIX}/skills/{skill['id']}").status_code == 404


# --- 对话注入 ---


def test_assigned_skill_is_injected_into_bot_context(client: TestClient) -> None:
    _fake(client)
    bot = _create_bot(client)
    skill = _create_skill(client, name="check-disk", body="先运行 df，再按磁盘占用排序汇报。")
    _assign(client, bot["id"], [{"skill_id": skill["id"]}])

    context = _chat(client, bot["id"]) or ""
    assert "【技能说明】" in context
    assert "check-disk" in context
    assert "先运行 df，再按磁盘占用排序汇报。" in context
    assert "【技能说明结束】" in context


def test_skill_is_not_injected_without_assignment(client: TestClient) -> None:
    _fake(client)
    bot = _create_bot(client)
    _create_skill(client, name="not-assigned", body="这段不应该出现 SKILL_BODY_MARKER。")

    context = _chat(client, bot["id"]) or ""
    assert "SKILL_BODY_MARKER" not in context
    assert "【技能说明】" not in context


def test_disabled_package_or_assignment_is_not_injected(client: TestClient) -> None:
    _fake(client)
    bot = _create_bot(client)
    disabled_package = _create_skill(
        client, name="disabled-package", body="PACKAGE_BODY_SHOULD_NOT_APPEAR"
    )
    client.patch(f"{PREFIX}/skills/{disabled_package['id']}", json={"enabled": False})
    _assign(client, bot["id"], [{"skill_id": disabled_package["id"]}])

    context = _chat(client, bot["id"]) or ""
    assert "PACKAGE_BODY_SHOULD_NOT_APPEAR" not in context

    enabled_package = _create_skill(
        client, name="disabled-assignment", body="ASSIGNMENT_BODY_SHOULD_NOT_APPEAR"
    )
    _assign(
        client,
        bot["id"],
        [{"skill_id": enabled_package["id"], "enabled": False}],
    )
    context = _chat(client, bot["id"]) or ""
    assert "ASSIGNMENT_BODY_SHOULD_NOT_APPEAR" not in context


def test_skill_block_truncates_at_char_budget(client: TestClient) -> None:
    _fake(client)
    bot = _create_bot(client)
    body = "开头标记。" + ("内容" * 10_000) + "结尾标记。"
    skill = _create_skill(client, name="huge", body=body)
    _assign(client, bot["id"], [{"skill_id": skill["id"]}])

    context = _chat(client, bot["id"]) or ""
    assert "开头标记。" in context
    assert "结尾标记。" not in context
    assert "超出上下文预算" in context
    # 技能块本身有上限，正文不可能整段进入上下文
    assert len(context) < len(body)


def test_render_failure_does_not_fail_turn(client: TestClient) -> None:
    _fake(client)
    bot = _create_bot(client)
    container = client.app.state.container

    class _BrokenSkills:
        async def render_block(self, _bot_id: Any) -> str:
            raise RuntimeError("技能库炸了")

    original = container.skills
    container.skills = _BrokenSkills()  # type: ignore[assignment]
    try:
        context = _chat(client, bot["id"])
    finally:
        container.skills = original
    # 失败只降级为「没有技能块」，轮次正常完成
    assert "技能库炸了" not in (context or "")


def test_skill_injection_failure_does_not_break_other_context(client: TestClient) -> None:
    _fake(client)
    bot = _create_bot(client)
    client.post(f"{PREFIX}/bots/{bot['id']}/memories", json={"content": "我负责巡检磁盘"})
    container = client.app.state.container

    class _BrokenSkills:
        async def render_block(self, _bot_id: Any) -> str:
            raise RuntimeError("boom")

    original = container.skills
    container.skills = _BrokenSkills()  # type: ignore[assignment]
    try:
        context = _chat(client, bot["id"]) or ""
    finally:
        container.skills = original
    assert "我负责巡检磁盘" in context
