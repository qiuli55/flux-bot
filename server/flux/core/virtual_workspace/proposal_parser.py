"""提案入参校验器：把 MCP `proposal.create` 的入参解析成 `CodeChangeSet`。

提案一律由 agent 经 MCP 提交——Flux 不组装 prompt、不解析模型对话（目标架构 §1），
本模块只做平台侧把关：结构合法、路径不越界、无重复路径、内容必须是完整文件。
任何不合契约的输入都明确报错，不静默兜底。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from flux.core.virtual_workspace.path_guard import (
    ensure_not_flux_internal,
    ensure_not_git_internal,
    ensure_not_secret_path,
)
from flux.errors import ValidationError

_FENCED_BLOCK = re.compile(r"```[a-zA-Z0-9_+-]*[ \t]*\r?\n(.*?)```", re.DOTALL)
_PREVIEW_CHARS = 300

#: 一条改动的动作类型（缺省时由服务层按"原文件是否存在"自动判定）
_OPERATIONS = ("create", "modify", "delete")


@dataclass(frozen=True)
class FileChange:
    """一个文件的改动提案：path 为相对项目根的 POSIX 路径。

    `op` 是 create / modify / delete，缺省为 None（自动判定）；`content` 为改动后的完整
    内容，delete 时必须是 None。
    """

    path: str
    content: str | None
    reason: str = ""
    op: str | None = None

    def to_dict(self) -> dict[str, Any]:
        # 不带 op 的既有形态保持原样（历史契约）；显式 op 才附上
        payload: dict[str, Any] = {
            "path": self.path,
            "content": self.content,
            "reason": self.reason,
        }
        if self.op is not None:
            payload["op"] = self.op
        return payload


@dataclass(frozen=True)
class CodeChangeSet:
    """一次需求对应的一组文件改动（提案草稿，尚未落库为 Proposal）。"""

    summary: str
    changes: tuple[FileChange, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(change.path for change in self.changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "changes": [change.to_dict() for change in self.changes],
        }


def parse_code_change_set(raw: str, *, source: str = "proposal") -> CodeChangeSet:
    """把提案文本解析成 CodeChangeSet；source 是提案来源标注，进错误详情供审计。"""
    payload = _decode_payload(raw, source=source)
    if not isinstance(payload, Mapping):
        raise _invalid(source, "提案顶层必须是 JSON 对象", raw)

    summary = payload.get("summary", "")
    if not isinstance(summary, str):
        raise _invalid(source, "summary 必须是字符串", raw)

    raw_changes = payload.get("changes")
    if not isinstance(raw_changes, list) or not raw_changes:
        raise _invalid(source, "changes 必须是非空数组", raw)

    changes: list[FileChange] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_changes):
        if not isinstance(item, Mapping):
            raise _invalid(source, f"changes[{index}] 必须是对象", raw)
        path = _require_path(item.get("path"), index, source, raw)
        if path in seen:
            raise _invalid(source, f"changes 中出现重复路径：{path}", raw)
        seen.add(path)
        op = item.get("op")
        if op is not None and (not isinstance(op, str) or op not in _OPERATIONS):
            raise _invalid(
                source,
                f"changes[{index}].op 必须是 {'/'.join(_OPERATIONS)} 之一",
                raw,
            )
        reason = item.get("reason", "")
        if not isinstance(reason, str):
            raise _invalid(source, f"changes[{index}].reason 必须是字符串", raw)
        if op == "delete":
            # 删除语义下没有"改动后内容"：带了 content 的输入是自相矛盾的，直接拒绝
            if "content" in item:
                raise _invalid(source, f"changes[{index}].op=delete 时禁止携带 content", raw)
            content: str | None = None
        else:
            content = item.get("content")
            if not isinstance(content, str):
                raise _invalid(source, f"changes[{index}].content 必须是字符串", raw)
        changes.append(FileChange(path=path, content=content, reason=reason.strip(), op=op))

    return CodeChangeSet(summary=summary.strip(), changes=tuple(changes))


# --- 内部：容错解析 ---


def _decode_payload(raw: str, *, source: str) -> Any:
    """入参可能是纯 JSON、```json 围栏、或夹带说明文字，这里逐种尝试。"""
    text = raw.strip()
    candidates: list[str] = []
    if text.startswith(("{", "[")):
        candidates.append(text)
    candidates.extend(block.strip() for block in _FENCED_BLOCK.findall(raw))
    starts = [index for index in (text.find("{"), text.find("[")) if index != -1]
    end = max(text.rfind("}"), text.rfind("]"))
    if starts and end > min(starts):
        candidates.append(text[min(starts) : end + 1])

    tried: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in tried:
            continue
        tried.add(candidate)
        try:
            # strict=False：模型常在字符串里直接塞换行/制表符，这种"非法 JSON"语义明确，
            # 宽松解析能接受；结构性问题仍会落到下面的 ValidationError。
            return json.loads(candidate, strict=False)
        except json.JSONDecodeError:
            continue
    raise ValidationError(
        "提案不是合法的 JSON",
        details={"source": source, "raw_preview": _preview(raw)},
    )


def _require_path(raw: Any, index: int, source: str, full: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise _invalid(source, f"changes[{index}].path 必须是非空字符串", full)
    candidate = raw.strip().replace("\\", "/")
    pure = PurePosixPath(candidate)
    if candidate.startswith("/") or pure.is_absolute():
        raise _invalid(source, f"changes[{index}].path 不得是绝对路径：{raw}", full)
    if ".." in pure.parts:
        raise _invalid(source, f"changes[{index}].path 不得越出项目根：{raw}", full)
    if not pure.parts:
        raise _invalid(source, f"changes[{index}].path 非法：{raw}", full)
    # 归一化 a/./b、a//b 之类的写法，保证同一次提案里路径唯一可比
    normalized = str(pure)
    # 密钥类文件与 Flux 内部目录（备份/锁）永不接受提案：前者是硬禁令（§3.5），
    # 后者一旦被改就等于抽掉 Apply Engine 的回滚依据
    ensure_not_secret_path(normalized)
    ensure_not_flux_internal(pure)
    ensure_not_git_internal(pure)
    return normalized


def _invalid(source: str, message: str, full: str) -> ValidationError:
    return ValidationError(
        f"提案非法（{source}）：{message}",
        details={"raw_preview": _preview(full)},
    )


def _preview(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "…"
