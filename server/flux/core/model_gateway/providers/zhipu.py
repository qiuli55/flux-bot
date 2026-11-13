"""Zhipu GLM 适配器（智谱开放平台 OpenAI 兼容端点）。

端点与参数口径（来源：https://docs.bigmodel.cn/cn/api/introduction 与
https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3，2026-11-12 抓取）：
`POST https://open.bigmodel.cn/api/paas/v4/chat/completions`，
鉴权头 `Authorization: Bearer <API_KEY>`，生成上限参数名为 `max_tokens`。

默认模型为 `glm-5.3`（当前官方文档主推的文本模型）。注意
`https://open.bigmodel.cn/api/coding/paas/v4` 是 Coding 订阅套餐专用端点，
不能作为默认值；需要时用 `FLUX_ZHIPU_BASE_URL` 或密钥库条目的 base_url 覆盖，
模型用 `FLUX_ZHIPU_MODEL` 覆盖。

官方端点 / 模型快照见 `flux.core.model_gateway.providers.official_models`。
不填 pricing：价格必须来自官方价格页快照（主规格 §15.4），本适配器不臆造金额。

为什么自己写 `httpx` 而不用官方 SDK：见 `flux.core.model_gateway.http` 的模块说明。
"""

from __future__ import annotations

from flux.core.model_gateway.providers.openai_compatible import OpenAICompatibleProvider
from flux.enums import ModelProvider


class ZhipuProvider(OpenAICompatibleProvider):
    provider = ModelProvider.ZHIPU
    # 智谱的兼容端点接受 OpenAI 的 max_tokens 口径
    token_limit_param = "max_tokens"
