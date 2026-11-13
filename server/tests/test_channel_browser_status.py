"""频道状态与浏览器 MCP 状态接口契约测试。

两个接口都是只读运维接口、都必须过 REST 鉴权：

- `/api/v1/channels/status`：配置完整性（只报环境变量名）、最近发送 / 接收、近期错误；
- `/api/v1/browser/status`：browser MCP 是否存在、2 秒探活、30 秒缓存。

用例全部离线：出站草稿写临时 SQLite，浏览器探活用注入的替身或 monkeypatch 的
HTTP client，不访问任何真实平台或 Playwright sidecar。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import flux.core.agent_runtime.browser_status as browser_status_module
from flux.bot_node.main import create_app as create_bot_app
from flux.config import Settings
from flux.core.agent_runtime.browser_status import (
    BrowserStatusProbe,
    ProbeOutcome,
)
from flux.core.channels.outbound_drafts import OutboundDraftService
from flux.core.channels.status import CHANNEL_SPECS
from flux.db.session import create_engine, create_session_factory
from flux.main import create_app

TOKEN = "test-token-channel-browser-status"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CHANNELS = "/api/v1/channels/status"
BROWSER = "/api/v1/browser/status"

#: 所有频道变量都显式置空：用例不读开发机 .env，缺失清单才是确定的。
_NO_CHANNEL_ENV: dict[str, Any] = {
    "claude_extra_mcp_servers": "",
    "feishu_app_id": None,
    "feishu_app_secret": None,
    "feishu_verification_token": None,
    "feishu_encrypt_key": None,
    "feishu_bot_id": None,
    "dingtalk_app_key": None,
    "dingtalk_app_secret": None,
    "dingtalk_token": None,
    "dingtalk_aes_key": None,
    "dingtalk_bot_id": None,
    "wecom_corp_id": None,
    "wecom_agent_id": None,
    "wecom_secret": None,
    "wecom_token": None,
    "wecom_encoding_aes_key": None,
    "wecom_bot_id": None,
}

FEISHU_ALL = {
    "feishu_verification_token": "feishu-verify-token-dummy",
    "feishu_bot_id": "11111111-1111-1111-1111-111111111111",
    "feishu_app_id": "cli_dummy_app_id",
    "feishu_app_secret": "feishu-app-secret-dummy",
    "feishu_encrypt_key": "feishu-encrypt-key-dummy",
}
WECOM_ALL = {
    "wecom_token": "wecom-token-dummy",
    "wecom_encoding_aes_key": "wecom-aes-key-dummy",
    "wecom_bot_id": "22222222-2222-2222-2222-222222222222",
    "wecom_corp_id": "ww-corp-dummy",
    "wecom_agent_id": "1000002",
    "wecom_secret": "wecom-secret-dummy",
}
DINGTALK_ALL = {
    "dingtalk_token": "dingtalk-token-dummy",
    "dingtalk_aes_key": "dingtalk-aes-key-dummy",
    "dingtalk_app_key": "dingtalk-app-key-dummy",
    "dingtalk_bot_id": "33333333-3333-3333-3333-333333333333",
    "dingtalk_app_secret": "dingtalk-app-secret-dummy",
}


def _settings(settings: Settings, **overrides: Any) -> Settings:
    base = {**_NO_CHANNEL_ENV, "auth_token": TOKEN}
    base.update(overrides)
    return settings.model_copy(update=base)


def _client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def _channel(response: dict[str, Any], name: str) -> dict[str, Any]:
    rows = response["data"]
    return next(row for row in rows if row["channel"] == name)


def _seed_draft(
    settings: Settings,
    *,
    channel: str = "feishu",
    body: str = "不应出现在状态接口里的正文",
    outcome: str = "sent",
) -> dict[str, Any]:
    """用独立引擎写一条终态草稿，避免与 TestClient 的事件循环共用连接。"""

    async def _run() -> dict[str, Any]:
        engine = create_engine(settings.database_url)
        try:
            service = OutboundDraftService(create_session_factory(engine))
            draft = await service.create(channel=channel, body=body, dest={"chat_id": "chat"})
            await service.claim(draft.id)
            if outcome == "sent":
                await service.mark_sent(draft.id)
            else:
                await service.mark_failed(draft.id, "TimeoutError: upstream timed out")
            return (await service.get(draft.id)).to_dict()
        finally:
            await engine.dispose()

    return asyncio.run(_run())


# --- 鉴权 ---


@pytest.mark.parametrize("path", [CHANNELS, BROWSER])
def test_status_endpoints_require_rest_auth(settings: Settings, db_schema: None, path: str) -> None:
    """未带令牌一律 401：这是运维信息，不能混进公开回调面。"""
    with _client(_settings(settings)) as client:
        response = client.get(path)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


@pytest.mark.parametrize("path", [CHANNELS, BROWSER])
def test_status_endpoints_work_on_main_and_bot_node(
    settings: Settings, db_schema: None, path: str
) -> None:
    """主应用与 Bot 节点都要挂载这两个路由，且同样要求令牌。"""
    configured = _settings(settings)
    for app in (create_app(configured), create_bot_app(configured)):
        with TestClient(app) as client:
            assert client.get(path).status_code == 401
            success = client.get(path, headers=AUTH)
            assert success.status_code == 200, success.text
            assert success.json()["success"] is True


# --- 频道配置 ---


def test_channel_specs_reference_real_settings_fields() -> None:
    """规格里的字段名必须真实存在，避免拼错后被 getattr 静默当成「缺失」。"""
    for spec in CHANNEL_SPECS:
        for field_name in (
            *spec.inbound_fields,
            *spec.outbound_fields,
            *spec.optional_fields,
        ):
            assert field_name in Settings.model_fields, (spec.name, field_name)


def test_channels_without_env_are_missing_and_list_env_names(
    settings: Settings, db_schema: None
) -> None:
    with _client(_settings(settings)) as client:
        response = client.get(CHANNELS, headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["metadata"]["receive_history"] == "process-local"
    feishu = _channel(body, "feishu")
    assert feishu["config_status"] == "missing"
    assert feishu["inbound_configured"] is False
    assert feishu["outbound_configured"] is False
    assert set(feishu["missing_env"]) == {
        "FLUX_FEISHU_VERIFICATION_TOKEN",
        "FLUX_FEISHU_BOT_ID",
        "FLUX_FEISHU_APP_ID",
        "FLUX_FEISHU_APP_SECRET",
    }
    assert feishu["last_send_at"] is None
    assert feishu["last_receive_at"] is None
    assert feishu["recent_errors"] == []

    wecom = _channel(body, "wecom")
    assert set(wecom["missing_inbound_env"]) == {
        "FLUX_WECOM_TOKEN",
        "FLUX_WECOM_ENCODING_AES_KEY",
        "FLUX_WECOM_BOT_ID",
    }
    assert set(wecom["missing_outbound_env"]) == {
        "FLUX_WECOM_CORP_ID",
        "FLUX_WECOM_AGENT_ID",
        "FLUX_WECOM_SECRET",
    }

    dingtalk = _channel(body, "dingtalk")
    assert "FLUX_DINGTALK_TOKEN" in dingtalk["missing_env"]
    assert "FLUX_DINGTALK_BOT_ID" in dingtalk["missing_env"]


def test_partial_config_lists_only_actually_missing_env(
    settings: Settings, db_schema: None
) -> None:
    """飞书入站齐全、只缺出站 secret：只能报 FLUX_FEISHU_APP_SECRET。"""
    partial = {**FEISHU_ALL, "feishu_app_secret": None}
    with _client(_settings(settings, **partial)) as client:
        response = client.get(CHANNELS, headers=AUTH)

    feishu = _channel(response.json(), "feishu")
    assert feishu["config_status"] == "partial"
    assert feishu["inbound_configured"] is True
    assert feishu["outbound_configured"] is False
    assert feishu["missing_env"] == ["FLUX_FEISHU_APP_SECRET"]
    assert feishu["missing_inbound_env"] == []
    assert feishu["missing_outbound_env"] == ["FLUX_FEISHU_APP_SECRET"]


def test_fully_configured_channel_has_no_missing_env(settings: Settings, db_schema: None) -> None:
    configured = {**FEISHU_ALL, **WECOM_ALL, **DINGTALK_ALL}
    with _client(_settings(settings, **configured)) as client:
        response = client.get(CHANNELS, headers=AUTH)

    body = response.json()
    for name in ("feishu", "wecom", "dingtalk"):
        row = _channel(body, name)
        assert row["config_status"] == "configured"
        assert row["missing_env"] == []
        assert row["inbound_configured"] is True
        assert row["outbound_configured"] is True


def test_status_never_returns_secret_values(settings: Settings, db_schema: None) -> None:
    """即使所有变量都填了，响应里也只能出现变量名，不能出现值。"""
    configured = {**FEISHU_ALL, **WECOM_ALL, **DINGTALK_ALL}
    with _client(_settings(settings, **configured)) as client:
        response = client.get(CHANNELS, headers=AUTH)

    raw = response.text
    for value in configured.values():
        if isinstance(value, str) and value:
            assert value not in raw, value

    # 出站失败也只回错误串，不回草稿正文。
    _seed_draft(settings, body="super-secret-body", outcome="failed")
    with _client(_settings(settings, **configured)) as client:
        failed = client.get(CHANNELS, headers=AUTH)
    assert "super-secret-body" not in failed.text


# --- 收发时间与近期错误 ---


def test_last_send_receive_and_recent_errors(settings: Settings, db_schema: None) -> None:
    _seed_draft(settings, channel="feishu", outcome="sent")
    _seed_draft(settings, channel="feishu", outcome="failed")

    received_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    errored_at = datetime(2026, 1, 3, 4, 5, 6, tzinfo=timezone.utc)
    with _client(_settings(settings)) as client:
        activity = client.app.state.container.channel_activity
        activity.record_receive("feishu", at=received_at)
        activity.record_error(
            "feishu",
            "turn_failed",
            "Bot paused\nretry later",
            at=errored_at,
        )
        response = client.get(CHANNELS, headers=AUTH)

    feishu = _channel(response.json(), "feishu")
    assert feishu["last_send_at"] is not None
    # mark_sent 用当前时间；只要求是合法的带时区 ISO 8601。
    sent = datetime.fromisoformat(feishu["last_send_at"])
    assert sent.tzinfo is not None
    assert datetime.now(timezone.utc) - sent < timedelta(minutes=5)

    assert feishu["last_receive_at"] == received_at.isoformat()

    errors = {(item["code"], item["source"]) for item in feishu["recent_errors"]}
    assert ("outbound_send_failed", "outbound") in errors
    assert ("turn_failed", "inbound") in errors
    outbound = next(item for item in feishu["recent_errors"] if item["source"] == "outbound")
    assert "upstream timed out" in outbound["message"]
    inbound = next(item for item in feishu["recent_errors"] if item["source"] == "inbound")
    assert inbound["message"] == "Bot paused retry later"
    # 最新的在前
    times = [item["time"] for item in feishu["recent_errors"]]
    assert times == sorted(times, reverse=True)


def test_failed_draft_counts_as_error_not_as_last_send(settings: Settings, db_schema: None) -> None:
    """发送失败不能冒充「最近成功发送」，但必须出现在近期错误里。"""
    _seed_draft(settings, channel="feishu", outcome="failed")
    with _client(_settings(settings)) as client:
        feishu = _channel(client.get(CHANNELS, headers=AUTH).json(), "feishu")

    assert feishu["last_send_at"] is None
    assert feishu["recent_errors"][0]["code"] == "outbound_send_failed"
    assert feishu["recent_errors"][0]["source"] == "outbound"


def test_other_channels_do_not_see_feishu_activity(settings: Settings, db_schema: None) -> None:
    with _client(_settings(settings)) as client:
        client.app.state.container.channel_activity.record_receive("feishu")
        response = client.get(CHANNELS, headers=AUTH)

    body = response.json()
    assert _channel(body, "feishu")["last_receive_at"] is not None
    assert _channel(body, "wecom")["last_receive_at"] is None
    assert _channel(body, "dingtalk")["last_receive_at"] is None


# --- 浏览器 MCP ---


class _Recorder:
    """记录探活调用的替身；按顺序返回预设结果或抛异常。"""

    def __init__(self, *results: Any) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self._results = list(results) or [ProbeOutcome(ok=True)]

    async def __call__(self, url: str, headers: dict[str, str]) -> ProbeOutcome:
        self.calls.append((url, dict(headers)))
        result = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        if isinstance(result, BaseException):
            raise result
        return result


def _browser_settings(
    settings: Settings,
    *,
    url: str = "http://playwright-mcp:8931/mcp",
    headers: dict[str, str] | None = None,
) -> Settings:
    server: dict[str, Any] = {"type": "http", "url": url}
    if headers is not None:
        server["headers"] = headers
    return _settings(
        settings,
        claude_extra_mcp_servers=json.dumps({"browser": server}),
    )


def test_browser_not_configured_does_not_probe(settings: Settings, db_schema: None) -> None:
    recorder = _Recorder()
    with _client(_settings(settings)) as client:
        client.app.state.container.browser_probe = BrowserStatusProbe(prober=recorder)
        response = client.get(BROWSER, headers=AUTH)

    assert recorder.calls == []
    body = response.json()["data"]
    assert body["configured"] is False
    assert body["ok"] is None
    assert body["url"] is None
    assert body["last_probe_at"] is None


def test_browser_configured_probes_and_reports_success(settings: Settings, db_schema: None) -> None:
    recorder = _Recorder(ProbeOutcome(ok=True, latency_ms=7, status_code=405))
    with _client(_browser_settings(settings)) as client:
        client.app.state.container.browser_probe = BrowserStatusProbe(prober=recorder)
        response = client.get(BROWSER, headers=AUTH)

    assert recorder.calls == [("http://playwright-mcp:8931/mcp", {})]
    body = response.json()["data"]
    assert body["configured"] is True
    assert body["server_name"] == "browser"
    assert body["type"] == "http"
    assert body["url"] == "http://playwright-mcp:8931/mcp"
    assert body["ok"] is True
    assert body["reachable"] is True
    assert body["status_code"] == 405
    assert body["latency_ms"] == 7
    assert body["error"] is None
    assert body["cached"] is False
    assert body["last_probe_at"] is not None
    assert body["timeout_seconds"] == 2.0
    assert body["cache_ttl_seconds"] == 30.0


def test_browser_probe_failure_is_reported(settings: Settings, db_schema: None) -> None:
    recorder = _Recorder(ProbeOutcome(ok=False, latency_ms=2001, error="timeout after 2s"))
    with _client(_browser_settings(settings)) as client:
        client.app.state.container.browser_probe = BrowserStatusProbe(prober=recorder)
        response = client.get(BROWSER, headers=AUTH)

    body = response.json()["data"]
    assert body["ok"] is False
    assert body["reachable"] is False
    assert body["status_code"] is None
    assert body["error"] == "timeout after 2s"


def test_browser_headers_are_sent_but_never_returned(settings: Settings, db_schema: None) -> None:
    """headers 会随探活请求发出（sidecar 需要鉴权），但响应绝不能回显。"""
    secret = "Bearer dummy-browser-token-should-not-leak"
    recorder = _Recorder(ProbeOutcome(ok=True, status_code=200))
    with _client(_browser_settings(settings, headers={"Authorization": secret})) as client:
        client.app.state.container.browser_probe = BrowserStatusProbe(prober=recorder)
        response = client.get(BROWSER, headers=AUTH)

    assert recorder.calls == [("http://playwright-mcp:8931/mcp", {"Authorization": secret})]
    assert secret not in response.text
    assert "Authorization" not in response.text


def test_browser_url_credentials_are_redacted(settings: Settings, db_schema: None) -> None:
    """URL 里若被塞了 userinfo / query token，响应只保留端点骨架。"""
    url = "http://alice:s3cr3t-password@browser.local:8931/mcp?api_key=query-secret"
    recorder = _Recorder(ProbeOutcome(ok=True, status_code=200))
    with _client(
        _settings(
            settings, claude_extra_mcp_servers=json.dumps({"browser": {"type": "http", "url": url}})
        )
    ) as client:
        client.app.state.container.browser_probe = BrowserStatusProbe(prober=recorder)
        response = client.get(BROWSER, headers=AUTH)

    assert response.json()["data"]["url"] == "http://browser.local:8931/mcp"
    assert "s3cr3t-password" not in response.text
    assert "query-secret" not in response.text


def test_browser_invalid_or_non_http_entry_is_not_configured(
    settings: Settings, db_schema: None
) -> None:
    """解析器拒绝 stdio / 非法 URL：状态接口按未配置处理，不探活。"""
    recorder = _Recorder()
    for raw in (
        json.dumps({"browser": {"type": "stdio", "command": "npx"}}),
        json.dumps({"browser": {"type": "http", "url": "ftp://example.com/mcp"}}),
        "not-json",
    ):
        with _client(_settings(settings, claude_extra_mcp_servers=raw)) as client:
            client.app.state.container.browser_probe = BrowserStatusProbe(prober=recorder)
            body = client.get(BROWSER, headers=AUTH).json()["data"]
        assert body["configured"] is False
    assert recorder.calls == []


def test_browser_probe_cache_avoids_repeated_network_calls() -> None:
    """30 秒窗口内命中缓存（成功与失败都缓存），窗口外重新探活。"""
    clock = [100.0]
    recorder = _Recorder(
        ProbeOutcome(ok=True, status_code=200),
        ProbeOutcome(ok=False, error="connection failed"),
    )
    probe = BrowserStatusProbe(prober=recorder, clock=lambda: clock[0])

    first = asyncio.run(probe.probe("http://browser/mcp"))
    assert first.cached is False
    assert len(recorder.calls) == 1

    clock[0] += 10
    second = asyncio.run(probe.probe("http://browser/mcp"))
    assert second.cached is True
    assert second.cache_age_seconds == 10
    assert second.outcome == first.outcome
    assert len(recorder.calls) == 1

    clock[0] += 21  # 总计 31 秒，超过 30 秒窗口
    third = asyncio.run(probe.probe("http://browser/mcp"))
    assert third.cached is False
    assert third.outcome.ok is False
    assert len(recorder.calls) == 2

    # 失败结果同样进缓存：窗口内的下一次不再打网络。
    clock[0] += 5
    fourth = asyncio.run(probe.probe("http://browser/mcp"))
    assert fourth.cached is True
    assert fourth.outcome.ok is False
    assert len(recorder.calls) == 2


def test_browser_default_probe_uses_two_second_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认探活必须带 2 秒超时；超时映射为 ok=false + timeout 摘要。"""
    captured: dict[str, Any] = {}

    class _HangingClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def __aenter__(self) -> _HangingClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, url: str, headers: dict[str, str]) -> httpx.Response:
            raise httpx.TimeoutException("simulated hang")

    monkeypatch.setattr(browser_status_module.httpx, "AsyncClient", _HangingClient)
    probe = BrowserStatusProbe()
    report = asyncio.run(probe.probe("http://browser/mcp"))

    assert captured["timeout"] == 2.0
    assert captured["follow_redirects"] is False
    assert report.outcome.ok is False
    assert report.outcome.error == "timeout after 2s"
