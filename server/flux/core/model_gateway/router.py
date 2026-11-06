"""Model Router（主规格 §5.3）。

M0 的选择策略是简化版：优先显式指定 → 默认供应商 → 任一已配置供应商，否则报错。
源文档要求的"按任务复杂度、成本、历史效果选择"需要历史数据积累，属 M6 成本与可观测里程碑。
"""

from __future__ import annotations

from typing import Any

from flux.core.model_gateway.base import ChatMessage, ChatResult, ModelProviderBase
from flux.enums import ModelProvider
from flux.errors import ProviderNotConfiguredError
from flux.logging import get_logger

logger = get_logger(__name__)


class ModelRouter:
    def __init__(
        self,
        providers: dict[ModelProvider, ModelProviderBase],
        default_provider: ModelProvider = ModelProvider.LOCAL,
    ) -> None:
        self._providers = dict(providers)
        self._default = default_provider

    def available(self) -> list[ModelProvider]:
        """已配置、可参与路由的供应商。"""
        return [name for name, provider in self._providers.items() if provider.is_configured()]

    def get(self, provider: ModelProvider) -> ModelProviderBase:
        instance = self._providers.get(provider)
        if instance is None or not instance.is_configured():
            raise ProviderNotConfiguredError(
                f"供应商 {provider} 未注册或未配置",
                details={
                    "provider": str(provider),
                    "available": [str(p) for p in self.available()],
                },
            )
        return instance

    def select(self, preferred: ModelProvider | None = None) -> ModelProviderBase:
        if preferred is not None:
            return self.get(preferred)
        if self._default in self._providers and self._providers[self._default].is_configured():
            return self._providers[self._default]
        for name in self.available():
            return self._providers[name]
        raise ProviderNotConfiguredError(
            "没有任何已配置的模型供应商",
            details={"available": []},
        )

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        provider: ModelProvider | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        instance = self.select(provider)
        logger.info(
            "模型调用 provider=%s model=%s messages=%d",
            instance.provider,
            instance.model_name,
            len(messages),
        )
        return await instance.chat(messages, **kwargs)
