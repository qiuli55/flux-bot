"""用户记忆的自动记录开关（设备侧，落盘 JSON）。

只保存一个布尔值：关闭后不再从任务对话里自动提炼记忆，用户手动添加与账号云
同步都不受影响。用文件而不是数据库，是因为它属于「设备偏好」，不随账号漫游。
"""

from __future__ import annotations

import json
from pathlib import Path

from flux.logging import get_logger

logger = get_logger(__name__)


class MemoryPrefsStore:
    def __init__(self, path: Path, *, default_auto_capture: bool = True) -> None:
        self._path = path
        self._default = default_auto_capture

    def auto_capture(self) -> bool:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._default
        except (OSError, ValueError):
            logger.warning("读取用户记忆偏好失败：%s", self._path)
            return self._default
        value = data.get("auto_capture") if isinstance(data, dict) else None
        return bool(value) if isinstance(value, bool) else self._default

    def set_auto_capture(self, enabled: bool) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        current = {}
        try:
            current = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        current["auto_capture"] = bool(enabled)
        self._path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        return bool(enabled)


__all__ = ["MemoryPrefsStore"]
