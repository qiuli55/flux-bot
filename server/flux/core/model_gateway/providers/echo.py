"""离线回显 Provider。

用途：本地开发与自动化测试。不联网、不产生费用、输出确定，
让 Agent Runtime / Task Engine 在没有模型密钥的环境下也能端到端跑通。

它不是"假模型"——它就是本地模型的一种形态：确定性回显。真正的本地推理
（Ollama / vLLM 等 OpenAI 兼容端点）在 M1 接入（主规格 §19.1 里程碑 M1）。
"""

from __future__ import annotations

import time

from flux.core.model_gateway.base import (
    ChatMessage,
    ChatResult,
    ModelPricing,
    ModelProviderBase,
    TokenUsage,
)
from flux.enums import ModelProvider


class EchoProvider(ModelProviderBase):
    provider = ModelProvider.LOCAL

    def __init__(self, model_name: str = "local-echo", pricing: ModelPricing | None = None) -> None:
        super().__init__(model_name=model_name, pricing=pricing)

    def is_configured(self) -> bool:
        return True

    async def chat(self, messages: list[ChatMessage], **kwargs: object) -> ChatResult:
        started = time.perf_counter()
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        content = f"[local-echo] 收到 {len(messages)} 条消息；最后一条用户输入：{last_user}"
        usage = TokenUsage(
            input_tokens=self.count_tokens(messages),
            output_tokens=self.count_tokens([ChatMessage(role="assistant", content=content)]),
        )
        return ChatResult(
            content=content,
            provider=self.provider,
            model=self.model_name,
            usage=usage,
            latency_ms=int((time.perf_counter() - started) * 1000),
            cost=self.calculate_cost(usage),
        )
