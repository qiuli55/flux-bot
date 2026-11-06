"""OpenAI 适配器（主规格 §5.3 / M1 交付物 Issue #013）。

端点与参数口径（来源：platform.openai.com/docs/models，2026-09-30 抓取）：
`POST /v1/chat/completions`，鉴权头 `Authorization: Bearer <API_KEY>`，
生成上限参数名为 `max_completion_tokens`（`max_tokens` 已废弃，本适配器不使用它）。

默认模型 id 为 `gpt-5.5`，取自开发者文档页。注意官方两处口径并不一致：
`platform.openai.com/docs/models` 列出 `gpt-5.5` / `gpt-5.4` / `gpt-5.4-mini`，
而 `openai.com` 定价页列出的是 GPT-6 Astra / Sol / Luna。本增量以开发者文档页为准，
可用环境变量 `FLUX_OPENAI_MODEL` 覆盖默认值。

为什么自己写 `httpx` 而不用官方 SDK：见 `flux.core.model_gateway.http` 的模块说明。
"""

from __future__ import annotations

from flux.core.model_gateway.providers.openai_compatible import OpenAICompatibleProvider
from flux.enums import ModelProvider


class OpenAIProvider(OpenAICompatibleProvider):
    provider = ModelProvider.OPENAI
    # OpenAI 已废弃 max_tokens，生成上限必须用 max_completion_tokens
    token_limit_param = "max_completion_tokens"
