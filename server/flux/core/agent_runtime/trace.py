"""Agent Run 执行轨迹的分类、统计与记录。

思考与工具事件在 SSE 里是增量，无法原样落库（一个 Run 可能几千帧）。这里做两件事：

1. **记录**：把增量合并成可持久化的轨迹项——同一段思考连续累加，同一 call_id 的
   工具事件合并成一条命令卡（codex 的 started/completed、opencode 的 running/error
   都是同一次调用）；
2. **统计**：工具按名称归类为 command / read / write / other，并从入参里取文件路径，
   供「概览」显示改动文件数、运行命令数、读取文件数。

口径（与产品确认过的默认口径一致）：

- 命令：bash / shell / exec 等命令类工具调用次数；
- 读取：read / view 类工具，按文件路径去重（shell 里的 `cat` 不解析，不冒充 read）；
- 改动：write / edit / apply_patch / file_change 等写类工具，按文件路径去重；
- 取不到路径时用 `call:<call_id>` 兜底，保证「一次文件操作」至少被计一次。
"""

from __future__ import annotations

import json
import re
from typing import Any

from flux.core.agent_runtime.trace_repository import RunTraceRepository
from flux.logging import get_logger

logger = get_logger(__name__)

#: 单个 Run 保留的轨迹项上限：防止一个超长 Run 把内存与库写爆
MAX_ITEMS = 400
#: 单段思考累计字符上限（多帧增量拼接后的总量）
MAX_THINKING_CHARS = 24000
#: 单次工具调用最多记录的路径数
MAX_PATHS = 20
#: 路径字段长度上限
PATH_LIMIT = 400

#: 命令类工具（执行 shell / 命令）
COMMAND_TOOLS = frozenset(
    {
        "bash",
        "shell",
        "sh",
        "command",
        "command_execution",
        "exec",
        "execute",
        "local_shell",
        "run_command",
        "terminal",
        "cmd",
        "powershell",
    }
)
#: 读取类工具
READ_TOOLS = frozenset(
    {
        "read",
        "read_file",
        "readfile",
        "read_text_file",
        "view",
        "view_file",
        "open_file",
        "cat",
        "get_file",
    }
)
#: 写入 / 改动类工具
WRITE_TOOLS = frozenset(
    {
        "write",
        "write_file",
        "writefile",
        "create_file",
        "edit",
        "edit_file",
        "multi_edit",
        "apply_patch",
        "patch",
        "file_change",
        "delete_file",
        "move_file",
        "rename_file",
        "str_replace",
        "replace_in_file",
        "notebook_edit",
    }
)

#: 入参里常见的路径键
_PATH_KEYS = (
    "file_path",
    "filePath",
    "path",
    "target_file",
    "targetFile",
    "filename",
    "file",
    "absolute_path",
    "abs_path",
    "notebook_path",
)
#: apply_patch 文本里的文件标记（*** Update File: xxx / *** Add File: xxx / ...）
_PATCH_MARKERS = ("*** Update File: ", "*** Add File: ", "*** Delete File: ", "*** Move to: ")


#: 名称里的语义词元：MCP 工具名形如 `mcp__flux__workspace_read_many_<hash>`，
#: 精确匹配认不出来，按词元兜底（hash 段不会与这些词元相等，不会误判）
_COMMAND_TOKENS = frozenset(
    {"bash", "shell", "sh", "exec", "execute", "command", "terminal", "cmd"}
)
_READ_TOKENS = frozenset({"read", "readfile", "view", "cat", "open"})
_WRITE_TOKENS = frozenset(
    {
        "write",
        "writefile",
        "edit",
        "patch",
        "apply",
        "create",
        "delete",
        "move",
        "rename",
        "replace",
        "file_change",
    }
)


def _normalize_name(name: str) -> str:
    lowered = name.strip().lower()
    # 带命名空间的工具名（functions.read / mcp__flux__workspace_read）按最后一段归类
    if "." in lowered:
        lowered = lowered.rsplit(".", 1)[-1]
    return lowered


def _name_tokens(lowered: str) -> set[str]:
    return {part for part in re.split(r"[._\-\s]+", lowered) if part}


def classify_tool(name: str, detail: str) -> str:
    """工具名 → command / read / write / other。"""
    lowered = _normalize_name(name)
    if lowered in COMMAND_TOOLS:
        return "command"
    if lowered in READ_TOOLS:
        return "read"
    if lowered in WRITE_TOOLS:
        return "write"
    tokens = _name_tokens(lowered)
    if tokens & _COMMAND_TOKENS:
        return "command"
    if tokens & _READ_TOKENS:
        return "read"
    if tokens & _WRITE_TOKENS:
        return "write"
    return "other"


def extract_paths(detail: str) -> list[str]:
    """从工具入参里取文件路径；detail 可能是 JSON 字符串、紧凑 JSON 或补丁文本。"""
    text = detail.strip()
    if not text:
        return []
    found: list[str] = []
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        _collect_paths(parsed, found)
    else:
        for marker in _PATCH_MARKERS:
            start = 0
            while True:
                index = text.find(marker, start)
                if index < 0:
                    break
                line = text[index + len(marker) :].splitlines()[0].strip()
                if line:
                    found.append(line)
                start = index + len(marker)
    return _dedupe_paths(found)


def _collect_paths(value: Any, found: list[str]) -> None:
    if isinstance(value, dict):
        for key in _PATH_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                found.append(candidate.strip())
        for key in ("files", "changes", "edits", "file_paths", "paths"):
            nested = value.get(key)
            if nested is not None:
                _collect_paths(nested, found)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                found.append(item.strip())
            elif isinstance(item, (dict, list)):
                _collect_paths(item, found)


