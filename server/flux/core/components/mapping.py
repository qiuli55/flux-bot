"""既有消息 → 统一组件树的映射（读取时附带，不改写历史数据）。

为什么不回填数据库：`task_messages` 是不可变轨迹，组件只是"同一份数据的另一种视图"。
`TaskMessage.to_dict()` 在返回前把 `payload["component"]` 附在**原 payload 旁边**，
老的 `kind` / `content` / `payload` 字段一个不动——老客户端按原样渲染，新客户端优先读
`component`。映射是纯函数且确定性的（同一个 message id / decision id 生成同一个
`action_id`），因此分页、轮询、重复请求拿到的按钮标识不会变。

覆盖的类型（payload 形状以真实写入点为准）：

- `file_card`     → card + image/file/audio（delivery_id 引用）
- `plan_card`     → card + 有序 list（步骤 / 涉及文件 / 补充说明）
- `handoff`       → card + 「打开桌面接管」客户端动作
- `confirmation`  → card + 需求确认 list
- `subtask`       → card + progress
- `run_finished`  → card + progress
- `secure_input`  → card + secure input（服务端动作 secure_input.submit）
- `secure_input_result` → 仅文本回执，不含任何密钥材料
- `decision` / `decision_auto` / `decision_result` → card + 选项按钮（仅 pending 时）
- `component`     → 原样返回（写入时已校验）

`attachments`（用户上传的图片/文件/语音）暂不映射：附件目前只有服务端绝对路径，
没有按 upload_id 取回内容的鉴权接口，贸然给 App 一个取不到的引用没有意义。
后续补上附件读取端点后，再映射成 image/file/audio 节点即可。
"""

from __future__ import annotations

import re
from typing import Any

from flux.core.components.schema import (
    MAX_CHILDREN,
    MAX_LABEL_LENGTH,
    MAX_TEXT_LENGTH,
    SCHEMA_VERSION,
    validate_component,
)
from flux.errors import ValidationError

COMPONENT_KIND = "component"

#: 读取时会附带 `payload["component"]` 的既有消息类型。
MAPPED_KINDS: frozenset[str] = frozenset(
    {
        "file_card",
        "plan_card",
        "handoff",
        "confirmation",
        "subtask",
        "run_finished",
        "secure_input",
        "secure_input_result",
        "decision",
        "decision_auto",
        "decision_result",
    }
)

_HTML_TAG = re.compile(r"<\s*(?:script|iframe|object|embed|style|link|meta)\b", re.I)


def component_payload(
    kind: str,
    content: str,
    payload: Any,
    message_id: str | None,
) -> dict[str, Any] | None:
    """返回附加了 `component` 的 payload；不需要 / 无法映射时返回 None。

    调用方（`TaskMessage.to_dict`）只在返回非 None 时替换 payload，原字段保持不变。
    """
    if kind == COMPONENT_KIND:
        return payload if isinstance(payload, dict) else None
    if kind not in MAPPED_KINDS:
        return None
    base = dict(payload) if isinstance(payload, dict) else {}
    node = component_for_message(kind=kind, content=content, payload=base, message_id=message_id)
    if node is None:
        return None
    try:
        node = validate_component(node)
    except ValidationError:
        # 历史 payload 可能残缺：宁可退回纯文本，也不让读取接口 500。
        return None
    fallback = _fallback_text(content, node)
    return {
        **base,
        "schema_version": SCHEMA_VERSION,
        "component": node,
        "fallback_text": fallback,
    }


def component_for_message(
    *,
    kind: str,
    content: str,
    payload: dict[str, Any],
    message_id: str | None,
) -> dict[str, Any] | None:
    """把一条既有类型消息映射成组件树；不是映射类型或数据不完整时返回 None。"""
    if kind in {"decision", "decision_auto", "decision_result"}:
        return _decision_node(content, payload)
    if kind == "secure_input":
        return _secure_input_node(content, payload, message_id)
    mapper = _MAPPERS.get(kind)
    if mapper is None:
        return None
    return mapper(content, payload)


# ---------------------------------------------------------------- 各类型


