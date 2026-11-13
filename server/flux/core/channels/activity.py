"""频道运行态（进程内）：最近接收时间与最近错误。

跨重启的持久真相源是 `outbound_drafts`：它能回答「最近一次成功发送」和「最近一次
发送失败」。入站没有对应的频道字段（`bot_turns` 只记 bot_id / client_turn_id），
所以这里用一个轻量的进程内登记簿补上「最近接收时间」与「入站错误」。

约定：

- 不落库、进程重启即清空（状态接口会同时暴露这个边界）；
- 只保存时间与短错误串，**不保存**消息正文、令牌、回调密文或请求体；
- 错误串会压平换行并截断，避免把整段上游响应带进状态接口。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

__all__ = [
    "ChannelActivityRegistry",
    "ChannelErrorRecord",
    "sanitize_error",
]

#: 单个频道最多保留的错误条数（进程内，足够运维看近期问题）
MAX_ERRORS_PER_CHANNEL = 20
#: 错误串截断长度：与出站草稿的落库口径一致，只留异常类型 + 短消息
MAX_ERROR_CHARS = 200
MAX_CODE_CHARS = 64


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """统一成带时区的 UTC，避免进程内记录与数据库读回的时间混排时抛 TypeError。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def sanitize_error(message: object) -> str:
    """把异常 / 上游响应折叠成安全的短错误串。

    去掉换行与多余空白并截断；调用方只传异常类型与短消息，绝不传请求体或密文。
    """
    text = (
        f"{type(message).__name__}: {message}"
        if isinstance(message, BaseException)
        else str(message or "")
    )
    flattened = " ".join(text.split())
    return flattened[:MAX_ERROR_CHARS]


@dataclass(frozen=True)
class ChannelErrorRecord:
    """一条进程内记录的频道错误。"""

    at: datetime
    code: str
    message: str
    source: str = "inbound"


@dataclass
class _ChannelState:
    last_receive_at: datetime | None = None
    errors: deque[ChannelErrorRecord] = field(
        default_factory=lambda: deque(maxlen=MAX_ERRORS_PER_CHANNEL)
    )


class ChannelActivityRegistry:
    """频道入站活动与错误的进程内登记簿。

    频道回调可能在任意线程 / 事件循环里写入，状态接口在另一个协程里读取，
    因此用一把小锁保护字典与 deque。只在极短临界区内持锁，不做 I/O。
    """

    def __init__(self, *, max_errors: int = MAX_ERRORS_PER_CHANNEL) -> None:
        self._max_errors = max(1, int(max_errors))
        self._states: dict[str, _ChannelState] = {}
        self._lock = threading.Lock()

    def _state(self, channel: str) -> _ChannelState:
        state = self._states.get(channel)
        if state is None:
            state = _ChannelState(errors=deque(maxlen=self._max_errors))
            self._states[channel] = state
        return state

    def record_receive(self, channel: str, *, at: datetime | None = None) -> None:
        """记一次成功入站（同一频道的较旧时间不会覆盖较新时间）。"""
        name = (channel or "").strip()
        if not name:
            return
        moment = _aware(at or _utcnow())
        with self._lock:
            state = self._state(name)
            if state.last_receive_at is None or moment >= state.last_receive_at:
                state.last_receive_at = moment

    def record_error(
        self,
        channel: str,
        code: str,
        message: object,
        *,
        at: datetime | None = None,
        source: str = "inbound",
    ) -> None:
        """记一条入站错误；错误串先压平截断，绝不落消息正文或令牌。"""
        name = (channel or "").strip()
        if not name:
            return
        record = ChannelErrorRecord(
            at=_aware(at or _utcnow()),
            code=(str(code or "error").strip() or "error")[:MAX_CODE_CHARS],
            message=sanitize_error(message),
            source=(source or "inbound")[:32],
        )
        with self._lock:
            self._state(name).errors.append(record)

    def last_receive_at(self, channel: str) -> datetime | None:
        with self._lock:
            state = self._states.get((channel or "").strip())
            return state.last_receive_at if state is not None else None

    def recent_errors(self, channel: str, *, limit: int = 5) -> list[ChannelErrorRecord]:
        """最近错误，最新在前（进程内列表本身按写入顺序追加）。"""
        with self._lock:
            state = self._states.get((channel or "").strip())
            records = list(state.errors) if state is not None else []
        records.sort(key=lambda item: item.at, reverse=True)
        return records[: max(0, limit)]

    def reset(self) -> None:
        """清空登记簿（测试或长生命周期服务重置用）。"""
        with self._lock:
            self._states.clear()
