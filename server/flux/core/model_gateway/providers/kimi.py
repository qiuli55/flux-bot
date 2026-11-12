"""Kimi / Moonshot 适配器。

端点与参数口径（来源：https://platform.kimi.ai/docs/api/overview，
Last-Modified 2026-11-11 23:50 America/Chicago；
https://platform.kimi.ai/docs/api/quickstart，2026-11-12 抓取）：
`POST https://api.moonshot.ai/v1/chat/completions`，
鉴权头 `Authorization: Bearer <MOONSHOT_API_KEY>`，生成上限参数名为 `max_tokens`。

默认模型为 `kimi-k3.1-pro`（官方 quickstart 的默认模型）；官方模型列表（2026-11-11）
另有 `kimi-k3`。可用 `FLUX_KIMI_MODEL` 覆盖默认模型。
官方当前文档使用 api.moonshot.ai；部分用户可能走 .cn 域名，故允许用
`FLUX_KIMI_BASE_URL` 或密钥库条目的 base_url 覆盖。

官方端点 / 模型快照见 `flux.core.model_gateway.providers.official_models`。
不填 pricing：价格必须来自官方价格页快照（主规格 §15.4），本适配器不臆造金额。

为什么自己写 `httpx` 而不用官方 SDK：见 `flux.core.model_gateway.http` 的模块说明。
"""

from __future__ import annotations

from flux.core.model_gateway.providers.openai_compatible import OpenAICompatibleProvider
from flux.enums import ModelProvider


class KimiProvider(OpenAICompatibleProvider):
    provider = ModelProvider.KIMI
    # Moonshot 兼容端点使用 max_tokens
    token_limit_param = "max_tokens"
