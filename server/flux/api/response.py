"""统一响应体（主规格 §12.10 裁决 A6：success / code / message / data / metadata）。"""

from __future__ import annotations

from typing import Any


def ok(data: Any = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "success": True,
        "code": "ok",
        "message": "",
        "data": data,
        "metadata": metadata or {},
    }


def fail(
    code: str,
    message: str,
    *,
    details: Any = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": False,
        "code": code,
        "message": message,
        "data": details,
        "metadata": metadata or {},
    }
