"""DeepSeek 适配器（主规格 §5.3 / M1 交付物 Issue #015）。

端点与参数口径（来源：api-docs.deepseek.com，Change Log 2026-09-10）：
`POST https://api.deepseek.com/chat/completions`，鉴权头 `Authorization: Bearer <API_KEY>`。
协议与 OpenAI 兼容，但生成上限参数名仍是 `max_tokens`（不是 `max_completion_tokens`）。

默认模型 id 为 `deepseek-flash`：2026-09-10 起该 id 对应 DeepSeek-V4.1-Flash。
旧名 `deepseek-v4-flash` 仍可调用，但对应模型已下线，本适配器不使用旧名；
可用环境变量 `FLUX_DEEPSEEK_MODEL` 覆盖默认值。Anthropic 兼容端点本次不使用。

**max_tokens 要给足**：DeepSeek 的 thinking 模式默认开启（`thinking.type` 默认为 `enabled`），
推理 token 计入 `max_tokens`。2026-09-30 用 `deepseek-flash` 实测：`max_tokens=32` 时
32 个输出 token 全被推理吃掉，`choices[0].message.content` 返回空串，
而 `choices[0].message.reasoning_content` 有值；同一请求 `max_tokens=512` 正常返回
`content="收到"`、`finish_reason="stop"`。故调用方不要用很小的 max_tokens 做探活；
不传则整条字段都不下发，由模型自身的默认输出上限决定。

为什么自己写 `httpx` 而不用官方 SDK：见 `flux.core.model_gateway.http` 的模块说明。
"""

from __future__ import annotations

from flux.core.model_gateway.providers.openai_compatible import OpenAICompatibleProvider
from flux.enums import ModelProvider


class DeepSeekProvider(OpenAICompatibleProvider):
    provider = ModelProvider.DEEPSEEK
    # DeepSeek 仍是 OpenAI 早期的 max_tokens 口径
    token_limit_param = "max_tokens"
