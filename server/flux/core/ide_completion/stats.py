"""IDE 行内补全的埋点统计（AI_COMPLETION_SPEC §8）。

补全链路此前是"盲"的：只知道请求发出去了，不知道首字要等多久、模型是不是经常
一个字都不给、用户到底接受了多少。而这三个信号恰恰是调优的依据（换模型 / 改预算 /
调触发阈值）。这里只做本地轻量统计，不上报、不落库、不记录任何代码内容：

- **请求侧**：请求数、成功 / 失败 / 中断、空结果、首字延迟、总耗时、字符数；
- **接受侧**：用户实际接受的次数（插件在 `InlineCompletionItem.command` 里回调）。

数据写单个 JSON（默认 `~/.flux/completion-stats.json`），进程内缓存 + 原子替换，
按天分桶只保留最近 `MAX_DAYS` 天，避免文件无限增长。读取失败一律退回空统计——
统计不能影响补全本身。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

#: 按天分桶的保留窗口（超过就丢最旧的，防止文件随使用时间线性膨胀）
MAX_DAYS = 14

_COUNTER_KEYS = (
    "requests",
    "successes",
    "failures",
    "aborts",
    "empty",
    "delivered",
    "accepted",
    "first_token_ms_total",
    "first_token_samples",
    "total_ms_total",
)


def _new_bucket() -> dict[str, int]:
    return {key: 0 for key in _COUNTER_KEYS}


def _merge_defaults(bucket: dict[str, Any]) -> dict[str, int]:
    """补齐缺失的计数键（兼容旧版本文件 / 手工编辑过的文件）。"""
    for key in _COUNTER_KEYS:
        bucket.setdefault(key, 0)
    return bucket  # type: ignore[return-value]


def _bump(bucket: dict[str, Any], **increments: int) -> None:
    _merge_defaults(bucket)
    for key, value in increments.items():
        if value:
            bucket[key] = int(bucket.get(key, 0)) + int(value)


def _public_bucket(bucket: dict[str, Any] | None) -> dict[str, Any]:
    data = _merge_defaults(dict(bucket or {}))
    delivered = data["delivered"]
    accepted = data["accepted"]
    first_samples = data["first_token_samples"]
    successes = data["successes"]
    return {
        "requests": data["requests"],
        "successes": successes,
        "failures": data["failures"],
        "aborts": data["aborts"],
        "empty": data["empty"],
        "delivered": delivered,
        "accepted": accepted,
        # 接受率以"真正展示了非空补全"为分母，而不是请求数——刷新/去抖会重发请求，
        # 用请求数做分母会把接受率系统性压低。
        "acceptance_rate": round(accepted / delivered, 4) if delivered else 0.0,
        "avg_first_token_ms": (
            round(data["first_token_ms_total"] / first_samples) if first_samples else None
        ),
        "avg_total_ms": round(data["total_ms_total"] / successes) if successes else None,
    }


class CompletionStatsStore:
    """补全埋点的单文件存储（线程安全、原子写、按天分桶）。"""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None

    @property
    def path(self) -> Path:
        return self._path

    # --- 记录 ---

    def record_request(self, *, mode: str = "", model: str = "") -> None:
        """一次补全请求开始（含后续失败 / 中断）。"""

        def apply(data: dict[str, Any]) -> None:
            for bucket in self._scopes(data, mode=mode, model=model):
                _bump(bucket, requests=1)

        self._mutate(apply)

    def record_result(
        self,
        *,
        mode: str = "",
        model: str = "",
        first_token_ms: int | None = None,
        total_ms: int | None = None,
        chars: int = 0,
        empty: bool = False,
    ) -> None:
        """一次补全成功结束（有 `done` 事件）。"""

        increments: dict[str, int] = {
            "successes": 1,
            "total_ms_total": max(0, int(total_ms or 0)),
        }
        if first_token_ms is not None:
            increments["first_token_ms_total"] = max(0, int(first_token_ms))
            increments["first_token_samples"] = 1
        if empty or chars <= 0:
            increments["empty"] = 1
        else:
            increments["delivered"] = 1

        def apply(data: dict[str, Any]) -> None:
            for bucket in self._scopes(data, mode=mode, model=model):
                _bump(bucket, **increments)
            data["last"] = {
                "at": _now_iso(),
                "ok": True,
                "mode": mode or None,
                "model": model or None,
                "first_token_ms": first_token_ms,
                "total_ms": total_ms,
                "chars": chars,
            }

        self._mutate(apply)

    def record_failure(self, code: str = "", *, mode: str = "", model: str = "") -> None:
        """一次补全以 `error` 事件结束。"""

        def apply(data: dict[str, Any]) -> None:
            for bucket in self._scopes(data, mode=mode, model=model):
                _bump(bucket, failures=1)
            data["last"] = {
                "at": _now_iso(),
                "ok": False,
                "mode": mode or None,
                "model": model or None,
                "code": code or None,
            }

        self._mutate(apply)

    def record_abort(self, *, mode: str = "", model: str = "") -> None:
        """客户端提前断开（编辑器里换了位置 / 继续敲字，旧流被主动取消）。"""

        def apply(data: dict[str, Any]) -> None:
            for bucket in self._scopes(data, mode=mode, model=model):
                _bump(bucket, aborts=1)

        self._mutate(apply)

    def record_accept(
        self,
        *,
        accepted: bool,
        mode: str = "",
        model: str = "",
        language_id: str = "",
        completion_mode: str = "",
    ) -> None:
        """用户接受（或明确拒绝）了一条补全。"""

        def apply(data: dict[str, Any]) -> None:
            for bucket in self._scopes(data, mode=mode, model=model):
                _bump(bucket, accepted=1 if accepted else 0)
            data["last_accept"] = {
                "at": _now_iso(),
                "accepted": bool(accepted),
                "mode": mode or None,
                "model": model or None,
                "language_id": language_id or None,
                "completion_mode": completion_mode or None,
            }

        self._mutate(apply)

    # --- 读取 ---

    def snapshot(self) -> dict[str, Any]:
        """汇总后的统计（GET 接口用）。"""

        with self._lock:
            data = self._load()
            days = data.get("days") if isinstance(data.get("days"), dict) else {}
            today = date.today().isoformat()
            return {
                "totals": _public_bucket(data.get("totals")),
                "today": _public_bucket(days.get(today)),
                "by_model": {
                    str(key): _public_bucket(value)
                    for key, value in (data.get("by_model") or {}).items()
                    if isinstance(value, dict)
                },
                "by_mode": {
                    str(key): _public_bucket(value)
                    for key, value in (data.get("by_mode") or {}).items()
                    if isinstance(value, dict)
                },
                "days": [
                    {"date": str(key), **_public_bucket(value)}
                    for key, value in sorted(days.items(), reverse=True)[:MAX_DAYS]
                    if isinstance(value, dict)
                ],
                "last": data.get("last"),
                "last_accept": data.get("last_accept"),
                "updated_at": data.get("updated_at"),
            }

    # --- 内部 ---

    def _scopes(
        self, data: dict[str, Any], *, mode: str = "", model: str = ""
    ) -> list[dict[str, Any]]:
        """一次写入要同时更新的所有计数桶：总计、今天、按模型、按模式。"""
        buckets = [
            _merge_defaults(data.setdefault("totals", {})),
            _merge_defaults(self._day(data)),
        ]
        if model:
            by_model = data.setdefault("by_model", {})
            buckets.append(_merge_defaults(by_model.setdefault(model, {})))
        if mode:
            by_mode = data.setdefault("by_mode", {})
            buckets.append(_merge_defaults(by_mode.setdefault(mode, {})))
        return buckets

    def _day(self, data: dict[str, Any]) -> dict[str, Any]:
        days = data.setdefault("days", {})
        today = date.today().isoformat()
        bucket = days.setdefault(today, {})
        # 只保留最近 MAX_DAYS 天：按 ISO 日期字符串排序即可
        for stale in sorted(days)[:-MAX_DAYS]:
            days.pop(stale, None)
        return bucket

    def _mutate(self, apply: Any) -> None:
        with self._lock:
            data = self._load()
            apply(data)
            data["updated_at"] = _now_iso()
            self._write(data)

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            self._data = self._read()
        return self._data

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._path.with_name(f"{self._path.name}.tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self._path)
        except OSError:
            # 统计写不进去（只读盘 / 权限）不能影响补全主链路，静默降级
            pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = ["MAX_DAYS", "CompletionStatsStore"]
