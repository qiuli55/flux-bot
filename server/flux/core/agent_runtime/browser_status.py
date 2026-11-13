"""浏览器 MCP（Playwright sidecar）只读健康探测。

`FLUX_CLAUDE_EXTRA_MCP_SERVERS` 里名为 ``browser`` 的 http / sse server 是
浏览器自动化的扩展点（见 docs/browser-agent.md）。运维需要一个「现在到底通不通」
的接口，但不能因为轮询把 sidecar 打爆，所以这里：

- 探活只是一次 GET，超时 2 秒；
- 成功与失败都缓存 30 秒（同一个 URL 在窗口内只打一次）；
- 只要拿到 HTTP 响应就算「可达」——MCP 的 streamable HTTP 端点对 GET 常回
  405 / 406，这也说明进程活着；真正的 JSON-RPC 会话仍由 Agent 自己建立；
- 回包与日志只带 URL、状态码、耗时与错误类别，**绝不带 headers 的值**
  （配置里的 Authorization 可能含令牌）。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from flux.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "BROWSER_SERVER_NAME",
    "PROBE_CACHE_TTL_SECONDS",
    "PROBE_TIMEOUT_SECONDS",
    "BrowserStatusProbe",
    "ProbeOutcome",
    "ProbeReport",
    "safe_url",
]

#: 扩展 MCP 配置里浏览器 server 的固定名字
BROWSER_SERVER_NAME = "browser"
#: 单次探活超时（秒）
PROBE_TIMEOUT_SECONDS = 2.0
#: 探活结果缓存时间（秒）；成功与失败同等对待
PROBE_CACHE_TTL_SECONDS = 30.0

Prober = Callable[[str, dict[str, str]], Awaitable["ProbeOutcome"]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ProbeOutcome:
    """一次探活的结果（不含请求 headers 或响应正文）。"""

    ok: bool
    latency_ms: int = 0
    status_code: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class ProbeReport:
    """带缓存元信息的探活报告。"""

    outcome: ProbeOutcome
    cached: bool
    cache_age_seconds: float
    checked_at: datetime


def _classify_error(exc: BaseException) -> str:
    """只报错误类别，不回显异常原文（可能带 URL / headers）。"""
    if isinstance(exc, httpx.TimeoutException):
        return f"timeout after {PROBE_TIMEOUT_SECONDS:g}s"
    if isinstance(exc, httpx.ConnectError):
        return "connection failed"
    if isinstance(exc, httpx.HTTPError):
        return "http error"
    return f"probe error ({type(exc).__name__})"


async def _http_probe(url: str, headers: dict[str, str]) -> ProbeOutcome:
    """默认探活实现：一次带 2 秒超时的 GET。"""
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=PROBE_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            response = await client.get(url, headers=headers)
    except httpx.TimeoutException as exc:
        return ProbeOutcome(
            ok=False,
            latency_ms=_elapsed_ms(started),
            error=_classify_error(exc),
        )
    except httpx.HTTPError as exc:
        return ProbeOutcome(
            ok=False,
            latency_ms=_elapsed_ms(started),
            error=_classify_error(exc),
        )
    except Exception as exc:  # noqa: BLE001 - 探活绝不能让状态接口 500
        logger.warning("浏览器 MCP 探活异常 type=%s", type(exc).__name__)
        return ProbeOutcome(
            ok=False,
            latency_ms=_elapsed_ms(started),
            error=_classify_error(exc),
        )
    # 有 HTTP 响应即视为进程可达；4xx（例如 GET 不被 MCP 端点接受）仍说明 sidecar 活着。
    return ProbeOutcome(
        ok=True,
        latency_ms=_elapsed_ms(started),
        status_code=response.status_code,
    )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


@dataclass
class _CacheEntry:
    stored_at: float
    checked_at: datetime
    outcome: ProbeOutcome


class BrowserStatusProbe:
    """带 30 秒缓存的浏览器 MCP 探活器（进程内单例，注入到 Container）。"""

    def __init__(
        self,
        *,
        prober: Prober | None = None,
        timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
        cache_ttl_seconds: float = PROBE_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._prober = prober or _http_probe
        self._timeout_seconds = max(0.1, float(timeout_seconds))
        self._cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        self._clock = clock
        self._now = now
        self._cache: dict[str, _CacheEntry] = {}

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def cache_ttl_seconds(self) -> float:
        return self._cache_ttl_seconds

    def clear_cache(self) -> None:
        self._cache.clear()

    async def probe(self, url: str, headers: Mapping[str, str] | None = None) -> ProbeReport:
        """探活一个 URL；窗口内命中缓存时不发网络请求。"""
        key = url
        now = self._clock()
        entry = self._cache.get(key)
        if entry is not None:
            age = max(0.0, now - entry.stored_at)
            if age < self._cache_ttl_seconds:
                return ProbeReport(
                    outcome=entry.outcome,
                    cached=True,
                    cache_age_seconds=age,
                    checked_at=entry.checked_at,
                )
        outcome = await self._prober(url, dict(headers or {}))
        checked_at = self._now()
        self._cache[key] = _CacheEntry(
            stored_at=self._clock(),
            checked_at=checked_at,
            outcome=outcome,
        )
        return ProbeReport(
            outcome=outcome,
            cached=False,
            cache_age_seconds=0.0,
            checked_at=checked_at,
        )


def safe_url(url: str) -> str:
    """只保留 scheme / host[:port] / path：去掉 userinfo、query 与 fragment。

    运维需要看到探的是哪个端点，但 URL 里若被塞了凭据或 token（userinfo / query），
    这些值不能出现在接口响应里。
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid>"
    netloc = parts.netloc.rsplit("@", 1)[-1]
    if not parts.scheme or not netloc:
        return "<invalid>"
    return urlunsplit((parts.scheme, netloc, parts.path or "", "", ""))


def browser_mcp_summary(
    report: ProbeReport | None,
    *,
    server: dict[str, Any],
    probe: BrowserStatusProbe,
) -> dict[str, Any]:
    """把内部报告折成 API 形状（不含 headers）。"""
    outcome = report.outcome if report is not None else None
    return {
        "configured": True,
        "server_name": BROWSER_SERVER_NAME,
        "type": server.get("type"),
        "url": safe_url(str(server.get("url") or "")),
        "ok": outcome.ok if outcome is not None else None,
        "reachable": outcome.ok if outcome is not None else None,
        "status_code": outcome.status_code if outcome is not None else None,
        "latency_ms": outcome.latency_ms if outcome is not None else None,
        "error": outcome.error if outcome is not None else None,
        "last_probe_at": report.checked_at.isoformat() if report is not None else None,
        "cached": report.cached if report is not None else False,
        "cache_age_seconds": (round(report.cache_age_seconds, 3) if report is not None else None),
        "cache_ttl_seconds": probe.cache_ttl_seconds,
        "timeout_seconds": probe.timeout_seconds,
    }