def _dedupe_paths(paths: list[str]) -> list[str]:
    seen: list[str] = []
    for path in paths:
        clipped = path[:PATH_LIMIT]
        if clipped not in seen:
            seen.append(clipped)
        if len(seen) >= MAX_PATHS:
            break
    return seen


def summarize_trace(rows: list[dict[str, Any]]) -> dict[str, int]:
    """轻量轨迹行（run_id / kind / category / paths / call_id）→ 概览计数。"""
    commands = 0
    read_paths: set[str] = set()
    write_paths: set[str] = set()
    for row in rows:
        category = str(row.get("category") or "")
        if category == "command":
            commands += 1
            continue
        if category not in ("read", "write"):
            continue
        target = read_paths if category == "read" else write_paths
        paths = row.get("paths")
        if isinstance(paths, list) and paths:
            target.update(str(path) for path in paths)
        else:
            target.add(f"call:{row.get('call_id') or row.get('seq') or row.get('run_id')}")
    return {
        "commands": commands,
        "files_read": len(read_paths),
        "files_changed": len(write_paths),
    }


class RunTraceRecorder:
    """把运行事件累积成轨迹项，Run 终态时一次性落库。

    崩溃时最后一段轨迹会丢——轨迹是过程展示，不是状态权威，可接受；
    任务状态与最终答复仍由 `task_messages` 保证。
    """

    def __init__(self, repository: RunTraceRepository, *, max_items: int = MAX_ITEMS) -> None:
        self._repository = repository
        self._max_items = max_items
        self._items: dict[str, list[dict[str, Any]]] = {}
        self._task_ids: dict[str, str | None] = {}
        #: 当前仍然「打开」的思考项下标：后续增量继续拼到它上面
        self._open_thinking: dict[str, int] = {}

    def record(self, run_id: str, task_id: str | None, payload: dict[str, Any]) -> None:
        """记录一条已归一化的运行事件载荷（kind + 字段）。"""
        if not run_id:
            return
        kind = str(payload.get("kind") or "")
        items = self._items.setdefault(run_id, [])
        self._task_ids[run_id] = task_id
        if kind == "thinking":
            self._record_thinking(run_id, items, str(payload.get("text") or ""))
        elif kind == "tool":
            self._record_tool(run_id, items, payload)
        elif kind == "error":
            self._close_thinking(run_id)
            text = str(payload.get("text") or "").strip()
            if text and len(items) < self._max_items:
                items.append({"kind": "error", "text": text})

    def _record_thinking(self, run_id: str, items: list[dict[str, Any]], text: str) -> None:
        if not text.strip():
            # 空增量只是「思考中」刻度，历史里没有内容就不占一行
            return
        index = self._open_thinking.get(run_id)
        if index is not None and index < len(items) and items[index].get("kind") == "thinking":
            merged = str(items[index].get("text") or "") + text
            items[index]["text"] = merged[:MAX_THINKING_CHARS]
            return
        if len(items) >= self._max_items:
            return
        items.append({"kind": "thinking", "text": text[:MAX_THINKING_CHARS]})
        self._open_thinking[run_id] = len(items) - 1

    def _record_tool(
        self, run_id: str, items: list[dict[str, Any]], payload: dict[str, Any]
    ) -> None:
        self._close_thinking(run_id)
        name = str(payload.get("name") or "").strip()
        detail = str(payload.get("detail") or "").strip()
        output = str(payload.get("output") or "")
        status = str(payload.get("status") or "").strip()
        call_id = str(payload.get("call_id") or "").strip()
        if call_id:
            for existing in items:
                if existing.get("kind") == "tool" and existing.get("call_id") == call_id:
                    _merge_tool(existing, name=name, detail=detail, output=output, status=status)
                    return
        if len(items) >= self._max_items:
            return
        item: dict[str, Any] = {"kind": "tool", "call_id": call_id}
        _merge_tool(item, name=name, detail=detail, output=output, status=status)
        items.append(item)

    def _close_thinking(self, run_id: str) -> None:
        self._open_thinking.pop(run_id, None)

    async def flush(self, run_id: str) -> None:
        """Run 终态：把累积项写入轨迹表（失败只记日志，不影响任务收尾）。"""
        items = self._items.pop(run_id, [])
        task_id = self._task_ids.pop(run_id, None)
        self._open_thinking.pop(run_id, None)
        if not items:
            return
        try:
            await self._repository.replace(run_id, task_id, items)
        except Exception:  # noqa: BLE001 - 轨迹落库失败不能连坐任务状态与最终答复
            logger.exception("执行轨迹落库失败 run=%s items=%d", run_id, len(items))

    def buffered(self, run_id: str) -> int:
        return len(self._items.get(run_id, ()))


def _merge_tool(item: dict[str, Any], *, name: str, detail: str, output: str, status: str) -> None:
    """把一次工具调用的最新字段合并进轨迹项（后到的完成事件补齐输出与状态）。"""
    if name:
        item["name"] = name
    if detail:
        item["detail"] = detail
        item["paths"] = extract_paths(detail)
        item["category"] = classify_tool(str(item.get("name") or ""), detail)
    elif "category" not in item:
        item["category"] = classify_tool(str(item.get("name") or ""), "")
    if output:
        item["output"] = output
    if status:
        item["status"] = status


__all__ = [
    "COMMAND_TOOLS",
    "MAX_ITEMS",
    "READ_TOOLS",
    "WRITE_TOOLS",
    "RunTraceRecorder",
    "classify_tool",
    "extract_paths",
    "summarize_trace",
]
