"""Qwen / DashScope 适配器（阿里云百炼 OpenAI 兼容模式）。

端点与参数口径（来源：https://www.alibabacloud.com/help/zh/model-studio/base-url，
页面标注更新时间 2026-10-11；2026-11-12 抓取）：
`POST https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions`，
鉴权头 `Authorization: Bearer <API_KEY>`，生成上限参数名为 `max_tokens`。

默认模型为 `qwen3.8-flash`：官方文本生成模型列表（2026-11-04 版）里的成本优先选项，
适合平台内部的压缩 / 摘要等高频调用。官方说明 `qwen4-max-preview` 仅限 Token Plan、
不面向后端服务，故不作默认值。可用 `FLUX_QWEN_MODEL` 覆盖默认模型；
新加坡地域可用 `FLUX_QWEN_BASE_URL`（或密钥库条目的 base_url）换成
`https://dashscope-intl.aliyuncs.com/compatible-mode/v1`。

官方端点 / 模型快照见 `flux.core.model_gateway.providers.official_models`。
不填 pricing：价格必须来自官方价格页快照（主规格 §15.4），本适配器不臆造金额。

为什么自己写 `httpx` 而不用官方 SDK：见 `flux.core.model_gateway.http` 的模块说明。
"""

from __future__ import annotations

from flux.core.model_gateway.providers.openai_compatible import OpenAICompatibleProvider
from flux.enums import ModelProvider


class QwenProvider(OpenAICompatibleProvider):
    provider = ModelProvider.QWEN
    # DashScope 兼容模式沿用 OpenAI 早期的 max_tokens 口径
    token_limit_param = "max_tokens"
