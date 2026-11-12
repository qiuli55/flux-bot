"""Provider 装配。

供应商只服务 Flux 侧基础设施的模型调用（T2 压缩、扫描摘要等）与密钥库通道——
Agent 用什么模型是 Agent 自己的事，不经本注册表（目标架构 §1）；Agent 要用的
第三方模型仍可经密钥库通道下发。除 LOCAL / OPENAI / ANTHROPIC 外，Flux Bot 还把
DeepSeek 与四家中国区供应商（QWEN / ZHIPU / KIMI / DOUBAO）作为一等公民接入：
后五家都是 OpenAI 兼容协议的薄子类，`openai_compatible` 仍作为通用逃生口。

是否可用由各自的 is_configured() 决定（无密钥即不可用），ModelRouter 按此过滤，
所以 /api/v1/health/ready 的 providers 列表会自然只显示已配置的供应商。

本增量不传 pricing（即 pricing=None），于是 ChatResult.cost 恒为 null：单价必须来自
官方价格页快照，属 M6「成本与可观测」（主规格 §15.4：绝不用猜测值填充），
不在本次范围内。
"""

from __future__ import annotations

from flux.config import Settings
from flux.core.agent_runtime.runtimes.base import ModelChannel
from flux.core.model_gateway.base import ModelProviderBase
from flux.core.model_gateway.providers.anthropic import AnthropicProvider
from flux.core.model_gateway.providers.deepseek import DeepSeekProvider
from flux.core.model_gateway.providers.doubao import DoubaoProvider
from flux.core.model_gateway.providers.echo import EchoProvider
from flux.core.model_gateway.providers.kimi import KimiProvider
from flux.core.model_gateway.providers.openai import OpenAIProvider
from flux.core.model_gateway.providers.openai_compatible import OpenAICompatibleProvider
from flux.core.model_gateway.providers.qwen import QwenProvider
from flux.core.model_gateway.providers.zhipu import ZhipuProvider
from flux.enums import ModelProvider
from flux.logging import get_logger

logger = get_logger(__name__)

#: provider 名称 → OpenAI 兼容适配器类。密钥库通道装配与密钥连通性探测共用这张表；
#: 新增 OpenAI 兼容供应商只需在这里加一行（anthropic 协议不同，单独分支处理）。
OPENAI_COMPATIBLE_PROVIDERS: dict[str, type[OpenAICompatibleProvider]] = {
    "openai": OpenAIProvider,
    "deepseek": DeepSeekProvider,
    "qwen": QwenProvider,
    "zhipu": ZhipuProvider,
    "kimi": KimiProvider,
    "doubao": DoubaoProvider,
}


class _OpenAICompatibleChannel(OpenAICompatibleProvider):
    """`openai_compatible` 在 `ModelProvider` 枚举里没有对应值，这里用字符串占位。

    只有错误文案与日志会用到它（`str(self.provider)`），不参与路由。
    """

    provider = "openai_compatible"  # type: ignore[assignment]


def build_channel_provider(
    channel: ModelChannel,
    *,
    model: str,
    timeout: float = 60.0,
    max_retries: int = 2,
) -> OpenAICompatibleProvider | AnthropicProvider:
    """把一条密钥库解析出的通道装配成临时 provider。

    群聊里每个 bot 可以带自己的 key（`tasks.api_key_id`），这时绕过全局路由器，
    直接用该 key 的端点与模型发起对话。
    """
    if channel.provider == "anthropic":
        return AnthropicProvider(
            model,
            api_key=channel.api_key,
            base_url=channel.base_url,
            timeout=timeout,
            max_retries=max_retries,
        )
    provider_cls = OPENAI_COMPATIBLE_PROVIDERS.get(channel.provider)
    if provider_cls is not None:
        return provider_cls(
            model,
            api_key=channel.api_key,
            base_url=channel.base_url,
            timeout=timeout,
            max_retries=max_retries,
        )
    return _OpenAICompatibleChannel(
        model,
        api_key=channel.api_key,
        base_url=channel.base_url,
        timeout=timeout,
        max_retries=max_retries,
    )


def build_providers(settings: Settings) -> dict[ModelProvider, ModelProviderBase]:
    """装配全部供应商。密钥由各适配器自行持有，绝不下发给 Agent（主规格 §14.3）。"""
    providers: dict[ModelProvider, ModelProviderBase] = {
        ModelProvider.LOCAL: EchoProvider(model_name=settings.local_model_name),
        ModelProvider.OPENAI: OpenAIProvider(
            model_name=settings.openai_model,
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=settings.model_timeout_seconds,
            max_retries=settings.model_max_retries,
        ),
        ModelProvider.ANTHROPIC: AnthropicProvider(
            model_name=settings.anthropic_model,
            api_key=settings.anthropic_api_key,
            base_url=settings.anthropic_base_url,
            timeout=settings.model_timeout_seconds,
            max_retries=settings.model_max_retries,
            # max_tokens 是 Anthropic 的必填字段：把兜底值做成显式配置项，而不是写死小值
            default_max_tokens=settings.anthropic_max_tokens,
        ),
        ModelProvider.DEEPSEEK: DeepSeekProvider(
            model_name=settings.deepseek_model,
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            timeout=settings.model_timeout_seconds,
            max_retries=settings.model_max_retries,
        ),
        ModelProvider.QWEN: QwenProvider(
            model_name=settings.qwen_model,
            api_key=settings.qwen_api_key,
            base_url=settings.qwen_base_url,
            timeout=settings.model_timeout_seconds,
            max_retries=settings.model_max_retries,
        ),
        ModelProvider.ZHIPU: ZhipuProvider(
            model_name=settings.zhipu_model,
            api_key=settings.zhipu_api_key,
            base_url=settings.zhipu_base_url,
            timeout=settings.model_timeout_seconds,
            max_retries=settings.model_max_retries,
        ),
        ModelProvider.KIMI: KimiProvider(
            model_name=settings.kimi_model,
            api_key=settings.kimi_api_key,
            base_url=settings.kimi_base_url,
            timeout=settings.model_timeout_seconds,
            max_retries=settings.model_max_retries,
        ),
        ModelProvider.DOUBAO: DoubaoProvider(
            model_name=settings.doubao_model,
            api_key=settings.doubao_api_key,
            base_url=settings.doubao_base_url,
            timeout=settings.model_timeout_seconds,
            max_retries=settings.model_max_retries,
        ),
    }
    logger.info(
        "model_gateway.providers configured=%s",
        [str(name) for name, provider in providers.items() if provider.is_configured()],
    )
    return providers
