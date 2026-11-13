"""密钥库连通性探测：用该 key 登记的模型与端点真实发起一次最小对话。

用途是"保存 key 时确认这个端点 / 这把 API Key 真的能用"：Base URL 可达、
鉴权通过、模型 id 存在。探测本身不抛异常——失败统一折叠成
`{ok: False, error, error_code}`，把"保存成功但连通性异常"与"保存失败"区分开。

两处口径：
- 重试次数固定 0：保存是交互动作，不能让用户等退避重试；
- 输出上限至少 512：DeepSeek thinking 模式把推理 token 计入 `max_tokens`，
  给太小（如 32）会让 `content` 为空，实测 512 可用。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from flux.core.agent_runtime.runtimes.base import ModelChannel
from flux.core.key_vault.service import KeyVaultService
from flux.core.model_gateway.base import ChatMessage
from flux.core.model_gateway.providers.anthropic import AnthropicProvider
from flux.core.model_gateway.providers.openai_compatible import OpenAICompatibleProvider
from flux.core.model_gateway.providers.registry import OPENAI_COMPATIBLE_PROVIDERS
from flux.errors import AIOSError
from flux.logging import get_logger

logger = get_logger(__name__)

#: 探测用输出上限（原因见模块说明，不能小）
PROBE_MAX_TOKENS = 512

#: 探测超时（秒）：比常规对话短，避免保存动作卡住管理面
PROBE_TIMEOUT = 20.0

_PROBE_PROMPT = "只回复两个字：可用"

#: 领域错误码 → 给人看的一句话；未命中时退回异常 message
_FRIENDLY_ERRORS = {
    "provider_auth_error": "鉴权失败：API Key 无效或未授权",
    "provider_bad_request": "上游拒绝了请求：模型 id 或请求参数可能不对",
    "provider_rate_limited": "上游限流：请稍后重试",
    "provider_unavailable": "上游不可用：端点无法访问或服务异常",
    "provider_timeout": "请求超时：端点无响应",
    "provider_not_configured": "该密钥缺少可用的调用凭据",
    "configuration_error": "配置不完整，无法发起请求",
}


class _OpenAICompatibleProbe(OpenAICompatibleProvider):
    """`openai_compatible` 在 `ModelProvider` 枚举里没有对应值，探测时用字符串占位。

    只有错误文案与日志会用到它（`str(self.provider)`），不参与任何路由。
    """

    provider = "openai_compatible"  # type: ignore[assignment]


def _build_provider(
    channel: ModelChannel,
    model: str,
    *,
    timeout: float,
    client: httpx.AsyncClient,
) -> OpenAICompatibleProvider | AnthropicProvider:
    if channel.provider == "anthropic":
        return AnthropicProvider(
            model,
            api_key=channel.api_key,
            base_url=channel.base_url,
            timeout=timeout,
            max_retries=0,
            client=client,
        )
    provider_cls = OPENAI_COMPATIBLE_PROVIDERS.get(channel.provider)
    if provider_cls is not None:
        return provider_cls(
            model,
            api_key=channel.api_key,
            base_url=channel.base_url,
            timeout=timeout,
            max_retries=0,
            client=client,
        )
    return _OpenAICompatibleProbe(
        model,
        api_key=channel.api_key,
        base_url=channel.base_url,
        timeout=timeout,
        max_retries=0,
        client=client,
    )


def _blank_result(provider: str, model: str, base_url: str) -> dict[str, Any]:
    return {
        "ok": False,
        "skipped": False,
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "latency_ms": 0,
        "reply": "",
        "error": "",
        "error_code": "",
        "detail": "",
    }


async def probe_channel(
    channel: ModelChannel,
    model: str,
    *,
    timeout: float = PROBE_TIMEOUT,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """对一条已解析的模型通道发起最小请求；永不抛异常。"""
    result = _blank_result(channel.provider, model, channel.base_url)
    started = time.perf_counter()
    owned = client is None
    if client is None:
        client = httpx.AsyncClient()
    try:
        provider = _build_provider(channel, model, timeout=timeout, client=client)
        reply = await provider.chat(
            [ChatMessage(role="user", content=_PROBE_PROMPT)],
            max_tokens=PROBE_MAX_TOKENS,
        )
    except AIOSError as exc:
        result["error_code"] = exc.code
        status = exc.details.get("status") if isinstance(exc.details, dict) else None
        if status == 402:
            result["error"] = "账户余额不足（上游返回 HTTP 402），请充值后再试"
        else:
            result["error"] = _FRIENDLY_ERRORS.get(exc.code, exc.message)
        result["detail"] = exc.message
        logger.warning(
            "key_vault.probe failed provider=%s model=%s code=%s status=%s",
            channel.provider,
            model,
            result["error_code"],
            status,
        )
    except Exception as exc:  # noqa: BLE001 - 探测是管理面辅助动作，任何异常都只能折叠成结果
        result["error_code"] = "probe_failed"
        result["error"] = f"探测失败：{exc}"[:500]
        result["detail"] = str(exc)[:500]
        logger.warning(
            "key_vault.probe error provider=%s model=%s error=%s",
            channel.provider,
            model,
            exc,
        )
    else:
        result["ok"] = True
        result["reply"] = (reply.content or "").strip()[:200]
        logger.info(
            "key_vault.probe ok provider=%s model=%s",
            channel.provider,
            model,
        )
    finally:
        if owned:
            await client.aclose()
        result["latency_ms"] = int((time.perf_counter() - started) * 1000)
    return result


async def probe_vault_key(
    vault: KeyVaultService,
    key_id: str,
    *,
    timeout: float = PROBE_TIMEOUT,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """按 key 库条目探测（用该 key 登记的第一个模型）。

    停用 / 没有模型的条目直接返回 `skipped=True`，不发起网络请求。
    """
    row = await vault.get(key_id)
    models = [str(item).strip() for item in (row.models or []) if str(item).strip()]
    wanted = models[0] if models else ""
    result = _blank_result(row.provider, wanted, row.base_url)
    if not row.enabled:
        result["skipped"] = True
        result["error_code"] = "key_disabled"
        result["error"] = "密钥已停用，未发起连通性校验"
        return result
    if not wanted:
        result["error_code"] = "no_model"
        result["error"] = "该密钥没有登记任何模型，无法校验"
        return result
    channel = await vault.channel_for_key(str(row.id), wanted)
    return await probe_channel(channel, wanted, timeout=timeout, client=client)


__all__ = ["PROBE_MAX_TOKENS", "PROBE_TIMEOUT", "probe_channel", "probe_vault_key"]
