"""提案入参校验器测试（MCP `proposal.create` 的入参把关）。

校验器只做平台侧把关：结构合法、路径不越界、无重复路径、content 是完整文件内容。
提案一律由 agent 经 MCP 提交——Flux 不组装 prompt、不解析模型对话（目标架构 §1）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from flux.core.virtual_workspace.proposal_parser import parse_code_change_set
from flux.errors import ValidationError

_VALID_PAYLOAD = {
    "summary": "新增 /health 健康检查接口",
    "changes": [
        {
            "path": "app/main.py",
            "content": (
                "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n"
                "@app.get('/health')\ndef health():\n    return {'status': 'ok'}\n"
            ),
            "reason": "在 API 入口注册健康检查路由",
        },
        {
            "path": "tests/test_health.py",
            "content": "def test_health():\n    assert True\n",
            "reason": "补一个覆盖新接口的测试",
        },
    ],
}


def test_parse_returns_code_change_set() -> None:
    change_set = parse_code_change_set(json.dumps(_VALID_PAYLOAD))

    assert change_set.summary == "新增 /health 健康检查接口"
    assert change_set.paths == ("app/main.py", "tests/test_health.py")
    assert change_set.changes[0].reason == "在 API 入口注册健康检查路由"
    assert "/health" in change_set.changes[0].content
    assert change_set.to_dict()["changes"][1] == {
        "path": "tests/test_health.py",
        "content": "def test_health():\n    assert True\n",
        "reason": "补一个覆盖新接口的测试",
    }


def test_parse_keeps_content_byte_for_byte() -> None:
    """content 是完整文件内容，必须原样保留：不转义、不折叠、不裁剪。"""
    content = 'if __name__ == "__main__":\n    print("你好\\n世界")\n'
    payload = {"summary": "x", "changes": [{"path": "app/main.py", "content": content}]}

    assert parse_code_change_set(json.dumps(payload)).changes[0].content == content


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("```json\n" + json.dumps(_VALID_PAYLOAD) + "\n```", "fenced"),
        (
            "好的，改动如下：\n\n```json\n"
            + json.dumps(_VALID_PAYLOAD)
            + "\n```\n\n以上就是全部改动。",
            "prose",
        ),
        ("{\n" + json.dumps(_VALID_PAYLOAD)[1:-1] + "\n}\n（改动已完成）", "trailing"),
        (json.dumps(_VALID_PAYLOAD), "plain"),
    ],
)
def test_parse_tolerates_common_wrapping(raw: str, expected: str) -> None:
    assert parse_code_change_set(raw).paths == ("app/main.py", "tests/test_health.py"), expected


def test_parse_accepts_raw_newlines_inside_json_strings() -> None:
    """模型把换行直接写进 JSON 字符串时语义仍明确，宽松解析应接受。"""
    raw = '{"summary":"换行","changes":[{"path":"a.py","content":"line1\nline2"}]}'
    result = parse_code_change_set(raw)
    assert result.changes[0].content == "line1\nline2"


def test_parse_rejects_non_json() -> None:
    with pytest.raises(ValidationError) as excinfo:
        parse_code_change_set("抱歉，我需要更多信息才能完成这个需求。")

    assert "提案不是合法的 JSON" in excinfo.value.message
    assert excinfo.value.code == "validation_error"
    assert excinfo.value.details["source"] == "proposal"
    assert "更多信息" in excinfo.value.details["raw_preview"]


def test_parse_error_details_carry_source_label() -> None:
    """source 进错误详情，供审计区分提案来源（agent / 导入 / 测试）。"""
    with pytest.raises(ValidationError) as excinfo:
        parse_code_change_set("not-json", source="builtin-agent")

    assert excinfo.value.details["source"] == "builtin-agent"


def test_parse_rejects_absolute_and_escaping_paths() -> None:
    for path in ("/etc/passwd", "../outside.py", "app/../../outside.py"):
        payload = {"summary": "x", "changes": [{"path": path, "content": "x"}]}
        with pytest.raises(ValidationError) as excinfo:
            parse_code_change_set(json.dumps(payload))
        assert "path" in excinfo.value.message


def test_parse_normalises_paths() -> None:
    payload = {"summary": "x", "changes": [{"path": "./app//main.py", "content": "x"}]}
    assert parse_code_change_set(json.dumps(payload)).paths == ("app/main.py",)


def test_parse_rejects_duplicate_paths() -> None:
    payload = {
        "summary": "x",
        "changes": [{"path": "a.py", "content": "1"}, {"path": "./a.py", "content": "2"}],
    }
    with pytest.raises(ValidationError) as excinfo:
        parse_code_change_set(json.dumps(payload))
    assert "重复路径" in excinfo.value.message


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ([], "顶层必须是 JSON 对象"),
        ({}, "changes 必须是非空数组"),
        ({"changes": []}, "changes 必须是非空数组"),
        ({"changes": "app/main.py"}, "changes 必须是非空数组"),
        ({"changes": [{"content": "x"}]}, "path 必须是非空字符串"),
        ({"changes": [{"path": "  ", "content": "x"}]}, "path 必须是非空字符串"),
        ({"changes": [{"path": "a.py"}]}, "content 必须是字符串"),
        ({"changes": [{"path": "a.py", "content": 1}]}, "content 必须是字符串"),
        ({"changes": [{"path": "a.py", "content": "x", "reason": 1}]}, "reason 必须是字符串"),
        ({"summary": 1, "changes": [{"path": "a.py", "content": "x"}]}, "summary 必须是字符串"),
        ({"changes": ["a.py"]}, "changes[0] 必须是对象"),
    ],
)
def test_parse_rejects_malformed_payload(payload: Any, expected: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        parse_code_change_set(json.dumps(payload))
    assert expected in excinfo.value.message
    assert "raw_preview" in excinfo.value.details


def test_parse_preview_is_truncated() -> None:
    with pytest.raises(ValidationError) as excinfo:
        parse_code_change_set("{" + "x" * 5000)
    preview = excinfo.value.details["raw_preview"]
    assert len(preview) == 301 and preview.endswith("…")
