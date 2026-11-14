"""生成式组件 schema 校验（schema_version = 1）。

重点不是"能塞进去多少"，而是边界必须明确：
- 类型白名单之外的节点、未定义字段、缺失必填字段一律拒绝；
- 深度 / 节点数 / 子节点数 / 字符串长度 / 整棵树大小都有硬上限；
- URL 只允许 https（以及服务端签发的 delivery_id），拒绝 javascript/data/file/http 与本机内网；
- 文本不接受 HTML / 脚本片段，markdown 只作为数据；
- schema_version 目前只接受 1。
"""

from __future__ import annotations

import pytest

from flux.core.components.schema import (
    MAX_CHILDREN,
    MAX_DEPTH,
    MAX_NODES,
    MAX_OPTIONS,
    MAX_TEXT_LENGTH,
    SCHEMA_VERSION,
    validate_component,
    validate_component_payload,
)
from flux.errors import ValidationError


def _card(*children: dict) -> dict:
    return {"type": "card", "title": "标题", "children": list(children)}


def _text(value: str = "正文") -> dict:
    return {"type": "text", "text": value}


def _payload(node: dict, fallback: str = "兜底文案") -> dict:
    return {"schema_version": SCHEMA_VERSION, "component": node, "fallback_text": fallback}


# --- 合法树 ---


def test_accepts_full_tree_and_normalizes_alias_and_string_items() -> None:
    raw = _card(
        _text("第一段"),
        {"type": "image", "url": "https://cdn.example.com/a.png", "alt": "图", "width": 320},
        {"type": "file", "name": "报告.pdf", "delivery_id": "abc123", "index": 1, "size": 1024},
        {"type": "audio", "delivery_id": "voice1", "duration_ms": 1200, "transcript": "你好"},
        {
            "type": "button",
            "label": "打开桌面",
            "action": {"type": "client", "name": "open_desktop"},
        },
        {
            "type": "choice",
            "name": "plan",
            "options": [{"id": "a", "label": "方案 A"}, {"id": "b", "label": "方案 B"}],
        },
        {
            "type": "input",
            "name": "api_key",
            "input_type": "secure",
            "placeholder": "粘贴密钥",
        },
        # status 是 progress 的兼容别名
        {"type": "status", "label": "上传中", "percent": 40, "status": "running"},
        {"type": "list", "ordered": True, "items": ["纯文本项", _text("节点项")]},
        {"type": "divider"},
    )
    result = validate_component(raw)
    assert result["type"] == "card"
    assert result["title"] == "标题"
    types = [node["type"] for node in result["children"]]
    assert types == [
        "text",
        "image",
        "file",
        "audio",
        "button",
        "choice",
        "input",
        "progress",
        "list",
        "divider",
    ]
    # 字符串列表项被归一化成 text 节点
    assert result["children"][8]["items"][0] == {"type": "text", "text": "纯文本项"}
    # delivery 引用默认 index=0；image 的 index 不允许出现在 url 形式里
    assert result["children"][2]["index"] == 1
    assert result["children"][7]["percent"] == 40


def test_validate_payload_returns_only_contract_fields() -> None:
    result = validate_component_payload(_payload(_text()))
    assert set(result) == {"schema_version", "component", "fallback_text"}
    assert result["schema_version"] == 1
    assert result["component"]["type"] == "text"


# --- 类型 / 字段 ---


def test_unknown_type_is_rejected_with_chinese_message() -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate_component({"type": "iframe", "src": "https://x.dev"})
    assert "组件类型不支持" in excinfo.value.message
    assert "iframe" in excinfo.value.message


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate_component({"type": "text", "text": "hi", "onclick": "steal()"})
    assert "未定义的字段" in excinfo.value.message
    assert "onclick" in excinfo.value.message


def test_dangerous_prototype_keys_are_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate_component({"type": "card", "__proto__": {"polluted": True}, "children": []})
    assert "危险字段" in excinfo.value.message


@pytest.mark.parametrize(
    "node",
    [
        {"type": "text"},
        {"type": "card", "children": [{"type": "button", "label": "没有动作"}]},
        {"type": "file", "url": "https://x.dev/a", "delivery_id": "abc"},
        {"type": "choice", "name": "x", "options": []},
        {"type": "input"},
        {"type": "list", "items": []},
        {"type": "progress", "percent": 101},
        {"type": "progress", "status": "paused"},
    ],
)
def test_missing_or_invalid_required_fields_are_rejected(node: dict) -> None:
    with pytest.raises(ValidationError):
        validate_component(node)


# --- 上限 ---


def test_depth_limit_is_enforced() -> None:
    node: dict = _text()
    for _ in range(MAX_DEPTH):
        node = _card(node)
    with pytest.raises(ValidationError) as excinfo:
        validate_component(node)
    assert "嵌套层级超过上限" in excinfo.value.message


def test_children_limit_is_enforced() -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate_component(_card(*[_text() for _ in range(MAX_CHILDREN + 1)]))
    assert "子节点数量超过上限" in excinfo.value.message


def test_node_count_limit_is_enforced() -> None:
    # 每层扇出 50，深度控制在限制内，但总节点数超过 MAX_NODES。
    leaves = [_text() for _ in range(MAX_CHILDREN)]
    middle = [_card(*leaves) for _ in range(MAX_CHILDREN)]
    with pytest.raises(ValidationError) as excinfo:
        validate_component(_card(*middle))
    assert "组件节点数超过上限" in excinfo.value.message


def test_text_length_limit_is_enforced() -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate_component(_text("啊" * (MAX_TEXT_LENGTH + 1)))
    assert "超过长度上限" in excinfo.value.message


