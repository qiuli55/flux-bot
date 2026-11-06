"""DSH 通知 → Flux 事件映射（集成方案 §18 Phase 1 第 6 步）。

Flux 前端不消费 DSH 原始协议，故在此把一条 DSH Notification 压扁成 Flux 事件体：
保留 `method` 与 `session_id` 作为关联键，把 `session.event` 里真正有业务含义的
`event_type` / `text` / `finish_reason` 提到顶层，前端订阅 `dsh.event` 即可消费。
"""

from __future__ import annotations

from typing import Any

from deepseek_harness import Notification

from flux.core.event.bus import Events


def to_flux_event(notification: Notification) -> tuple[str, dict[str, Any]]:
    """把一条 DSH 通知压成 Flux 事件体，返回 (事件名, 事件体)。

    无法识别的通知同样返回一条（`event_type` 为 None），绝不吞掉上游消息。
    """
    payload = notification.payload
    body: dict[str, Any] = {"method": notification.method, "event_type": None}
    session_id = payload.get("sessionId")
    if isinstance(session_id, str):
        body["session_id"] = session_id

    if notification.method == "session.event":
        event = payload.get("event")
        if isinstance(event, dict):
            event_type = event.get("type")
            if isinstance(event_type, str):
                body["event_type"] = event_type
            data = event.get("data")
            if event_type == "assistant/message":
                body["text"] = _assistant_text(data)
            elif event_type == "turn/end":
                body["finish_reason"] = _finish_reason(data)
                detail = _failure_detail(data)
                if detail:
                    body["error_detail"] = detail
            elif event_type == "tool/call":
                body.update(_tool_call(data))
            elif event_type == "tool/result":
                body.update(_tool_result(data))

    return Events.DSH_EVENT, body


def _tool_call(data: Any) -> dict[str, Any]:
    """tool/call → 时间线命令卡字段（与 codex/opencode 的归一化字段同名）。

    内置 DSH Agent 的 bash/read/write/edit 调用就靠这里进入执行轨迹；
    以前只映射 assistant 文本，导致内置 Agent 永远没有命令与文件统计。
    """
    if not isinstance(data, dict):
        return {}
    fields: dict[str, Any] = {"status": "running"}
    name = data.get("name")
    if isinstance(name, str) and name:
        fields["name"] = name
    arguments = data.get("arguments")
    if (isinstance(arguments, str) and arguments) or isinstance(arguments, (dict, list)):
        fields["detail"] = arguments
    call_id = data.get("callId")
    if isinstance(call_id, str) and call_id:
        fields["call_id"] = call_id
    return fields


def _tool_result(data: Any) -> dict[str, Any]:
    """tool/result → 同一条命令卡的收尾（输出 + 成功/失败）。"""
    if not isinstance(data, dict):
        return {}
    message = data.get("message")
    message = message if isinstance(message, dict) else {}
    call_id = data.get("callId")
    if not isinstance(call_id, str) or not call_id:
        source = message.get("source")
        if isinstance(source, dict) and isinstance(source.get("callId"), str):
            call_id = source["callId"]
    fields: dict[str, Any] = {}
    if isinstance(call_id, str) and call_id:
        fields["call_id"] = call_id
    output, is_error = _tool_output(message)
    if output:
        fields["output"] = output
    fields["status"] = "error" if is_error else "completed"
    return fields


def _tool_output(message: dict[str, Any]) -> tuple[str, bool]:
    """取工具结果的文本与是否失败（tool-result 的 content 里可能有多段文本）。"""
    content = message.get("content")
    if not isinstance(content, list):
        return "", False
    texts: list[str] = []
    is_error = False
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool-result":
            continue
        if block.get("isError") is True:
            is_error = True
        nested = block.get("content")
        if not isinstance(nested, list):
            continue
        for part in nested:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str) and text:
                    texts.append(text)
    return "".join(texts), is_error


def _assistant_text(data: Any) -> str:
    """按 SDK 同款规则取助手文本：data.message.content 或 data.content 里 type=text 的块。"""
    if not isinstance(data, dict):
        return ""
    message = data.get("message")
    content_owner = message if isinstance(message, dict) else data
    content = content_owner.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "".join(parts)


def _finish_reason(data: Any) -> str | None:
    """取 turn/end 的结束原因：data.reason.kind。"""
    reason = data.get("reason") if isinstance(data, dict) else None
    kind = reason.get("kind") if isinstance(reason, dict) else None
    return kind if isinstance(kind, str) else None


def _failure_detail(data: Any) -> str:
    """取失败原因原文：`turn/end` 的 `reason.error/failure.message`。

    额度/鉴权这类错误在运行库层面只表现为 `kind=error`，用户能据以自救的只有这句
    上游原文（如 "Insufficient Balance ... QUOTA 402"）；丢掉它任务面板就只剩
    "执行出错"四个字，无法判断是该充值还是该换模型。
    """
    reason = data.get("reason") if isinstance(data, dict) else None
    if not isinstance(reason, dict):
        return ""
    for key in ("error", "failure"):
        failure = reason.get(key)
        if not isinstance(failure, dict):
            continue
        message = failure.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return ""


__all__ = ["to_flux_event"]
