"""中国区模型供应商（Qwen / 智谱 GLM / Kimi / 豆包）的离线测试。

四家都是 OpenAI 兼容协议的薄子类，覆盖点与 DeepSeek 一致：官方默认端点 / 模型、
`max_tokens` 口径、未配置、HTTP 4xx/5xx 映射、Base URL 覆盖、注册表装配、
密钥库录入与连通性探测、health/ready 的已配置供应商列表。

一律使用 `httpx.MockTransport` 注入请求处理函数，不发起任何真实网络请求。
"""

from __future__ import annotations

import json
from typing import Any, NamedTuple

import httpx
import pytest
from fastapi.testclient import TestClient

from flux.config import Settings
from flux.core.agent_runtime.model_catalog import (
    PROVIDER_DOUBAO,
    PROVIDER_KIMI,
    PROVIDER_QWEN,
    PROVIDER_TITLES,
    PROVIDER_ZHIPU,
    default_base_url,
    supports_provider,
)
from flux.core.agent_runtime.runtimes.base import ModelChannel
from flux.core.key_vault.probe import probe_channel
from flux.core.model_gateway.base import ChatMessage
from flux.core.model_gateway.providers.doubao import DoubaoProvider
from flux.core.model_gateway.providers.kimi import KimiProvider
from flux.core.model_gateway.providers.official_models import (
    DOUBAO_BASE_URL,
    DOUBAO_DEFAULT_MODEL,
    KIMI_BASE_URL,
    KIMI_DEFAULT_MODEL,
    QWEN_BASE_URL,
    QWEN_DEFAULT_MODEL,
    ZHIPU_BASE_URL,
    ZHIPU_DEFAULT_MODEL,
)
from flux.core.model_gateway.providers.openai_compatible import OpenAICompatibleProvider
from flux.core.model_gateway.providers.qwen import QwenProvider
from flux.core.model_gateway.providers.registry import (
    OPENAI_COMPATIBLE_PROVIDERS,
    build_channel_provider,
    build_providers,
)
from flux.core.model_gateway.providers.zhipu import ZhipuProvider
from flux.core.model_gateway.router import ModelRouter
from flux.enums import ModelProvider
from flux.errors import AIOSError, ProviderNotConfiguredError
from flux.main import create_app

PREFIX = "/api/v1"


class Case(NamedTuple):
    provider: ModelProvider
    cls: type[OpenAICompatibleProvider]
    key_provider: str
    base_url: str
    model: str
    settings_prefix: str


CASES: tuple[Case, ...] = (
    Case(
        ModelProvider.QWEN,
        QwenProvider,
        PROVIDER_QWEN,
        QWEN_BASE_URL,
        QWEN_DEFAULT_MODEL,
        "qwen",
    ),
    Case(
        ModelProvider.ZHIPU,
        ZhipuProvider,
        PROVIDER_ZHIPU,
        ZHIPU_BASE_URL,
        ZHIPU_DEFAULT_MODEL,
        "zhipu",
    ),
    Case(
        ModelProvider.KIMI,
        KimiProvider,
        PROVIDER_KIMI,
        KIMI_BASE_URL,
        KIMI_DEFAULT_MODEL,
        "kimi",
    ),
    Case(
        ModelProvider.DOUBAO,
        DoubaoProvider,
        PROVIDER_DOUBAO,
        DOUBAO_BASE_URL,
        DOUBAO_DEFAULT_MODEL,
        "doubao",
    ),
)
CASE_IDS = [case.provider.value for case in CASES]


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _response(content: str = "你好") -> dict[str, Any]:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
    }


def _all_keys_disabled(settings: Settings, **updates: object) -> Settings:
    """显式关掉所有平台密钥，避免开发机 .env 让"未配置"断言漂移。"""
    values: dict[str, object] = {
        "openai_api_key": None,
        "anthropic_api_key": None,
        "deepseek_api_key": None,
        "qwen_api_key": None,
        "zhipu_api_key": None,
        "kimi_api_key": None,
        "doubao_api_key": None,
    }
    values.update(updates)
    return settings.model_copy(update=values)


# --- 官方默认值 ---


def test_settings_defaults_match_official_catalog() -> None:
    """Settings 的默认端点 / 模型必须与官方快照一致（防止两处各写一份后漂移）。"""
    settings = Settings()
    assert settings.qwen_base_url == QWEN_BASE_URL
    assert settings.qwen_model == QWEN_DEFAULT_MODEL
    assert settings.zhipu_base_url == ZHIPU_BASE_URL
    assert settings.zhipu_model == ZHIPU_DEFAULT_MODEL
    assert settings.kimi_base_url == KIMI_BASE_URL
    assert settings.kimi_model == KIMI_DEFAULT_MODEL
    assert settings.doubao_base_url == DOUBAO_BASE_URL
    assert settings.doubao_model == DOUBAO_DEFAULT_MODEL