def test_payload_size_limit_is_enforced() -> None:
    # 单字段都在长度上限内，但整棵树序列化后超过 64KB。
    nodes = [_text("啊" * MAX_TEXT_LENGTH) for _ in range(MAX_NODES // 4)]
    with pytest.raises(ValidationError) as excinfo:
        validate_component(_card(*nodes))
    assert "组件树过大" in excinfo.value.message


def test_choice_option_limit_is_enforced() -> None:
    options = [{"id": f"o{i}", "label": f"选项 {i}"} for i in range(MAX_OPTIONS + 1)]
    with pytest.raises(ValidationError) as excinfo:
        validate_component({"type": "choice", "name": "x", "options": options})
    assert "选项数量超过上限" in excinfo.value.message


# --- URL ---


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        "http://example.com/a.png",
        "https://user:pass@example.com/a.png",
        "https://localhost/a.png",
        "https://127.0.0.1/a.png",
        "https://192.168.1.10/a.png",
        "https://10.0.0.1/a.png",
        "https://[::1]/a.png",
    ],
)
def test_unsafe_image_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValidationError):
        validate_component({"type": "image", "url": url})


def test_open_url_only_accepts_https_public_hosts() -> None:
    ok = validate_component(
        {
            "type": "button",
            "label": "打开文档",
            "action": {"type": "client", "name": "open_url", "url": "https://example.com/doc"},
        }
    )
    assert ok["action"]["url"] == "https://example.com/doc"
    with pytest.raises(ValidationError):
        validate_component(
            {
                "type": "button",
                "label": "打开",
                "action": {"type": "client", "name": "open_url", "url": "javascript:alert(1)"},
            }
        )


def test_delivery_reference_is_accepted_and_validated() -> None:
    node = validate_component({"type": "image", "delivery_id": "delivery_1", "index": 2})
    assert node["delivery_id"] == "delivery_1"
    assert node["index"] == 2
    with pytest.raises(ValidationError):
        validate_component({"type": "image", "delivery_id": "../etc/passwd"})


def test_upload_id_reference_is_accepted_for_image_file_audio() -> None:
    image = validate_component(
        {"type": "image", "upload_id": "abc123", "alt": "截图", "mime": "image/png"}
    )
    assert image == {
        "type": "image",
        "upload_id": "abc123",
        "alt": "截图",
        "mime": "image/png",
    }
    assert "index" not in image

    document = validate_component(
        {"type": "file", "name": "报告.pdf", "upload_id": "abc123", "size": 10}
    )
    assert document["upload_id"] == "abc123"
    assert document["name"] == "报告.pdf"

    audio = validate_component({"type": "audio", "upload_id": "voice_1"})
    assert audio == {"type": "audio", "upload_id": "voice_1"}


@pytest.mark.parametrize(
    "node",
    [
        # url / delivery_id / upload_id 必须且只能有一个
        {"type": "image", "url": "https://x.dev/a.png", "upload_id": "abc123"},
        {"type": "image", "delivery_id": "d1", "upload_id": "abc123"},
        {
            "type": "file",
            "name": "x",
            "url": "https://x.dev/a",
            "delivery_id": "d1",
            "upload_id": "abc123",
        },
        {"type": "image"},
        # index 只属于 delivery_id
        {"type": "image", "upload_id": "abc123", "index": 1},
        # upload_id 只允许目录名安全字符
        {"type": "image", "upload_id": "../etc/passwd"},
        {"type": "image", "upload_id": "a/b"},
        {"type": "image", "upload_id": ""},
        {"type": "image", "upload_id": "x" * 65},
    ],
)
def test_upload_id_source_is_exclusive_and_validated(node: dict) -> None:
    with pytest.raises(ValidationError):
        validate_component(node)


# --- 文本 / 动作 ---


@pytest.mark.parametrize(
    "value",
    ["<script>alert(1)</script>", "<iframe src='https://x.dev'></iframe>", "javascript:alert(1)"],
)
def test_html_and_script_are_rejected_in_text(value: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate_component(_text(value))
    assert "HTML / 脚本" in excinfo.value.message


def test_server_action_shape_is_validated() -> None:
    node = validate_component(
        {
            "type": "button",
            "label": "同意",
            "action": {
                "type": "server",
                "name": "decision.choose",
                "action_id": "decision:abc:0",
                "decision_id": "abc",
                "option": "方案 A",
            },
        }
    )
    assert node["action"]["action_id"] == "decision:abc:0"
    with pytest.raises(ValidationError):
        validate_component(
            {
                "type": "button",
                "label": "同意",
                "action": {"type": "server", "name": "decision.choose", "action_id": "x"},
            }
        )
    with pytest.raises(ValidationError):
        validate_component(
            {
                "type": "button",
                "label": "危险",
                "action": {"type": "server", "name": "shell.exec", "action_id": "x"},
            }
        )


# --- 版本 / payload ---


def test_unknown_schema_version_is_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate_component_payload({**_payload(_text()), "schema_version": 2})
    assert "schema_version" in excinfo.value.message


def test_payload_requires_fallback_text() -> None:
    with pytest.raises(ValidationError):
        validate_component_payload({"schema_version": 1, "component": _text()})
    with pytest.raises(ValidationError):
        validate_component_payload(
            {"schema_version": 1, "component": _text(), "fallback_text": " "}
        )


def test_strict_payload_rejects_extra_fields_but_legacy_read_tolerates_them() -> None:
    payload = {**_payload(_text()), "decision": {"id": "d1"}}
    with pytest.raises(ValidationError):
        validate_component_payload(payload)
    result = validate_component_payload(payload, strict=False)
    assert result["component"]["type"] == "text"


def test_validation_does_not_mutate_input() -> None:
    raw = _card(_text(), {"type": "status", "status": "done"})
    before = repr(raw)
    validate_component(raw)
    assert repr(raw) == before
