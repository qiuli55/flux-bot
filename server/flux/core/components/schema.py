"""生成式组件的版本化 schema 与服务端校验（schema_version = 1）。

组件以一条 `task_messages` 记录落库：`kind="component"`，`content` 为必填纯文本兜底，
`payload` 为 `{"schema_version": 1, "component": <node>, "fallback_text": "..."}`。
老消息不改写，读取时由 `mapping` 模块在同一 payload 里附带 `component`（见该模块）。

节点白名单（新增类型必须先改这里并同步 App）：

- `card`      容器：title / subtitle / children
- `text`      正文：text，可选 markdown（只作为数据，服务端不执行任何标记）
- `image`     图片：https url 或 delivery_id，可选 alt / mime / width / height
- `file`      文件：name + https url 或 delivery_id，可选 mime / size
- `audio`     音频：https url 或 delivery_id，可选 duration_ms / transcript
- `button`    按钮：label + action
- `choice`    选项：name 或 id + options（每项 {id, label}），可选 multiple / action
- `input`     输入：name，可选 placeholder / input_type（text | secure）/ label / action
- `progress`  进度：可选 label / percent / status；入参里的 `status` 类型名会归一成 `progress`
- `list`      列表：items（节点或字符串），可选 ordered
- `divider`   分隔线：无字段

动作分两类：

- client：客户端自己处理，不发服务端请求。目前允许 `open_desktop`（打开桌面接管）
  与 `open_url`（https + 非本机/内网地址）。
- server：`{type: "server", name, action_id, ...}`，只能由服务端在映射既有消息时铸造
  `action_id`；`component.emit` 工具禁止模型自带 server 动作。允许的服务端动作见
  `SERVER_ACTION_NAMES`，回调入口是 `POST /api/v1/tasks/{task_id}/messages/{id}/actions`。

安全边界：只允许 https（以及服务端签发的 delivery_id 引用）；拒绝 javascript: / data: /
file: / http:、带账号密码的 URL、本机与私有地址；拒绝 HTML/脚本片段；限制深度、节点数、
子节点数、字符串长度与整棵树的大小。校验失败抛 `ValidationError`（中文消息）。
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any
from urllib.parse import urlsplit

from flux.errors import ValidationError

#: 当前唯一支持的 schema 版本。老客户端看到不认识的版本应展示 fallback_text。
SCHEMA_VERSION = 1

#: 组件树上限：保守取值，够表达卡片/表单/步骤，又不会把对话撑爆。
MAX_DEPTH = 8
MAX_NODES = 200
MAX_CHILDREN = 50
MAX_TEXT_LENGTH = 4000
MAX_LABEL_LENGTH = 512
MAX_NAME_LENGTH = 128
MAX_URL_LENGTH = 2048
MAX_OPTIONS = 20
MAX_PAYLOAD_BYTES = 64 * 1024

NODE_TYPES: frozenset[str] = frozenset(
    {
        "card",
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
    }
)

#: `status` 是 `progress` 的兼容别名；归一化后只保留 `progress`。
NODE_TYPE_ALIASES: dict[str, str] = {"status": "progress"}

CLIENT_ACTION_NAMES: frozenset[str] = frozenset({"open_desktop", "open_url"})
SERVER_ACTION_NAMES: frozenset[str] = frozenset(
    {"decision.choose", "decision.reject", "secure_input.submit"}
)
PROGRESS_STATUSES: frozenset[str] = frozenset({"running", "done", "error", "idle"})
INPUT_TYPES: frozenset[str] = frozenset({"text", "secure"})

_DANGEROUS_KEYS: frozenset[str] = frozenset({"__proto__", "constructor", "prototype"})
_HTML_PATTERN = re.compile(r"<\s*(?:script|iframe|object|embed|style|link|meta)\b", re.I)
_DELIVERY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ACTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "card": frozenset({"type", "title", "subtitle", "children"}),
    "text": frozenset({"type", "text", "markdown"}),
    "image": frozenset({"type", "url", "delivery_id", "index", "alt", "mime", "width", "height"}),
    "file": frozenset({"type", "name", "url", "delivery_id", "index", "mime", "size"}),
    "audio": frozenset({"type", "url", "delivery_id", "index", "duration_ms", "transcript"}),
    "button": frozenset({"type", "label", "action"}),
    "choice": frozenset({"type", "name", "id", "options", "multiple", "action"}),
    "input": frozenset({"type", "name", "placeholder", "input_type", "label", "action"}),
    "progress": frozenset({"type", "label", "percent", "status"}),
    "list": frozenset({"type", "items", "ordered"}),
    "divider": frozenset({"type"}),
}

_ACTION_KEYS = frozenset({"type", "name", "url", "action_id", "decision_id", "option"})


class _State:
    """一次校验共享的计数与路径（用于把错误定位到具体节点）。"""

    def __init__(self) -> None:
        self.nodes = 0

    def count(self, path: str) -> None:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise ValidationError(
                f"组件节点数超过上限（最多 {MAX_NODES} 个）",
                details={"path": path, "limit": MAX_NODES},
            )


def validate_component(node: Any) -> dict[str, Any]:
    """校验并归一化一棵组件树；返回可安全落库的新对象（不修改入参）。

    非法输入一律抛 `ValidationError`，调用方必须在写库前调用本函数。
    """
    state = _State()
    normalized = _node(node, path="component", depth=1, state=state)
    size = len(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        raise ValidationError(
            f"组件树过大（{size} 字节，上限 {MAX_PAYLOAD_BYTES} 字节）",
            details={"bytes": size, "limit": MAX_PAYLOAD_BYTES},
        )
    return normalized


def validate_component_payload(payload: Any, *, strict: bool = True) -> dict[str, Any]:
    """校验 `kind="component"` 的完整 payload；返回归一化后的新对象。

    `strict=True` 时拒绝 schema_version / component / fallback_text 之外的键
    （新写入走这条）；`strict=False` 供读取既有消息时容忍附带元数据。
    """
    if not isinstance(payload, dict):
        raise ValidationError("组件消息 payload 必须是对象")
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValidationError(
            f"未知的组件 schema_version：{version}（当前只支持 {SCHEMA_VERSION}）",
            details={"schema_version": version},
        )
    if strict:
        unknown = sorted(set(payload) - {"schema_version", "component", "fallback_text"})
        if unknown:
            raise ValidationError(
                f"组件消息包含未定义的字段：{'、'.join(unknown)}",
                details={"unknown": unknown},
            )
    fallback = payload.get("fallback_text")
    if not isinstance(fallback, str) or not fallback.strip():
        raise ValidationError(
            "组件消息缺少纯文本兜底 fallback_text", details={"field": "fallback_text"}
        )
    if len(fallback) > MAX_TEXT_LENGTH:
        raise ValidationError(
            f"fallback_text 超过长度上限（最多 {MAX_TEXT_LENGTH} 字符）",
            details={"field": "fallback_text", "limit": MAX_TEXT_LENGTH},
        )
    _ensure_safe_text(fallback, field="fallback_text")
    component = validate_component(payload.get("component"))
    return {
        "schema_version": SCHEMA_VERSION,
        "component": component,
        "fallback_text": fallback,
    }


# ---------------------------------------------------------------- 节点


def _node(raw: Any, *, path: str, depth: int, state: _State) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("组件节点必须是对象", details={"path": path})
    if depth > MAX_DEPTH:
        raise ValidationError(
            f"组件嵌套层级超过上限（最多 {MAX_DEPTH} 层）",
            details={"path": path, "limit": MAX_DEPTH},
        )
    for key in raw:
        if key in _DANGEROUS_KEYS:
            raise ValidationError(
                f"组件节点包含危险字段：{key}", details={"path": path, "key": key}
            )
    raw_type = raw.get("type")
    if not isinstance(raw_type, str) or not raw_type.strip():
        raise ValidationError("组件节点缺少必填字段：type", details={"path": path})
    node_type = NODE_TYPE_ALIASES.get(raw_type, raw_type)
    if node_type not in NODE_TYPES:
        raise ValidationError(
            f"组件类型不支持：{raw_type}；允许的类型：{'、'.join(sorted(NODE_TYPES))}",
            details={"path": path, "type": raw_type, "allowed": sorted(NODE_TYPES)},
        )
    allowed = _ALLOWED_KEYS[node_type]
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValidationError(
            f"组件节点 {node_type} 包含未定义的字段：{'、'.join(unknown)}",
            details={"path": path, "type": node_type, "unknown": unknown},
        )

    state.count(path)
    builder = _BUILDERS[node_type]
    return builder(raw, path=path, depth=depth, state=state)


def _build_card(raw: dict[str, Any], *, path: str, depth: int, state: _State) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "card"}
    _optional_text(node, raw, "title", path=path, limit=MAX_LABEL_LENGTH)
    _optional_text(node, raw, "subtitle", path=path, limit=MAX_LABEL_LENGTH)
    children = _children(raw, path=path, depth=depth, state=state)
    node["children"] = children
    return node


def _build_text(raw: dict[str, Any], *, path: str, depth: int, state: _State) -> dict[str, Any]:
    text = _required_text(raw, "text", path=path, limit=MAX_TEXT_LENGTH)
    node: dict[str, Any] = {"type": "text", "text": text}
    markdown = raw.get("markdown", False)
    if not isinstance(markdown, bool):
        raise ValidationError("markdown 必须是布尔值", details={"path": path, "field": "markdown"})
    if markdown:
        node["markdown"] = True
    return node


def _build_image(raw: dict[str, Any], *, path: str, depth: int, state: _State) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "image"}
    _source_fields(node, raw, path=path)
    _optional_text(node, raw, "alt", path=path, limit=MAX_LABEL_LENGTH)
    _optional_text(node, raw, "mime", path=path, limit=MAX_NAME_LENGTH)
    for key in ("width", "height"):
        value = _optional_int(raw, key, path=path, minimum=1, maximum=10000)
        if value is not None:
            node[key] = value
    return node


def _build_file(raw: dict[str, Any], *, path: str, depth: int, state: _State) -> dict[str, Any]:
    node: dict[str, Any] = {
        "type": "file",
        "name": _required_text(raw, "name", path=path, limit=MAX_LABEL_LENGTH),
    }
    _source_fields(node, raw, path=path)
    _optional_text(node, raw, "mime", path=path, limit=MAX_NAME_LENGTH)
    size = _optional_int(raw, "size", path=path, minimum=0, maximum=5 * 1024 * 1024 * 1024)
    if size is not None:
        node["size"] = size
    return node


def _build_audio(raw: dict[str, Any], *, path: str, depth: int, state: _State) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "audio"}
    _source_fields(node, raw, path=path)
    duration = _optional_int(raw, "duration_ms", path=path, minimum=0, maximum=24 * 3600 * 1000)
    if duration is not None:
        node["duration_ms"] = duration
    _optional_text(node, raw, "transcript", path=path, limit=MAX_TEXT_LENGTH)
    return node


def _build_button(raw: dict[str, Any], *, path: str, depth: int, state: _State) -> dict[str, Any]:
    node: dict[str, Any] = {
        "type": "button",
        "label": _required_text(raw, "label", path=path, limit=MAX_LABEL_LENGTH),
    }
    node["action"] = _action(raw.get("action"), path=f"{path}.action")
    return node


def _build_choice(raw: dict[str, Any], *, path: str, depth: int, state: _State) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "choice"}
    name = _optional_text_value(raw, "name", path=path, limit=MAX_NAME_LENGTH)
    identifier = _optional_text_value(raw, "id", path=path, limit=MAX_NAME_LENGTH)
    if name is None and identifier is None:
        raise ValidationError(
            "组件节点缺少必填字段：name 或 id", details={"path": path, "type": "choice"}
        )
    if name is not None:
        node["name"] = name
    if identifier is not None:
        node["id"] = identifier
    options = raw.get("options")
    if not isinstance(options, list) or not options:
        raise ValidationError(
            "choice 节点缺少必填字段：options", details={"path": path, "type": "choice"}
        )
    if len(options) > MAX_OPTIONS:
        raise ValidationError(
            f"选项数量超过上限（最多 {MAX_OPTIONS} 项）",
            details={"path": path, "limit": MAX_OPTIONS},
        )
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(options):
        item_path = f"{path}.options[{index}]"
        if not isinstance(item, dict):
            raise ValidationError("每个选项必须是对象", details={"path": item_path})
        unknown = sorted(set(item) - {"id", "label"})
        if unknown:
            raise ValidationError(
                f"选项包含未定义的字段：{'、'.join(unknown)}",
                details={"path": item_path, "unknown": unknown},
            )
        option_id = _required_text(item, "id", path=item_path, limit=MAX_NAME_LENGTH)
        if option_id in seen:
            raise ValidationError(f"选项 id 重复：{option_id}", details={"path": item_path})
        seen.add(option_id)
        normalized.append(
            {
                "id": option_id,
                "label": _required_text(item, "label", path=item_path, limit=MAX_LABEL_LENGTH),
            }
        )
    node["options"] = normalized
    multiple = raw.get("multiple", False)
    if not isinstance(multiple, bool):
        raise ValidationError("multiple 必须是布尔值", details={"path": path, "field": "multiple"})
    if multiple:
        node["multiple"] = True
    if "action" in raw:
        node["action"] = _action(raw.get("action"), path=f"{path}.action")
    return node


def _build_input(raw: dict[str, Any], *, path: str, depth: int, state: _State) -> dict[str, Any]:
    node: dict[str, Any] = {
        "type": "input",
        "name": _required_text(raw, "name", path=path, limit=MAX_NAME_LENGTH),
    }
    _optional_text(node, raw, "placeholder", path=path, limit=MAX_LABEL_LENGTH)
    _optional_text(node, raw, "label", path=path, limit=MAX_LABEL_LENGTH)
    input_type = raw.get("input_type", "text")
    if input_type not in INPUT_TYPES:
        raise ValidationError(
            f"input_type 不受支持：{input_type}；允许：text、secure",
            details={"path": path, "field": "input_type"},
        )
    if input_type == "secure":
        node["input_type"] = "secure"
    if "action" in raw:
        node["action"] = _action(raw.get("action"), path=f"{path}.action")
    return node


def _build_progress(raw: dict[str, Any], *, path: str, depth: int, state: _State) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "progress"}
    _optional_text(node, raw, "label", path=path, limit=MAX_LABEL_LENGTH)
    percent = raw.get("percent")
    if percent is not None:
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            raise ValidationError("percent 必须是数字", details={"path": path, "field": "percent"})
        if percent < 0 or percent > 100:
            raise ValidationError(
                "percent 必须在 0~100 之间", details={"path": path, "field": "percent"}
            )
        node["percent"] = percent
    status = raw.get("status")
    if status is not None:
        if status not in PROGRESS_STATUSES:
            raise ValidationError(
                f"progress.status 不受支持：{status}；允许：running、done、error、idle",
                details={"path": path, "field": "status"},
            )
        node["status"] = status
    return node


def _build_list(raw: dict[str, Any], *, path: str, depth: int, state: _State) -> dict[str, Any]:
    items = raw.get("items")
    if not isinstance(items, list) or not items:
        raise ValidationError(
            "list 节点缺少必填字段：items", details={"path": path, "type": "list"}
        )
    if len(items) > MAX_CHILDREN:
        raise ValidationError(
            f"list items 超过上限（最多 {MAX_CHILDREN} 项）",
            details={"path": path, "limit": MAX_CHILDREN},
        )
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        item_path = f"{path}.items[{index}]"
        if isinstance(item, str):
            if not item.strip():
                raise ValidationError("列表文本项不能为空", details={"path": item_path})
            normalized.append(
                {
                    "type": "text",
                    "text": _checked_text(
                        item, field="text", limit=MAX_TEXT_LENGTH, path=item_path
                    ),
                }
            )
        else:
            normalized.append(_node(item, path=item_path, depth=depth + 1, state=state))
    node: dict[str, Any] = {"type": "list", "items": normalized}
    ordered = raw.get("ordered", False)
    if not isinstance(ordered, bool):
        raise ValidationError("ordered 必须是布尔值", details={"path": path, "field": "ordered"})
    if ordered:
        node["ordered"] = True
    return node


def _build_divider(raw: dict[str, Any], *, path: str, depth: int, state: _State) -> dict[str, Any]:
    return {"type": "divider"}


_BUILDERS = {
    "card": _build_card,
    "text": _build_text,
    "image": _build_image,
    "file": _build_file,
    "audio": _build_audio,
    "button": _build_button,
    "choice": _build_choice,
    "input": _build_input,
    "progress": _build_progress,
    "list": _build_list,
    "divider": _build_divider,
}


# ---------------------------------------------------------------- 字段


def _children(raw: dict[str, Any], *, path: str, depth: int, state: _State) -> list[dict[str, Any]]:
    children = raw.get("children")
    if children is None:
        return []
    if not isinstance(children, list):
        raise ValidationError("children 必须是数组", details={"path": path, "field": "children"})
    if len(children) > MAX_CHILDREN:
        raise ValidationError(
            f"子节点数量超过上限（最多 {MAX_CHILDREN} 个）",
            details={"path": path, "limit": MAX_CHILDREN},
        )
    return [
        _node(item, path=f"{path}.children[{index}]", depth=depth + 1, state=state)
        for index, item in enumerate(children)
    ]


def _required_text(raw: dict[str, Any], key: str, *, path: str, limit: int) -> str:
    if key not in raw:
        raise ValidationError(f"组件节点缺少必填字段：{key}", details={"path": path, "field": key})
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            f"组件字段 {key} 必须是非空字符串", details={"path": path, "field": key}
        )
    return _checked_text(value, field=key, limit=limit, path=path)


def _optional_text_value(raw: dict[str, Any], key: str, *, path: str, limit: int) -> str | None:
    if key not in raw:
        return None
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            f"组件字段 {key} 必须是非空字符串", details={"path": path, "field": key}
        )
    return _checked_text(value, field=key, limit=limit, path=path)


def _optional_text(
    node: dict[str, Any], raw: dict[str, Any], key: str, *, path: str, limit: int
) -> None:
    value = _optional_text_value(raw, key, path=path, limit=limit)
    if value is not None:
        node[key] = value


def _optional_int(
    raw: dict[str, Any], key: str, *, path: str, minimum: int, maximum: int
) -> int | None:
    if key not in raw:
        return None
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"组件字段 {key} 必须是整数", details={"path": path, "field": key})
    if value < minimum or value > maximum:
        raise ValidationError(
            f"组件字段 {key} 必须在 {minimum}~{maximum} 之间",
            details={"path": path, "field": key},
        )
    return value


def _source_fields(node: dict[str, Any], raw: dict[str, Any], *, path: str) -> None:
    """图片 / 文件 / 音频的来源：https url 或服务端签发的 delivery_id，二选一。"""
    has_url = "url" in raw
    has_delivery = "delivery_id" in raw
    if has_url == has_delivery:
        raise ValidationError(
            "组件节点必须且只能提供 url 或 delivery_id 之一",
            details={"path": path, "type": node["type"]},
        )
    if has_url:
        node["url"] = _https_url(raw.get("url"), field="url", path=path)
        if "index" in raw:
            raise ValidationError(
                "只有 delivery_id 引用才需要 index", details={"path": path, "field": "index"}
            )
    else:
        delivery_id = raw.get("delivery_id")
        if not isinstance(delivery_id, str) or not _DELIVERY_ID_PATTERN.match(delivery_id):
            raise ValidationError(
                "delivery_id 格式不合法", details={"path": path, "field": "delivery_id"}
            )
        node["delivery_id"] = delivery_id
        index = _optional_int(raw, "index", path=path, minimum=0, maximum=9999)
        node["index"] = index if index is not None else 0


def _action(raw: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("action 必须是对象", details={"path": path})
    unknown = sorted(set(raw) - _ACTION_KEYS)
    if unknown:
        raise ValidationError(
            f"action 包含未定义的字段：{'、'.join(unknown)}",
            details={"path": path, "unknown": unknown},
        )
    action_type = raw.get("type")
    name = raw.get("name")
    if action_type == "client":
        if not isinstance(name, str) or name not in CLIENT_ACTION_NAMES:
            raise ValidationError(
                f"客户端动作不受支持：{name}；允许：{'、'.join(sorted(CLIENT_ACTION_NAMES))}",
                details={"path": path, "name": name},
            )
        if "action_id" in raw or "decision_id" in raw or "option" in raw:
            raise ValidationError("客户端动作不能携带服务端动作字段", details={"path": path})
        action: dict[str, Any] = {"type": "client", "name": name}
        if name == "open_url":
            action["url"] = _https_url(raw.get("url"), field="url", path=path)
        elif "url" in raw:
            raise ValidationError("open_desktop 不接受 url", details={"path": path, "field": "url"})
        return action
    if action_type == "server":
        if not isinstance(name, str) or name not in SERVER_ACTION_NAMES:
            raise ValidationError(
                f"服务端动作不受支持：{name}；允许：{'、'.join(sorted(SERVER_ACTION_NAMES))}",
                details={"path": path, "name": name},
            )
        action_id = raw.get("action_id")
        if not isinstance(action_id, str) or not _ACTION_ID_PATTERN.match(action_id):
            raise ValidationError(
                "服务端动作缺少合法的 action_id", details={"path": path, "field": "action_id"}
            )
        if "url" in raw:
            raise ValidationError("服务端动作不接受 url", details={"path": path, "field": "url"})
        action = {"type": "server", "name": name, "action_id": action_id}
        if name in {"decision.choose", "decision.reject"}:
            decision_id = raw.get("decision_id")
            if not isinstance(decision_id, str) or not decision_id.strip():
                raise ValidationError(
                    "决策动作缺少 decision_id", details={"path": path, "field": "decision_id"}
                )
            action["decision_id"] = _checked_text(
                decision_id, field="decision_id", limit=MAX_NAME_LENGTH, path=path
            )
            if name == "decision.choose":
                action["option"] = _required_text(raw, "option", path=path, limit=MAX_LABEL_LENGTH)
            elif "option" in raw:
                raise ValidationError(
                    "decision.reject 不接受 option", details={"path": path, "field": "option"}
                )
        elif "decision_id" in raw or "option" in raw:
            raise ValidationError("secure_input.submit 不接受决策字段", details={"path": path})
        return action
    raise ValidationError(
        "action.type 必须是 client 或 server", details={"path": path, "type": action_type}
    )


def _https_url(raw: Any, *, field: str, path: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError(f"组件字段 {field} 必须是非空字符串", details={"path": path})
    value = raw.strip()
    if len(value) > MAX_URL_LENGTH:
        raise ValidationError(
            f"URL 超过长度上限（最多 {MAX_URL_LENGTH} 字符）",
            details={"path": path, "field": field},
        )
    parsed = urlsplit(value)
    if parsed.scheme != "https":
        raise ValidationError(
            f"不安全的 URL 协议：{parsed.scheme or '（空）'}；组件 URL 只允许 https",
            details={"path": path, "field": field, "scheme": parsed.scheme},
        )
    if not parsed.hostname:
        raise ValidationError("URL 缺少主机名", details={"path": path, "field": field})
    if parsed.username or parsed.password:
        raise ValidationError("URL 不能包含账号或密码", details={"path": path, "field": field})
    host = parsed.hostname.strip().lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValidationError(
            "URL 不能指向本机地址", details={"path": path, "field": field, "host": host}
        )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return value
    if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
        raise ValidationError(
            "URL 不能指向本机或内网地址",
            details={"path": path, "field": field, "host": host},
        )
    return value


def _checked_text(value: str, *, field: str, limit: int, path: str) -> str:
    if len(value) > limit:
        raise ValidationError(
            f"{field} 超过长度上限（最多 {limit} 字符）",
            details={"path": path, "field": field, "limit": limit},
        )
    _ensure_safe_text(value, field=field, path=path)
    return value


def _ensure_safe_text(value: str, *, field: str, path: str = "component") -> None:
    if "\x00" in value:
        raise ValidationError(f"{field} 不能包含空字符", details={"path": path, "field": field})
    if _HTML_PATTERN.search(value) or "javascript:" in value.lower():
        raise ValidationError(
            f"{field} 不能包含 HTML / 脚本内容",
            details={"path": path, "field": field},
        )


__all__ = [
    "CLIENT_ACTION_NAMES",
    "INPUT_TYPES",
    "MAX_CHILDREN",
    "MAX_DEPTH",
    "MAX_LABEL_LENGTH",
    "MAX_NODES",
    "MAX_OPTIONS",
    "MAX_PAYLOAD_BYTES",
    "MAX_TEXT_LENGTH",
    "NODE_TYPES",
    "NODE_TYPE_ALIASES",
    "PROGRESS_STATUSES",
    "SCHEMA_VERSION",
    "SERVER_ACTION_NAMES",
    "validate_component",
    "validate_component_payload",
]