def _file_card_node(content: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    files = payload.get("files")
    delivery_id = payload.get("delivery_id")
    if not isinstance(files, list) or not files or not isinstance(delivery_id, str):
        return None
    children: list[dict[str, Any]] = []
    for item in files[:MAX_CHILDREN]:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            continue
        name = _text(item.get("name"), MAX_LABEL_LENGTH) or f"文件 {index + 1}"
        mime = _text(item.get("mime"), 128)
        file_kind = str(item.get("kind") or "")
        if file_kind == "image":
            node: dict[str, Any] = {
                "type": "image",
                "delivery_id": delivery_id,
                "index": index,
                "alt": name,
            }
        elif file_kind == "audio":
            node = {"type": "audio", "delivery_id": delivery_id, "index": index}
        else:
            node = {
                "type": "file",
                "name": name,
                "delivery_id": delivery_id,
                "index": index,
            }
            size = item.get("size")
            if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
                node["size"] = size
        if mime and node["type"] != "audio":
            node["mime"] = mime
        children.append(node)
    if not children:
        return None
    title = _text(payload.get("note"), MAX_LABEL_LENGTH) or "文件"
    return _card(title, content=_text(content, MAX_TEXT_LENGTH), children=children)


def _plan_card_node(content: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    title = _text(payload.get("title"), MAX_LABEL_LENGTH)
    if not title:
        return None
    children: list[dict[str, Any]] = []
    steps = payload.get("steps")
    if isinstance(steps, list) and steps:
        items: list[dict[str, Any]] = []
        for step in steps[:MAX_CHILDREN]:
            if not isinstance(step, dict):
                continue
            step_title = _text(step.get("title"), MAX_LABEL_LENGTH)
            if not step_title:
                continue
            detail = _text(step.get("detail"), MAX_TEXT_LENGTH)
            text = f"**{step_title}**"
            if detail:
                text = f"{text}\n\n{detail}"
            items.append({"type": "text", "text": text, "markdown": True})
        if items:
            children.append({"type": "text", "text": "实施步骤", "markdown": True})
            children.append({"type": "list", "ordered": True, "items": items})
    files = payload.get("files")
    if isinstance(files, list) and files:
        items = [
            {"type": "text", "text": f"`{_text(item, MAX_LABEL_LENGTH)}`"}
            for item in files[:MAX_CHILDREN]
            if _text(item, MAX_LABEL_LENGTH)
        ]
        if items:
            children.append({"type": "text", "text": "涉及文件", "markdown": True})
            children.append({"type": "list", "items": items})
    markdown = _text(payload.get("markdown"), MAX_TEXT_LENGTH)
    if markdown:
        children.append({"type": "text", "text": markdown, "markdown": True})
    if not children:
        children.append({"type": "text", "text": _text(content, MAX_TEXT_LENGTH) or title})
    return _card(title, children=children)


def _handoff_node(content: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    target = _text(payload.get("to_agent"), MAX_LABEL_LENGTH)
    source = _text(payload.get("from_agent"), MAX_LABEL_LENGTH)
    title = f"已交接给 {target}" if target else "已交接"
    children: list[dict[str, Any]] = []
    body = _text(content, MAX_TEXT_LENGTH)
    if body:
        children.append({"type": "text", "text": body})
    children.append(
        {
            "type": "button",
            "label": "打开桌面接管",
            "action": {"type": "client", "name": "open_desktop"},
        }
    )
    return _card(title, subtitle=source, children=children)


def _confirmation_node(content: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    confirmation = payload.get("confirmation")
    if not isinstance(confirmation, dict):
        return None
    items = confirmation.get("items")
    if not isinstance(items, list) or not items:
        return None
    rows: list[dict[str, Any]] = []
    for item in items[:MAX_CHILDREN]:
        if not isinstance(item, dict):
            continue
        label = _text(item.get("label"), MAX_LABEL_LENGTH)
        value = _text(item.get("value"), MAX_TEXT_LENGTH)
        if not label or not value:
            continue
        rows.append({"type": "text", "text": f"{label}：{value}"})
    if not rows:
        return None
    children: list[dict[str, Any]] = [{"type": "list", "items": rows}]
    actor = _text(confirmation.get("actor"), 32)
    return _card(
        "需求确认", subtitle="我修改后的版本" if actor == "user" else None, children=children
    )


def _subtask_node(content: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    title = _text(payload.get("title"), MAX_LABEL_LENGTH) or "后台任务"
    children: list[dict[str, Any]] = []
    body = _text(content, MAX_TEXT_LENGTH)
    if body:
        children.append({"type": "text", "text": body})
    progress = _progress_node(
        label=_text(payload.get("status"), MAX_LABEL_LENGTH),
        status=_PROGRESS_BY_SUBTASK.get(str(payload.get("status") or "")),
    )
    if progress is not None:
        children.append(progress)
    if not children:
        return None
    return _card(title, children=children)


def _run_finished_node(content: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    status = str(payload.get("status") or "")
    children: list[dict[str, Any]] = []
    body = _text(content, MAX_TEXT_LENGTH)
    if body:
        children.append({"type": "text", "text": body})
    progress = _progress_node(
        label=_TITLE_BY_RUN_STATUS.get(status), status=_PROGRESS_BY_RUN.get(status)
    )
    if progress is not None:
        children.append(progress)
    error = _text(payload.get("error"), MAX_TEXT_LENGTH)
    if error and error not in (body or ""):
        children.append({"type": "text", "text": f"错误：{error}"})
    if not children:
        return None
    return _card(_TITLE_BY_RUN_STATUS.get(status, "执行结束"), children=children)


def _secure_input_node(
    content: str, payload: dict[str, Any], message_id: str | None
) -> dict[str, Any] | None:
    spec = payload.get("secure_input")
    if not isinstance(spec, dict) or not spec:
        return None
    title = _text(spec.get("title"), MAX_LABEL_LENGTH) or "安全输入"
    name = _text(spec.get("name"), 128) or title
    children: list[dict[str, Any]] = []
    body = _text(content, MAX_TEXT_LENGTH)
    if body:
        children.append({"type": "text", "text": body})
    description = _text(spec.get("description"), MAX_TEXT_LENGTH)
    if description and description != body:
        children.append({"type": "text", "text": description})
    input_node: dict[str, Any] = {
        "type": "input",
        "name": name,
        "placeholder": _text(spec.get("placeholder"), MAX_LABEL_LENGTH) or "粘贴密钥",
        "input_type": "secure",
        "label": title,
    }
    # 密钥明文永远不进卡片；这里只放一个确定性的服务端动作标识。
    if message_id:
        input_node["action"] = {
            "type": "server",
            "name": "secure_input.submit",
            "action_id": f"secure_input:{message_id}",
        }
    children.append(input_node)
    return _card(title, children=children)


def _secure_input_result_node(content: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    body = _text(content, MAX_TEXT_LENGTH)
    if not body:
        return None
    # 回执里只有 key 名称/掩码/状态；不复述 key_id 等内部标识。
    return _card("密钥已保存", children=[{"type": "text", "text": body}])


def _decision_node(content: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    record = payload.get("decision")
    if not isinstance(record, dict):
        return None
    status = str(record.get("status") or "")
    question = _text(record.get("question"), MAX_TEXT_LENGTH)
    body = _text(content, MAX_TEXT_LENGTH)
    children: list[dict[str, Any]] = []
    if question:
        children.append({"type": "text", "text": question})
    elif body:
        children.append({"type": "text", "text": body})
    context = _text(record.get("context"), MAX_TEXT_LENGTH)
    if context and context != question:
        children.append({"type": "text", "text": context})
    recommendation = _text(record.get("recommendation"), MAX_LABEL_LENGTH)
    if recommendation:
        children.append({"type": "text", "text": f"推荐：{recommendation}"})
    chosen = _text(record.get("chosen"), MAX_LABEL_LENGTH)
    if status != "pending" and chosen:
        children.append({"type": "text", "text": f"结果：{chosen}"})
    if status != "pending" and body and body != question:
        children.append({"type": "text", "text": body})

    options = record.get("options")
    labels: list[str] = []
    if isinstance(options, list):
        rows: list[dict[str, Any]] = []
        for option in options[:MAX_CHILDREN]:
            if not isinstance(option, dict):
                continue
            label = _text(option.get("label"), MAX_LABEL_LENGTH)
            if not label:
                continue
            labels.append(label)
            detail = _text(option.get("description"), MAX_TEXT_LENGTH)
            if not detail:
                detail = _text(option.get("impact"), MAX_TEXT_LENGTH)
            rows.append({"type": "text", "text": f"{label}：{detail}" if detail else label})
        if rows:
            children.append({"type": "list", "items": rows})

    decision_id = _text(record.get("id"), 128)
    if status == "pending" and decision_id and labels:
        buttons: list[dict[str, Any]] = []
        for index, label in enumerate(labels):
            buttons.append(
                {
                    "type": "button",
                    "label": label,
                    "action": {
                        "type": "server",
                        "name": "decision.choose",
                        "action_id": f"decision:{decision_id}:{index}",
                        "decision_id": decision_id,
                        "option": label,
                    },
                }
            )
        buttons.append(
            {
                "type": "button",
                "label": "都不合适",
                "action": {
                    "type": "server",
                    "name": "decision.reject",
                    "action_id": f"decision:{decision_id}:reject",
                    "decision_id": decision_id,
                },
            }
        )
        children.extend(buttons)
    elif status == "pending" and body and body != question:
        children.append({"type": "text", "text": body})

    if not children:
        return None
    title = (
        "需要你决定"
        if status == "pending"
        else "已自动决策"
        if status == "auto_resolved"
        else "决策结果"
    )
    return _card(title, children=children)


# ---------------------------------------------------------------- 工具


def _card(
    title: str | None,
    *,
    subtitle: str | None = None,
    content: str | None = None,
    children: list[dict[str, Any]],
) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "card", "children": children}
    if title:
        node["title"] = title
    if subtitle:
        node["subtitle"] = subtitle
    if content:
        children.insert(0, {"type": "text", "text": content})
    return node


def _progress_node(*, label: str | None, status: str | None) -> dict[str, Any] | None:
    if not label and not status:
        return None
    node: dict[str, Any] = {"type": "progress"}
    if label:
        node["label"] = label
    if status:
        node["status"] = status
    return node


def _fallback_text(content: str, node: dict[str, Any]) -> str:
    body = _text(content, MAX_TEXT_LENGTH)
    if body:
        return body
    found = _first_text(node)
    return found[:MAX_TEXT_LENGTH] if found else "卡片消息"


def _first_text(node: dict[str, Any]) -> str:
    if node.get("type") == "text" and isinstance(node.get("text"), str):
        return node["text"]
    for key in ("children", "items"):
        value = node.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    found = _first_text(item)
                    if found:
                        return found
    return ""


def _text(value: Any, limit: int) -> str | None:
    """把历史 payload 里的任意值降级成安全文本：非字符串丢弃，危险片段替换为全角。"""
    if not isinstance(value, str):
        return None
    text = value.replace("\x00", "").strip()
    if not text:
        return None
    text = _HTML_TAG.sub("＜", text)
    text = re.sub(r"javascript:", "javascript：", text, flags=re.I)
    return text[:limit]


_PROGRESS_BY_SUBTASK = {
    "running": "running",
    "completed": "done",
    "failed": "error",
    "cancelled": "idle",
}
_PROGRESS_BY_RUN = {
    "pending": "idle",
    "running": "running",
    "completed": "done",
    "failed": "error",
    "cancelled": "idle",
}
_TITLE_BY_RUN_STATUS = {
    "pending": "等待执行",
    "running": "正在执行",
    "completed": "执行完成",
    "failed": "执行失败",
    "cancelled": "已取消",
}

_MAPPERS = {
    "file_card": _file_card_node,
    "plan_card": _plan_card_node,
    "handoff": _handoff_node,
    "confirmation": _confirmation_node,
    "subtask": _subtask_node,
    "run_finished": _run_finished_node,
    "secure_input_result": _secure_input_result_node,
}
# decision 族需要 message_id 之外的分支处理，在 component_for_message 里单列。

__all__ = [
    "COMPONENT_KIND",
    "MAPPED_KINDS",
    "component_for_message",
    "component_payload",
]
