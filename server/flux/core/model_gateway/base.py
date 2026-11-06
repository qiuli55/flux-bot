"""模型网关契约（主规格 §5.3）。

Provider 统一实现 chat() / stream() / count_tokens() / calculate_cost() 四个方法，
上层（Agent Runtime、Task Engine）只依赖本模块的抽象，不感知具体供应商。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from flux.enums import ModelProvider
from flux.errors import ProviderNotConfiguredError
from flux.services.cost_service.calculator import compute_cost


@dataclass(frozen=True)
class ChatMessage:
    role: str  # system / user / assistant
    content: str


def resolve_model_name(kwargs: dict[str, Any], default: str) -> str:
    """单次调用可临时覆盖模型（群聊里每个 bot 有自己的模型）。"""
    override = kwargs.get("model")
    return str(override).strip() if override else default


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class ModelPricing:
    """单价（每 1000 token）。

    默认不填 → calculate_cost 返回 None，绝不用猜测值填充（主规格 §15.4）。
    """

    input_per_1k: float
    output_per_1k: float
    currency: str = "USD"


@dataclass(frozen=True)
class ChatResult:
    content: str
    provider: ModelProvider
    model: str
    usage: TokenUsage
    latency_ms: int
    cost: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class ModelProviderBase(ABC):
    """所有供应商适配器的基类。"""

    provider: ModelProvider

    def __init__(self, model_name: str, pricing: ModelPricing | None = None) -> None:
        self.model_name = model_name
        self.pricing = pricing

    # --- 必须实现 ---

    @abstractmethod
    async def chat(self, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        """非流式对话。"""

    @abstractmethod
    def is_configured(self) -> bool:
        """是否具备调用条件（例如密钥/端点已配置）。未配置的 Provider 不参与路由。"""

    # --- 通用实现，子类可按需覆写 ---

    async def stream(self, messages: list[ChatMessage], **kwargs: Any) -> AsyncIterator[str]:
        """流式对话。

        默认实现基于 chat() 单次返回后整体下发；真正逐 token 流式的 Provider 应覆写本方法。
        """
        result = await self.chat(messages, **kwargs)
        yield result.content

    def count_tokens(self, messages: list[ChatMessage]) -> int:
        """估算输入 token 数。

        M0 采用字符数/4 的粗略估算（英文近似）；接入真实 tokenizer 前，该值仅供预算参考，
        不代表账单口径——账单口径以供应商返回的 usage 为准。
        """
        chars = sum(len(m.content) for m in messages)
        return max(1, chars // 4)

    def calculate_cost(self, usage: TokenUsage) -> float | None:
        if self.pricing is None:
            return None
        return compute_cost(usage.input_tokens, usage.output_tokens, self.pricing)

    # --- 辅助 ---

    def _ensure_configured(self) -> None:
        if not self.is_configured():
            raise ProviderNotConfiguredError(
                f"供应商 {self.provider} 未配置，无法调用",
                details={"provider": str(self.provider)},
            )