def test_official_base_urls_are_the_documented_ones() -> None:
    """端点写死断言语义：它们是官方文档值，改代码时必须重新核验。"""
    assert QWEN_BASE_URL == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert ZHIPU_BASE_URL == "https://open.bigmodel.cn/api/paas/v4"
    assert KIMI_BASE_URL == "https://api.moonshot.ai/v1"
    assert DOUBAO_BASE_URL == "https://ark.cn-beijing.volces.com/api/v3"
    # Coding 套餐端点不能成为默认值
    assert "coding" not in ZHIPU_BASE_URL


def test_default_model_ids_are_the_documented_ones() -> None:
    assert QWEN_DEFAULT_MODEL == "qwen3.8-flash"
    assert ZHIPU_DEFAULT_MODEL == "glm-5.3"
    assert KIMI_DEFAULT_MODEL == "kimi-k3.1-pro"
    assert DOUBAO_DEFAULT_MODEL == "doubao-seed-2-1-pro-260915"


# --- 请求 / 响应映射 ---


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
async def test_provider_uses_official_endpoint_and_max_tokens(case: Case) -> None:
    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_response())

    provider = case.cls(
        model_name=case.model,
        api_key="test-key",
        base_url=case.base_url,
        client=_client(handler),
        max_retries=0,
    )
    result = await provider.chat(
        [ChatMessage(role="user", content="你好")], max_tokens=64, temperature=0.2
    )

    assert seen["url"] == f"{case.base_url}/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == case.model
    assert seen["body"]["max_tokens"] == 64
    assert "max_completion_tokens" not in seen["body"]
    assert seen["body"]["temperature"] == 0.2
    assert result.provider is case.provider
    assert result.model == case.model
    assert result.content == "你好"
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 6
    # 不填单价：金额必须来自官方价格页快照（主规格 §15.4）
    assert result.cost is None


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
async def test_provider_omits_token_limit_when_caller_omits_it(case: Case) -> None:
    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_response())

    provider = case.cls(
        model_name=case.model,
        api_key="test-key",
        base_url=case.base_url,
        client=_client(handler),
        max_retries=0,
    )
    await provider.chat([ChatMessage(role="user", content="你好")])

    assert "max_tokens" not in seen["body"]
    assert "max_completion_tokens" not in seen["body"]


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
async def test_unconfigured_provider_reports_not_configured(case: Case) -> None:
    provider = case.cls(model_name=case.model, api_key=None, base_url=case.base_url)
    assert provider.is_configured() is False

    with pytest.raises(ProviderNotConfiguredError) as excinfo:
        await provider.chat([ChatMessage(role="user", content="你好")])

    assert excinfo.value.code == "provider_not_configured"
    assert excinfo.value.details["provider"] == case.provider.value


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (401, "provider_auth_error"),
        (403, "provider_auth_error"),
        (429, "provider_rate_limited"),
        (500, "provider_unavailable"),
        (503, "provider_unavailable"),
        (400, "provider_bad_request"),
    ],
)
async def test_status_is_mapped_to_domain_error(
    case: Case, status: int, expected_code: str
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "上游拒绝了本次调用"}})

    provider = case.cls(
        model_name=case.model,
        api_key="test-key",
        base_url=case.base_url,
        client=_client(handler),
        max_retries=0,
    )

    with pytest.raises(AIOSError) as excinfo:
        await provider.chat([ChatMessage(role="user", content="你好")])

    assert excinfo.value.code == expected_code
    assert excinfo.value.details["provider"] == case.provider.value
    assert excinfo.value.details["status"] == status


# --- 注册表装配 ---


def test_registry_maps_every_openai_compatible_provider_to_its_class() -> None:
    assert set(OPENAI_COMPATIBLE_PROVIDERS) == {
        "openai",
        "deepseek",
        PROVIDER_QWEN,
        PROVIDER_ZHIPU,
        PROVIDER_KIMI,
        PROVIDER_DOUBAO,
    }
    assert OPENAI_COMPATIBLE_PROVIDERS[PROVIDER_QWEN] is QwenProvider
    assert OPENAI_COMPATIBLE_PROVIDERS[PROVIDER_ZHIPU] is ZhipuProvider
    assert OPENAI_COMPATIBLE_PROVIDERS[PROVIDER_KIMI] is KimiProvider
    assert OPENAI_COMPATIBLE_PROVIDERS[PROVIDER_DOUBAO] is DoubaoProvider


def test_registry_builds_cn_providers_from_settings() -> None:
    settings = Settings(
        qwen_api_key="qwen-key",
        zhipu_api_key="zhipu-key",
        kimi_api_key="kimi-key",
        doubao_api_key="doubao-key",
    )
    providers = build_providers(settings)

    for case in CASES:
        provider = providers[case.provider]
        assert isinstance(provider, case.cls)
        assert provider.api_key == f"{case.settings_prefix}-key"
        assert provider.base_url == case.base_url
        assert provider.model_name == case.model
        assert provider.is_configured() is True


def test_registry_honors_base_url_and_model_overrides() -> None:
    settings = Settings(
        qwen_api_key="qwen-key",
        qwen_base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        qwen_model="qwen3.8-max",
    )
    provider = build_providers(settings)[ModelProvider.QWEN]

    assert provider.base_url == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    assert provider.model_name == "qwen3.8-max"


