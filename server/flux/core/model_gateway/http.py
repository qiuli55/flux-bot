"""模型网关唯一的 HTTP 调用入口（主规格 §5.1 错误处理：失败 / 超时 → 重试）。

为什么手写 `httpx` 而不用 OpenAI / Anthropic 官方 SDK：

1. 依赖面更小——三家供应商都是 HTTP JSON 接口，SDK 带来的是一层需要跟着升版的封装；
2. 三家协议差异（生成上限参数名、鉴权头、system 位置）本就由本层显式表达，更容易审计；
3. `httpx.MockTransport` 可以离线断言请求与响应映射，避免"只有在有密钥时才验证过"。

安全约束：本模块只读取响应体与状态码，绝不把请求头或密钥放进日志与异常 message，
错误摘要截断到 500 字符（主规格 §14.3 密钥不下发 / §15.3 日志）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from flux.errors import (
    AIOSError,
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

# 可重试的上游状态码：限流 + 上游自身故障。4xx 中的鉴权 / 参数错误重试没有意义。
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# 退避基数（秒）：第 attempt 次重试前等待 0.5 * 2**attempt
RETRY_BACKOFF_BASE_SECONDS = 0.5

# 上游错误摘要的最大长度，避免把整段 HTML / 长错误体写进日志与响应
ERROR_SUMMARY_MAX_CHARS = 500


def _extract_error_summary(body_text: str) -> str:
    """尽力从上游错误体里提取可读摘要。

    已知两种形态：OpenAI 形 `{"error": {"message": ...}}` 与
    Anthropic 形 `{"error": {"type": ...}}`；两者都缺时退回原始文本。
    """
    if not body_text:
        return ""

    parsed: Any
    try:
        parsed = json.loads(body_text)
    except json.JSONDecodeError:
        return body_text

    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            for key in ("message", "type"):
                value = error.get(key)
                if isinstance(value, str) and value:
                    return value
        if isinstance(error, str) and error:
            return error

    return body_text


def map_status_to_error(status: int, body_text: str, provider: str) -> AIOSError:
    """把上游 HTTP 状态码映射为本服务领域异常（主规格 §12.10 统一错误码）。"""
    details: dict[str, Any] = {"provider": provider, "status": status}
    summary = _extract_error_summary(body_text)[:ERROR_SUMMARY_MAX_CHARS]
    suffix = f"：{summary}" if summary else ""
    message = f"供应商 {provider} 返回 HTTP {status}{suffix}"

    if status in (401, 403):
        return ProviderAuthError(message, details=details)
    if status == 429:
        return ProviderRateLimitedError(message, details=details)
    if status >= 500:
        return ProviderUnavailableError(message, details=details)
    return ProviderBadRequestError(message, details=details)


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    payload: dict[str, Any],
    headers: dict[str, str],
    provider: str,
    timeout: float = 60.0,
    max_retries: int = 2,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
    """POST 一个 JSON 请求并返回解析后的 JSON 响应。

    - 可重试状态码（`RETRYABLE_STATUS`）与网络层错误最多重试 `max_retries` 次；
    - 重试前等待 `RETRY_BACKOFF_BASE_SECONDS * 2**attempt` 秒（`sleep` 可注入，测试用 no-op）；
    - 重试耗尽的网络错误分别映射为 `ProviderTimeoutError` / `ProviderUnavailableError`；
    - 非重试状态码立即映射为对应领域异常，不做无意义的等待。
    """
    attempts = max_retries + 1
    for attempt in range(attempts):
        has_retry_left = attempt < max_retries
        try:
            response = await client.post(url, json=payload, headers=headers, timeout=timeout)
        except httpx.TimeoutException:
            if has_retry_left:
                await sleep(RETRY_BACKOFF_BASE_SECONDS * 2**attempt)
                continue
            raise ProviderTimeoutError(
                f"供应商 {provider} 请求超时",
                details={"provider": provider, "status": None},
            ) from None
        except httpx.HTTPError:
            if has_retry_left:
                await sleep(RETRY_BACKOFF_BASE_SECONDS * 2**attempt)
                continue
            raise ProviderUnavailableError(
                f"供应商 {provider} 连接失败",
                details={"provider": provider, "status": None},
            ) from None

        if response.is_success:
            try:
                return response.json()
            except ValueError as exc:
                raise ProviderError(
                    f"供应商 {provider} 返回了无法解析的 JSON 响应",
                    details={"provider": provider, "status": response.status_code},
                ) from exc

        if response.status_code in RETRYABLE_STATUS and has_retry_left:
            await sleep(RETRY_BACKOFF_BASE_SECONDS * 2**attempt)
            continue

        raise map_status_to_error(response.status_code, response.text, provider)

    # 循环内每条分支都会 return 或 raise；走到这里说明重试次数配置异常
    raise ProviderError(
        f"供应商 {provider} 重试次数配置无效",
        details={"provider": provider, "status": None},
    )
