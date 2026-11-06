"""真实模型供应商适配器的离线测试（主规格 §5.3 / §19.1 M1 Issues #013–#015）。

一律使用 `httpx.MockTransport` 注入请求处理函数，不发起任何真实网络请求。
`max_retries` 传 0 或注入 no-op `sleep`，保证用例不会真的等待。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from flux.config import Settings
from flux.core.model_gateway.base import ChatMessage
from flux.core.model_gateway.http import map_status_to_error, post_json
from flux.core.model_gateway.providers.anthropic import DEFAULT_MAX_TOKENS, AnthropicProvider
from flux.core.model_gateway.providers.deepseek import DeepSeekProvider
from flux.core.model_gateway.providers.openai import OpenAIProvider
from flux.core.model_gateway.providers.registry import build_providers
from flux.core.model_gateway.router import ModelRouter
from flux.enums import ModelProvider
from flux.errors import AIOSError, ProviderNotConfiguredError

OPENAI_KEY = "test-openai-key"


class _Recorder:
    """记录最近一次请求，供断言 URL / 请求头 / 请求体。"""

    def __init__(self) -> None:
        self.url: httpx.URL | None = None
        self.headers: httpx.Headers | None = None
        self.body: dict[str, Any] = {}
        self.count = 0


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _json_body(request: httpx.Request) -> dict[str, Any]:
    data = json.loads(request.content)
    assert isinstance(data, dict)
    return data


def _openai_response() -> dict[str, Any]:
    return {
        "choices": [{"message": {"role": "assistant", "content": "你好，我是 OpenAI。"}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def _deepseek_response() -> dict[str, Any]:
    return {
        "choices": [{"message": {"role": "assistant", "content": "你好，我是 DeepSeek。"}}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14},
    }


def _anthropic_response() -> dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": "第一段。"},
            {"type": "text", "text": "第二段。"},
        ],
        "usage": {"input_tokens": 21, "output_tokens": 13},
    }


def _no_sleep_sink(delays: list[float]):
    async def _sleep(seconds: float) -> None:
        delays.append(seconds)

    return _sleep


# --- OpenAI（Issue #013）---


async def test_openai_request_and_response_mapping() -> None:
    recorder = _Recorder()

    async def handler(request: httpx.Request) -> httpx.Response:
        recorder.url = request.url
        recorder.headers = request.headers
        recorder.body = _json_body(request)
        return httpx.Response(200, json=_openai_response())

    provider = OpenAIProvider(
        model_name="gpt-5.5",
        api_key=OPENAI_KEY,
        base_url="https://api.openai.com/v1",
        client=_client(handler),
        max_retries=0,
    )
    result = await provider.chat(
        [ChatMessage(role="user", content="你好")], max_tokens=256, temperature=0.3
    )

    assert str(recorder.url) == "https://api.openai.com/v1/chat/completions"
    assert recorder.headers is not None
    assert recorder.headers["authorization"] == f"Bearer {OPENAI_KEY}"
    assert "max_completion_tokens" in recorder.body
    assert "max_tokens" not in recorder.body
    assert recorder.body["temperature"] == 0.3
    assert result.content == "你好，我是 OpenAI。"
    assert result.provider == ModelProvider.OPENAI
    assert result.model == "gpt-5.5"
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 7
    assert result.cost is None
    assert result.latency_ms >= 0
    assert result.raw["usage"]["total_tokens"] == 18


# --- DeepSeek（Issue #015）---


async def test_deepseek_request_and_default_model() -> None:
    recorder = _Recorder()

    async def handler(request: httpx.Request) -> httpx.Response:
        recorder.url = request.url
        recorder.body = _json_body(request)
        return httpx.Response(200, json=_deepseek_response())

    provider = DeepSeekProvider(
        model_name="deepseek-flash",
        api_key="test-deepseek-key",
        base_url="https://api.deepseek.com",
        client=_client(handler),
        max_retries=0,
    )
    result = await provider.chat([ChatMessage(role="user", content="你好")], max_tokens=128)

    assert str(recorder.url) == "https://api.deepseek.com/chat/completions"
    assert "max_tokens" in recorder.body
    assert "max_completion_tokens" not in recorder.body
    assert recorder.body["model"] == "deepseek-flash"
    assert result.provider == ModelProvider.DEEPSEEK
    assert result.content == "你好，我是 DeepSeek。"
    assert result.usage.input_tokens == 9
    assert result.cost is None


@pytest.mark.parametrize(
    ("provider_cls", "base_url"),
    [
        (OpenAIProvider, "https://api.openai.com/v1"),
        (DeepSeekProvider, "https://api.deepseek.com"),
    ],
)
async def test_no_token_limit_field_when_caller_omits_it(provider_cls: Any, base_url: str) -> None:
    """调用方不设上限 → 请求体里不出现任何生成上限字段（不填 1024 兜底）。

    兜底值会把 Agent 的完整文件输出截断、让提案 JSON 解析失败，所以"不传"必须等于
    "不下发"，而不是"退回某个默认值"。
    """
    recorder = _Recorder()

    async def handler(request: httpx.Request) -> httpx.Response:
        recorder.body = _json_body(request)
        return httpx.Response(200, json=_openai_response())

    provider = provider_cls(
        model_name="test-model",
        api_key="test-key",
        base_url=base_url,
        client=_client(handler),
        max_retries=0,
    )
    await provider.chat([ChatMessage(role="user", content="你好")])

    assert "max_tokens" not in recorder.body
    assert "max_completion_tokens" not in recorder.body


def test_deepseek_default_model_name_from_settings() -> None:
    providers = build_providers(Settings())
    assert providers[ModelProvider.DEEPSEEK].model_name == "deepseek-flash"


# --- Anthropic（Issue #014）---


async def test_anthropic_request_and_response_mapping() -> None:
    recorder = _Recorder()

    async def handler(request: httpx.Request) -> httpx.Response:
        recorder.url = request.url
        recorder.headers = request.headers
        recorder.body = _json_body(request)
        return httpx.Response(200, json=_anthropic_response())

    provider = AnthropicProvider(
        model_name="claude-sonnet-5-5",
        api_key="test-anthropic-key",
        base_url="https://api.anthropic.com",
        client=_client(handler),
        max_retries=0,
    )
    result = await provider.chat(
        [
            ChatMessage(role="system", content="你是审查者。"),
            ChatMessage(role="user", content="看这段代码"),
        ],
        max_tokens=512,
        temperature=1.5,
    )

    assert str(recorder.url) == "https://api.anthropic.com/v1/messages"
    assert recorder.headers is not None
    assert recorder.headers["x-api-key"] == "test-anthropic-key"
    assert recorder.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in recorder.headers
    assert recorder.body["system"] == "你是审查者。"
    assert recorder.body["max_tokens"] == 512
    assert all(message["role"] != "system" for message in recorder.body["messages"])
    assert recorder.body["temperature"] == 1.0
    assert result.content == "第一段。第二段。"
    assert result.usage.input_tokens == 21
    assert result.usage.output_tokens == 13
    assert result.provider == ModelProvider.ANTHROPIC
    assert result.cost is None


async def test_anthropic_system_absent_when_no_system_message() -> None:
    recorder = _Recorder()

    async def handler(request: httpx.Request) -> httpx.Response:
        recorder.body = _json_body(request)
        return httpx.Response(200, json=_anthropic_response())

    provider = AnthropicProvider(
        model_name="claude-sonnet-5-5",
        api_key="test-anthropic-key",
        base_url="https://api.anthropic.com",
        client=_client(handler),
        max_retries=0,
    )
    await provider.chat([ChatMessage(role="user", content="你好")])

    assert "system" not in recorder.body
    assert recorder.body["max_tokens"] == DEFAULT_MAX_TOKENS


async def test_anthropic_uses_configured_fallback_when_caller_omits_max_tokens() -> None:
    """调用方不设上限时下发的是配置值，而不是写死的小值。

    Anthropic 的 max_tokens 是必填字段、做不到"不传=不限"，但兜底值一旦偏小，
    Agent 要产出的完整文件内容会被截断、提案 JSON 解析失败——所以这个兜底必须
    显式可配（FLUX_ANTHROPIC_MAX_TOKENS）且给足余量。
    """
    recorder = _Recorder()

    async def handler(request: httpx.Request) -> httpx.Response:
        recorder.body = _json_body(request)
        return httpx.Response(200, json=_anthropic_response())

    provider = AnthropicProvider(
        model_name="claude-sonnet-5-5",
        api_key="test-anthropic-key",
        base_url="https://api.anthropic.com",
        default_max_tokens=48000,
        client=_client(handler),
        max_retries=0,
    )
    await provider.chat([ChatMessage(role="user", content="你好")])

    assert recorder.body["max_tokens"] == 48000


def test_anthropic_module_default_is_not_a_truncating_value() -> None:
    """模块级默认兜底不得退回小值（曾经是 1024，会把提案输出截断）。"""
    assert DEFAULT_MAX_TOKENS >= 32000


def test_registry_passes_anthropic_max_tokens_from_settings() -> None:
    settings = Settings(anthropic_max_tokens=12345)
    providers = build_providers(settings)
    provider = providers[ModelProvider.ANTHROPIC]
    assert isinstance(provider, AnthropicProvider)
    assert provider.default_max_tokens == 12345


async def test_anthropic_missing_content_raises_provider_error() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": 1}})

    provider = AnthropicProvider(
        model_name="claude-sonnet-5-5",
        api_key="test-anthropic-key",
        base_url="https://api.anthropic.com",
        client=_client(handler),
        max_retries=0,
    )
    with pytest.raises(AIOSError) as excinfo:
        await provider.chat([ChatMessage(role="user", content="你好")])
    assert excinfo.value.code == "provider_error"


# --- 错误映射 ---


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (401, "provider_auth_error"),
        (429, "provider_rate_limited"),
        (500, "provider_unavailable"),
        (400, "provider_bad_request"),
    ],
)
async def test_status_is_mapped_to_domain_error(status: int, expected_code: str) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "上游拒绝了本次调用"}})

    provider = OpenAIProvider(
        model_name="gpt-5.5",
        api_key=OPENAI_KEY,
        base_url="https://api.openai.com/v1",
        client=_client(handler),
        max_retries=0,
    )
    with pytest.raises(AIOSError) as excinfo:
        await provider.chat([ChatMessage(role="user", content="你好")])

    assert excinfo.value.code == expected_code
    assert excinfo.value.details["provider"] == "openai"
    assert excinfo.value.details["status"] == status


def test_map_status_to_error_reads_anthropic_error_shape() -> None:
    error = map_status_to_error(
        401, '{"type": "error", "error": {"type": "authentication_error"}}', "anthropic"
    )
    assert error.code == "provider_auth_error"
    assert error.details["provider"] == "anthropic"
    assert "authentication_error" in error.message


def test_map_status_to_error_truncates_long_summary() -> None:
    raw_summary = "很长的上游错误" * 100
    body = '{"error": {"message": "' + raw_summary + '"}}'
    error = map_status_to_error(400, body, "openai")
    assert error.code == "provider_bad_request"
    # 上游摘要截断到 500 字符，message 只多出固定前缀
    assert raw_summary not in error.message
    assert len(error.message) <= 500 + len("供应商 openai 返回 HTTP 400：")


def test_map_status_to_error_falls_back_to_raw_text() -> None:
    error = map_status_to_error(502, "<html>Bad Gateway</html>", "deepseek")
    assert error.code == "provider_unavailable"
    assert "Bad Gateway" in error.message


async def test_timeout_maps_to_provider_timeout() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("读取超时", request=request)

    with pytest.raises(AIOSError) as excinfo:
        await post_json(
            _client(handler),
            "https://api.openai.com/v1/chat/completions",
            payload={"model": "gpt-5.5"},
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
            provider="openai",
            max_retries=0,
        )
    assert excinfo.value.code == "provider_timeout"
    assert excinfo.value.details["provider"] == "openai"
    assert excinfo.value.details["status"] is None
    assert OPENAI_KEY not in excinfo.value.message


async def test_invalid_json_success_body_raises_provider_error() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="不是 JSON")

    with pytest.raises(AIOSError) as excinfo:
        await post_json(
            _client(handler),
            "https://api.openai.com/v1/chat/completions",
            payload={"model": "gpt-5.5"},
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
            provider="openai",
            max_retries=0,
        )
    assert excinfo.value.code == "provider_error"
    assert excinfo.value.details["provider"] == "openai"


# --- 重试与退避 ---


async def test_retry_exhausted_on_persistent_rate_limit() -> None:
    calls = {"count": 0}
    delays: list[float] = []

    async def handler(_: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(429, json={"error": {"message": "限流"}})

    with pytest.raises(AIOSError) as excinfo:
        await post_json(
            _client(handler),
            "https://api.openai.com/v1/chat/completions",
            payload={"model": "gpt-5.5"},
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
            provider="openai",
            max_retries=2,
            sleep=_no_sleep_sink(delays),
        )

    assert excinfo.value.code == "provider_rate_limited"
    assert calls["count"] == 3
    assert delays == [0.5, 1.0]


async def test_retry_recovers_after_transient_server_error() -> None:
    calls = {"count": 0}
    delays: list[float] = []

    async def handler(_: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(500, json={"error": {"message": "上游故障"}})
        return httpx.Response(200, json=_openai_response())

    data = await post_json(
        _client(handler),
        "https://api.openai.com/v1/chat/completions",
        payload={"model": "gpt-5.5"},
        headers={"Authorization": f"Bearer {OPENAI_KEY}"},
        provider="openai",
        max_retries=2,
        sleep=_no_sleep_sink(delays),
    )

    assert calls["count"] == 2
    assert delays == [0.5]
    assert data["choices"][0]["message"]["content"] == "你好，我是 OpenAI。"


async def test_non_retryable_status_does_not_retry() -> None:
    calls = {"count": 0}

    async def handler(_: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(401, json={"error": {"message": "密钥无效"}})

    with pytest.raises(AIOSError) as excinfo:
        await post_json(
            _client(handler),
            "https://api.openai.com/v1/chat/completions",
            payload={"model": "gpt-5.5"},
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
            provider="openai",
            max_retries=2,
            sleep=_no_sleep_sink([]),
        )
    assert excinfo.value.code == "provider_auth_error"
    assert calls["count"] == 1


async def test_connection_error_retries_then_reports_unavailable() -> None:
    calls = {"count": 0}
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise httpx.ConnectError("连接被拒绝", request=request)

    with pytest.raises(AIOSError) as excinfo:
        await post_json(
            _client(handler),
            "https://api.deepseek.com/chat/completions",
            payload={"model": "deepseek-flash"},
            headers={"Authorization": "Bearer test-deepseek-key"},
            provider="deepseek",
            max_retries=1,
            sleep=_no_sleep_sink(delays),
        )
    assert excinfo.value.code == "provider_unavailable"
    assert calls["count"] == 2
    assert delays == [0.5]


# --- 未配置 ---


async def test_unconfigured_provider_reports_not_configured() -> None:
    provider = OpenAIProvider(
        model_name="gpt-5.5",
        api_key=None,
        base_url="https://api.openai.com/v1",
    )
    assert provider.is_configured() is False
    with pytest.raises(ProviderNotConfiguredError) as excinfo:
        await provider.chat([ChatMessage(role="user", content="你好")])
    assert excinfo.value.code == "provider_not_configured"
    assert excinfo.value.details["provider"] == "openai"


async def test_unconfigured_anthropic_reports_not_configured() -> None:
    provider = AnthropicProvider(
        model_name="claude-sonnet-5-5",
        api_key=None,
        base_url="https://api.anthropic.com",
    )
    assert provider.is_configured() is False
    with pytest.raises(ProviderNotConfiguredError):
        await provider.chat([ChatMessage(role="user", content="你好")])


# --- registry ---


def test_registry_registers_all_four_providers() -> None:
    providers = build_providers(Settings())
    assert set(providers) == {
        ModelProvider.LOCAL,
        ModelProvider.OPENAI,
        ModelProvider.ANTHROPIC,
        ModelProvider.DEEPSEEK,
    }


def test_registry_without_keys_does_not_expose_http_providers() -> None:
    """没有密钥时四家供应商都不可用，只剩离线回显的 LOCAL。"""
    settings = Settings(
        openai_api_key=None,
        anthropic_api_key=None,
        deepseek_api_key=None,
    )
    router = ModelRouter(build_providers(settings))
    assert router.available() == [ModelProvider.LOCAL]
