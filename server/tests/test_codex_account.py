"""ChatGPT 账号（Codex 订阅通道）的确定性测试。

不启动真实 codex、不访问 OpenAI：只验证解析、状态脱敏、auth.json 注入与 provider 配置。
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from flux.config import Settings
from flux.core.agent_runtime.codex_account import (
    CodexAccountService,
    codex_proxy_env,
    decode_jwt_claims,
    parse_device_auth_output,
    sync_account_auth,
)
from flux.core.agent_runtime.runtimes.base import RUNTIME_CODEX
from flux.core.agent_runtime.runtimes.cli import CliRuntime, _codex_config, _codex_provider_lines
from flux.errors import ConfigurationError

ACCOUNT_HOME = "account-home"

_DEVICE_AUTH_OUTPUT = """
Follow these steps to sign in with ChatGPT using device code authorization:

1. Open this link in your browser and sign in to your account:
   https://auth.openai.com/codex/device

2. Enter this one-time code (expires in 15 minutes):
   WXYZ-ABCDE

Device codes are a common phishing target. Never share this code.
"""


def _seg(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwt(payload: dict) -> str:
    return f"{_seg({'alg': 'none', 'typ': 'JWT'})}.{_seg(payload)}.signature"


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "env": "test",
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'codex-account.db'}",
        "codex_account_home": str(tmp_path / ACCOUNT_HOME),
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _write_auth(tmp_path: Path, *, plan: str = "plus", email: str = "user@example.com") -> Path:
    home = tmp_path / ACCOUNT_HOME
    home.mkdir(parents=True, exist_ok=True)
    auth = {
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _jwt(
                {
                    "email": email,
                    "exp": 1893456000,
                    "https://api.openai.com/auth": {
                        "chatgpt_plan_type": plan,
                        "chatgpt_account_id": "acc-123",
                    },
                }
            ),
            "access_token": "secret-access-token",
            "refresh_token": "secret-refresh-token",
            "account_id": "acc-123",
        },
        "last_refresh": "2026-10-26T00:00:00Z",
    }
    path = home / "auth.json"
    path.write_text(json.dumps(auth), encoding="utf-8")
    return path


def _runtime(settings: Settings) -> SimpleNamespace:
    """只带 `_inject_account_auth` 需要的字段：避免真起 Supervisor。"""
    return SimpleNamespace(_settings=settings, runtime_id=RUNTIME_CODEX)


# --- 解析 ---


def test_parse_device_auth_output_extracts_url_and_code() -> None:
    url, code = parse_device_auth_output(_DEVICE_AUTH_OUTPUT)
    assert url == "https://auth.openai.com/codex/device"
    assert code == "WXYZ-ABCDE"


def test_parse_device_auth_output_ignores_unrelated_links() -> None:
    url, code = parse_device_auth_output("see https://example.com/help for details")
    assert url == ""
    assert code == ""


def test_parse_device_auth_output_prefers_device_page_over_api_endpoint() -> None:
    text = (
        "error sending request for url "
        "(https://auth.openai.com/api/accounts/deviceauth/usercode)\n"
        "open https://auth.openai.com/codex/device\n"
    )
    url, _ = parse_device_auth_output(text)
    assert url == "https://auth.openai.com/codex/device"


def test_decode_jwt_claims_reads_payload_without_verification() -> None:
    claims = decode_jwt_claims(_jwt({"email": "a@b.c"}))
    assert claims["email"] == "a@b.c"
    assert decode_jwt_claims("not-a-jwt") == {}


# --- 状态 ---


def test_status_reports_account_but_never_leaks_tokens(tmp_path: Path) -> None:
    _write_auth(tmp_path)
    status = CodexAccountService(_settings(tmp_path)).status()

    assert status["logged_in"] is True
    assert status["auth_mode"] == "chatgpt"
    assert status["email"] == "user@example.com"
    assert status["plan"] == "plus"
    assert status["account_id"] == "acc-123"
    assert status["expires_at"] == "2030-01-01T00:00:00+00:00"

    dumped = json.dumps(status)
    assert "secret-access-token" not in dumped
    assert "secret-refresh-token" not in dumped
    assert "id_token" not in status


def test_status_without_auth_is_logged_out(tmp_path: Path) -> None:
    status = CodexAccountService(_settings(tmp_path)).status()
    assert status["logged_in"] is False
    assert status["email"] is None
    assert status["pending_login"] is None


def test_status_exposes_free_plan_for_ui_warning(tmp_path: Path) -> None:
    _write_auth(tmp_path, plan="free")
    assert CodexAccountService(_settings(tmp_path)).status()["plan"] == "free"


def test_status_survives_unreadable_auth_file(tmp_path: Path) -> None:
    home = tmp_path / ACCOUNT_HOME
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text("{not json", encoding="utf-8")
    status = CodexAccountService(_settings(tmp_path)).status()
    assert status["logged_in"] is False


# --- 注入 ---


def test_sync_account_auth_copies_file_with_owner_only_permissions(tmp_path: Path) -> None:
    source = _write_auth(tmp_path)
    target = tmp_path / "run" / "codex-home"

    assert sync_account_auth(source.parent, target) is True
    assert (target / "auth.json").read_bytes() == source.read_bytes()
    if os.name != "nt":
        assert (target / "auth.json").stat().st_mode & 0o777 == 0o600


def test_sync_account_auth_returns_false_when_not_logged_in(tmp_path: Path) -> None:
    assert sync_account_auth(tmp_path / "missing", tmp_path / "run" / "codex-home") is False
    assert not (tmp_path / "run" / "codex-home" / "auth.json").exists()


def test_inject_account_auth_copies_login_state_for_default_provider(tmp_path: Path) -> None:
    _write_auth(tmp_path)
    settings = _settings(tmp_path)
    home = tmp_path / "run" / "codex-home"

    CliRuntime._inject_account_auth(_runtime(settings), home)  # type: ignore[arg-type]
    assert (home / "auth.json").is_file()


def test_inject_account_auth_silently_skips_without_account(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    home = tmp_path / "run" / "codex-home"
    CliRuntime._inject_account_auth(_runtime(settings), home)  # type: ignore[arg-type]
    assert not (home / "auth.json").exists()


def test_inject_account_auth_requires_login_for_chatgpt_provider(tmp_path: Path) -> None:
    settings = _settings(tmp_path, codex_provider="chatgpt")
    with pytest.raises(ConfigurationError) as excinfo:
        CliRuntime._inject_account_auth(  # type: ignore[arg-type]
            _runtime(settings), tmp_path / "run" / "codex-home"
        )
    assert "ChatGPT" in str(excinfo.value)


# --- 代理 ---


def test_codex_proxy_env_empty_by_default(tmp_path: Path) -> None:
    assert codex_proxy_env(_settings(tmp_path)) == {}


def test_codex_proxy_env_sets_both_case_variants(tmp_path: Path) -> None:
    env = codex_proxy_env(_settings(tmp_path, codex_proxy="http://10.99.0.1:7890"))
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        assert env[key] == "http://10.99.0.1:7890"


# --- provider 配置 ---


def test_chatgpt_provider_writes_no_model_provider_block(tmp_path: Path) -> None:
    lines = _codex_provider_lines(
        _settings(tmp_path, codex_provider="chatgpt"), model="gpt-5.6-terra"
    )
    text = "\n".join(lines)
    assert 'model = "gpt-5.6-terra"' in text
    assert "model_provider" not in text
    assert "preferred_auth_method" not in text


def test_codex_config_for_chatgpt_keeps_mcp_block_and_no_provider(
    tmp_path: Path,
) -> None:
    config = _codex_config(
        "http://127.0.0.1:8000/mcp",
        "flux-token",
        settings=_settings(tmp_path, codex_provider="chatgpt"),
        run_id="run-1",
    )
    assert "[mcp_servers.flux]" in config
    assert "model_provider" not in config
    assert "env_key" not in config
