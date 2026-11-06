"""IDE 行内补全偏好与当前 Agent 选择的本地落盘（AI_COMPLETION_SPEC §5.3 / §7）。

两类数据写在同一个 JSON 文件里：

```json
{
  "completion": {"enabled": true, "model": "follow-agent", "debounce_ms": 400, "max_lines": 8},
  "active_agent": {"agent_id": null, "model": null, "key_id": null}
}
```

为什么不用数据库：这是"每台开发机一套"的编辑器偏好，跟着 IDE 走而不是跟着账号走；
单文件 + 原子替换足够，且后端重启后仍然生效。读取失败（文件损坏 / 权限）一律退回
默认值——补全不能因为一份坏掉的偏好文件而整体不可用。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

#: 跟随当前 Agent 的模型选择（默认）
FOLLOW_AGENT = "follow-agent"

DEFAULT_DEBOUNCE_MS = 400
DEFAULT_MAX_LINES = 8

MIN_DEBOUNCE_MS = 100
MAX_DEBOUNCE_MS = 2000
MIN_MAX_LINES = 1
MAX_MAX_LINES = 64


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _clean_optional_str(value: Any, *, max_length: int = 128) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:max_length] if text else None


@dataclass(frozen=True, slots=True)
class CompletionPrefs:
    """行内补全偏好。"""

    enabled: bool = True
    model: str = FOLLOW_AGENT
    debounce_ms: int = DEFAULT_DEBOUNCE_MS
    max_lines: int = DEFAULT_MAX_LINES

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "model": self.model,
            "debounce_ms": self.debounce_ms,
            "max_lines": self.max_lines,
        }

    @classmethod
    def from_mapping(cls, raw: Any) -> CompletionPrefs:
        if not isinstance(raw, dict):
            return cls()
        model = _clean_optional_str(raw.get("model")) or FOLLOW_AGENT
        return cls(
            enabled=bool(raw.get("enabled", True)),
            model=model,
            debounce_ms=_clamp_int(
                raw.get("debounce_ms"),
                default=DEFAULT_DEBOUNCE_MS,
                minimum=MIN_DEBOUNCE_MS,
                maximum=MAX_DEBOUNCE_MS,
            ),
            max_lines=_clamp_int(
                raw.get("max_lines"),
                default=DEFAULT_MAX_LINES,
                minimum=MIN_MAX_LINES,
                maximum=MAX_MAX_LINES,
            ),
        )

    def merged(self, **changes: Any) -> CompletionPrefs:
        return replace(self, **{k: v for k, v in changes.items() if v is not None})


@dataclass(frozen=True, slots=True)
class ActiveAgentSelection:
    """当前生效的 Agent 及其模型覆盖（`follow-agent` 的解析依据）。"""

    agent_id: str | None = None
    model: str | None = None
    key_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"agent_id": self.agent_id, "model": self.model, "key_id": self.key_id}

    @classmethod
    def from_mapping(cls, raw: Any) -> ActiveAgentSelection:
        if not isinstance(raw, dict):
            return cls()
        return cls(
            agent_id=_clean_optional_str(raw.get("agent_id"), max_length=64),
            model=_clean_optional_str(raw.get("model"), max_length=128),
            key_id=_clean_optional_str(raw.get("key_id"), max_length=64),
        )

    @property
    def is_empty(self) -> bool:
        return not (self.agent_id or self.model or self.key_id)


class CompletionPrefsStore:
    """补全偏好 + 当前 Agent 选择的单文件存储（线程安全、原子写）。"""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    # --- 读取 ---

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def prefs(self) -> CompletionPrefs:
        return CompletionPrefs.from_mapping(self._read().get("completion"))

    def active_agent(self) -> ActiveAgentSelection:
        return ActiveAgentSelection.from_mapping(self._read().get("active_agent"))

    # --- 写入 ---

    def _write(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子替换：进程崩溃时不会留下半截 JSON
        temp = self._path.with_name(f"{self._path.name}.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self._path)

    def set_prefs(self, prefs: CompletionPrefs) -> CompletionPrefs:
        with self._lock:
            current = self._read()
            current["completion"] = prefs.to_dict()
            self._write(current)
        return prefs

    def update_prefs(
        self,
        *,
        enabled: bool | None = None,
        model: str | None = None,
        debounce_ms: int | None = None,
        max_lines: int | None = None,
    ) -> CompletionPrefs:
        """按字段更新；`None` 表示保持原值。"""
        with self._lock:
            current = self._read()
            merged = CompletionPrefs.from_mapping(current.get("completion")).merged(
                enabled=enabled,
                model=_clean_optional_str(model),
                debounce_ms=debounce_ms,
                max_lines=max_lines,
            )
            current["completion"] = merged.to_dict()
            self._write(current)
        return merged

    def set_active_agent(self, selection: ActiveAgentSelection) -> ActiveAgentSelection:
        with self._lock:
            current = self._read()
            current["active_agent"] = selection.to_dict()
            self._write(current)
        return selection

    def snapshot(self) -> dict[str, Any]:
        """一次性返回两类数据（GET 接口用）。"""
        data = self._read()
        return {
            **CompletionPrefs.from_mapping(data.get("completion")).to_dict(),
            "active_agent": ActiveAgentSelection.from_mapping(data.get("active_agent")).to_dict(),
        }


__all__ = [
    "DEFAULT_DEBOUNCE_MS",
    "DEFAULT_MAX_LINES",
    "FOLLOW_AGENT",
    "MAX_DEBOUNCE_MS",
    "MAX_MAX_LINES",
    "MIN_DEBOUNCE_MS",
    "MIN_MAX_LINES",
    "ActiveAgentSelection",
    "CompletionPrefs",
    "CompletionPrefsStore",
]