def test_router_only_exposes_configured_cn_providers(settings: Settings) -> None:
    settings = _all_keys_disabled(settings, qwen_api_key="qwen-key", doubao_api_key="doubao-key")
    router = ModelRouter(build_providers(settings))

    assert router.available() == [ModelProvider.LOCAL, ModelProvider.QWEN, ModelProvider.DOUBAO]


def test_router_excludes_unconfigured_cn_providers(settings: Settings) -> None:
    router = ModelRouter(build_providers(_all_keys_disabled(settings)))
    assert router.available() == [ModelProvider.LOCAL]


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_build_channel_provider_returns_the_specific_adapter(case: Case) -> None:
    channel = ModelChannel(
        provider=case.key_provider,
        base_url="https://proxy.example.test/v1",
        api_key="sk-test-12345678",
        key_id="stub-key",
    )

    provider = build_channel_provider(channel, model="some-model")

    assert isinstance(provider, case.cls)
    assert provider.base_url == "https://proxy.example.test/v1"
    assert provider.model_name == "some-model"


# --- 密钥库 ---


def test_default_base_url_reads_settings_for_cn_providers() -> None:
    settings = Settings(
        qwen_base_url="https://qwen.example.test/v1",
        zhipu_base_url="https://zhipu.example.test/v4",
        kimi_base_url="https://kimi.example.test/v1",
        doubao_base_url="https://doubao.example.test/api/v3",
    )
    expected = {
        PROVIDER_QWEN: "https://qwen.example.test/v1",
        PROVIDER_ZHIPU: "https://zhipu.example.test/v4",
        PROVIDER_KIMI: "https://kimi.example.test/v1",
        PROVIDER_DOUBAO: "https://doubao.example.test/api/v3",
    }

    for key_provider, base_url in expected.items():
        assert default_base_url(settings, key_provider) == base_url


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_key_vault_api_accepts_cn_provider(client: TestClient, case: Case) -> None:
    response = client.post(
        f"{PREFIX}/keys",
        json={
            "name": f"{case.settings_prefix}-official",
            "provider": case.key_provider,
            "api_key": "sk-test-12345678",
            "models": [case.model],
        },
    )

    assert response.status_code == 200, response.text
    created = response.json()["data"]
    assert created["provider"] == case.key_provider
    # 供应商中文名供前端展示（key_models / 模型目录用）
    assert PROVIDER_TITLES[case.key_provider]
    listed = client.get(f"{PREFIX}/keys").json()["data"]
    assert [row["provider"] for row in listed] == [case.key_provider]
    # Base URL 留空 = 走该供应商的平台默认值，不在录入时臆造
    assert listed[0]["base_url"] == ""


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
async def test_probe_channel_uses_the_cn_adapter(case: Case) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "可用"}}]})

    channel = ModelChannel(
        provider=case.key_provider,
        base_url=case.base_url,
        api_key="sk-test-12345678",
        key_id="stub-key",
    )

    async with _client(handler) as client:
        result = await probe_channel(channel, case.model, client=client)

    assert result["ok"] is True
    assert result["error_code"] == ""
    assert result["reply"] == "可用"
    assert seen["url"] == f"{case.base_url}/chat/completions"
    assert seen["auth"] == "Bearer sk-test-12345678"
    assert seen["body"]["model"] == case.model
    # 探测必须给足输出上限（太小会让 thinking 模型返回空串）
    assert seen["body"]["max_tokens"] == 512


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [(401, "provider_auth_error"), (429, "provider_rate_limited"), (500, "provider_unavailable")],
)
async def test_probe_channel_maps_cn_provider_errors(status: int, expected_code: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "upstream rejected"}})

    channel = ModelChannel(
        provider=PROVIDER_QWEN,
        base_url=QWEN_BASE_URL,
        api_key="sk-test-12345678",
        key_id="stub-key",
    )

    async with _client(handler) as client:
        result = await probe_channel(channel, QWEN_DEFAULT_MODEL, client=client)

    assert result["ok"] is False
    assert result["error_code"] == expected_code


# --- 就绪探针 ---


def test_ready_lists_configured_cn_providers(settings: Settings, db_schema: None) -> None:
    app_settings = _all_keys_disabled(
        settings, qwen_api_key="qwen-key", doubao_api_key="doubao-key"
    )

    with TestClient(create_app(app_settings)) as client:
        response = client.get(f"{PREFIX}/health/ready")

    assert response.status_code == 200, response.text
    assert response.json()["data"]["providers"] == ["local", "qwen", "doubao"]


# --- Agent runtime 兼容面 ---


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_opencode_accepts_cn_provider_keys_but_claude_and_codex_do_not(case: Case) -> None:
    """四家都只有 chat/completions：opencode 的 openai-compatible 适配器可用，
    claude-code（只讲 Anthropic 协议）与 codex（只讲 Responses API）不接收。"""
    assert supports_provider("opencode", case.key_provider) is True
    assert supports_provider("claude-code", case.key_provider) is False
    assert supports_provider("codex", case.key_provider) is False
