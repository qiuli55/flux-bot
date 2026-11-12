"""Doubao / Volcengine Ark 适配器（火山方舟 OpenAI 兼容端点）。

端点与参数口径（来源：
https://ark.volcengine.com/region:cn-beijing/docs/compatible-with-openai-sdk 与
https://ark.volcengine.com/region:cn-beijing/docs/base-url-and-authentication，
2026-11-12 抓取）：`POST https://ark.cn-beijing.volces.com/api/v3/chat/completions`，
鉴权头 `Authorization: Bearer <ARK_API_KEY>`，生成上限参数名为 `max_tokens`。

默认模型取官方示例里的 `doubao-seed-2-1-pro-260915`。Ark 的 model 字段也接受
账号内的推理接入点 Endpoint ID（`ep-...` 形式）——这类 id 无法在代码里穷举，
用户可用 `FLUX_DOUBAO_MODEL` 或密钥库条目的 models 填任意已开通的 id。
本次只支持 Bearer API Key，不实现 Access Key（AK/SK）签名。

官方端点 / 模型快照见 `flux.core.model_gateway.providers.official_models`。
不填 pricing：价格必须来自官方价格页快照（主规格 §15.4），本适配器不臆造金额。

为什么自己写 `httpx` 而不用官方 SDK：见 `flux.core.model_gateway.http` 的模块说明。
"""

from __future__ import annotations

from flux.core.model_gateway.providers.openai_compatible import OpenAICompatibleProvider
from flux.enums import ModelProvider


class DoubaoProvider(OpenAICompatibleProvider):
    provider = ModelProvider.DOUBAO
    # 方舟兼容端点使用 max_tokens
    token_limit_param = "max_tokens"
