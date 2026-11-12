"""Agent 可用模型目录（对话页切换模型 / key 库模型选择用）。

模型的权威来源分两类：

1. 各 runtime 自己的原生配置，Flux 不臆造模型 id：
   - `claude-code`：内置 Runtime 只有一个配置项（`FLUX_ANTHROPIC_MODEL`），列表即它自己；
   - `codex`：若配置了 `FLUX_CODEX_MODEL_CATALOG`（codex 的 model_catalog_json），
     从该文件读 `models[].slug`；否则退回 `FLUX_CODEX_MODEL`；
   - `opencode`：读它自己的全局配置 `provider.<id>.models`，拼成 `<provider>/<model>`
     （用户配置的 Zen 免费模型因此能在手机/桌面端直接选）；
   - 其它 runtime：空列表（前端只显示当前模型，不提供切换）。
2. 密钥库（`api_keys`）里登记的 key + 模型；按 runtime 的协议兼容性过滤。

`POST /tasks/{id}/model`、`POST /tasks` 与各 runtime 的 Run 创建接口都用同一份目录做校验，
避免"前端能选、后端跑不了"。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flux.config import Settings
from flux.core.agent_runtime.runtimes.base import (
    DEFAULT_RUNTIME,
    RUNTIME_CLAUDE,
    RUNTIME_CODEX,
    RUNTIME_OPENCODE,
)

#: 密钥库允许的供应商类型（写库/校验用）
PROVIDER_DEEPSEEK = "deepseek"
PROVIDER_OPENAI = "openai"
PROVIDER_OPENAI_COMPATIBLE = "openai_compatible"
PROVIDER_ANTHROPIC = "anthropic"
KEY_PROVIDERS: tuple[str, ...] = (
    PROVIDER_DEEPSEEK,
    PROVIDER_OPENAI,
    PROVIDER_OPENAI_COMPATIBLE,
    PROVIDER_ANTHROPIC,
)

PROVIDER_TITLES: dict[str, str] = {
    PROVIDER_DEEPSEEK: "DeepSeek",
    PROVIDER_OPENAI: "OpenAI",
    PROVIDER_OPENAI_COMPATIBLE: "OpenAI 兼容",
    PROVIDER_ANTHROPIC: "Anthropic",
}

#: 各 runtime 能吃哪些供应商协议的 key。协议层不兼容的一律不展示、不接收。
_RUNTIME_PROVIDERS: dict[str, frozenset[str]] = {
    # claude-code 走 Anthropic Messages 协议（Base URL 可指向 OpenClaw 等网关）
    RUNTIME_CLAUDE: frozenset({PROVIDER_ANTHROPIC}),
    # codex 的第三方供应商块只讲 OpenAI wire api（responses / chat）
    RUNTIME_CODEX: frozenset({PROVIDER_DEEPSEEK, PROVIDER_OPENAI, PROVIDER_OPENAI_COMPATIBLE}),
    # opencode 原生支持多家供应商
    RUNTIME_OPENCODE: frozenset(KEY_PROVIDERS),
}


@dataclass(frozen=True, slots=True)
class KeyEntry:
    """密钥库里一条 key 的安全投影（不含明文/密文）。"""

    id: str
    name: str
    provider: str
    base_url: str
    wire_api: str
    models: tuple[str, ...]
    key_hint: str
    enabled: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "provider_title": PROVIDER_TITLES.get(self.provider, self.provider),
            "base_url": self.base_url,
            "wire_api": self.wire_api,
            "models": list(self.models),
            "key_hint": self.key_hint,
            "enabled": self.enabled,
        }


@dataclass(frozen=True, slots=True)
class KeyModelOption:
    """key 库中一个可选模型（供前端/校验使用）。"""

    key_id: str
    key_name: str
    provider: str
    model: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key_id": self.key_id,
            "key_name": self.key_name,
            "provider": self.provider,
            "provider_title": PROVIDER_TITLES.get(self.provider, self.provider),
            "model": self.model,
        }


def _catalog_slugs(raw_path: str) -> list[str]:
    path_text = (raw_path or "").strip()
    if not path_text:
        return []
    path = Path(path_text).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("models") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    slugs: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if isinstance(slug, str) and slug.strip() and slug.strip() not in slugs:
            slugs.append(slug.strip())
    return slugs


def _opencode_model_ids(raw_path: str) -> list[str]:
    """从 opencode 全局配置取用户已配置的模型 id（`<provider>/<model>`）。

    opencode 的模型清单由它自己的全局配置维护（provider 的 models 键，含 Zen 免费模型）；
    Flux 只读模型 id 并把顶层 `model`（opencode 的默认）置顶，供应商的 options/密钥一律不读。
    """
    path = Path((raw_path or "").strip()).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    providers = data.get("provider") if isinstance(data, dict) else None
    if not isinstance(providers, dict):
        return []
    ids: list[str] = []
    for provider_id, provider in providers.items():
        models = provider.get("models") if isinstance(provider, dict) else None
        if not isinstance(models, dict):
            continue
        for model_id in models:
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            full = f"{provider_id}/{model_id.strip()}"
            if full not in ids:
                ids.append(full)
    default = data.get("model") if isinstance(data, dict) else None
    if isinstance(default, str) and default.strip() in ids:
        ids.remove(default.strip())
        ids.insert(0, default.strip())
    return ids


def models_for_runtime(settings: Settings, runtime: str | None) -> list[str]:
    """该 runtime 支持的原生模型 id 列表（顺序即展示顺序，第一项是默认值）。"""
    name = (runtime or DEFAULT_RUNTIME).strip().lower()
    if name == RUNTIME_CLAUDE:
        model = (settings.anthropic_model or "").strip()
        return [model] if model else []
    if name == RUNTIME_CODEX:
        configured = (settings.codex_model or "").strip()
        catalog = _catalog_slugs(settings.codex_model_catalog)
        if configured:
            return [configured, *(m for m in catalog if m != configured)]
        return catalog
    if name == RUNTIME_OPENCODE:
        # 模型清单由 opencode 全局配置持有；列表首项 = 它的默认模型。
        return _opencode_model_ids(settings.opencode_config_file)
    return []


def default_model_for_runtime(settings: Settings, runtime: str | None) -> str:
    """未设置任务级覆盖时该 runtime 实际会用的模型（空串 = runtime 自己的默认）。"""
    name = (runtime or DEFAULT_RUNTIME).strip().lower()
    if name == RUNTIME_CLAUDE:
        return (settings.anthropic_model or "").strip()
    if name == RUNTIME_CODEX:
        configured = (settings.codex_model or "").strip()
        if configured:
            return configured
        catalog = _catalog_slugs(settings.codex_model_catalog)
        return catalog[0] if catalog else ""
    if name == RUNTIME_OPENCODE:
        models = _opencode_model_ids(settings.opencode_config_file)
        return models[0] if models else ""
    return ""


def supports_provider(runtime: str | None, provider: str) -> bool:
    """该 runtime 的协议层能否使用这个供应商的 key。"""
    name = (runtime or DEFAULT_RUNTIME).strip().lower()
    return provider in _RUNTIME_PROVIDERS.get(name, frozenset())


def default_base_url(settings: Settings, provider: str) -> str:
    """key 未填 Base URL 时的默认地址（兼容端点必须自己填）。"""
    if provider == PROVIDER_DEEPSEEK:
        return (settings.deepseek_base_url or "").strip()
    if provider == PROVIDER_OPENAI:
        return (settings.openai_base_url or "").strip()
    if provider == PROVIDER_ANTHROPIC:
        return (settings.anthropic_base_url or "").strip()
    return ""


def vault_models_for_runtime(
    entries: Iterable[KeyEntry],
    runtime: str | None,
    *,
    enabled_only: bool = True,
) -> list[KeyModelOption]:
    """把密钥库条目摊平成该 runtime 可选的模型列表（供应商协议不兼容的跳过）。"""
    options: list[KeyModelOption] = []
    for entry in entries:
        if enabled_only and not entry.enabled:
            continue
        if not supports_provider(runtime, entry.provider):
            continue
        for model in entry.models:
            options.append(
                KeyModelOption(
                    key_id=entry.id,
                    key_name=entry.name,
                    provider=entry.provider,
                    model=model,
                )
            )
    return options


__all__ = [
    "KEY_PROVIDERS",
    "PROVIDER_ANTHROPIC",
    "PROVIDER_DEEPSEEK",
    "PROVIDER_OPENAI",
    "PROVIDER_OPENAI_COMPATIBLE",
    "PROVIDER_TITLES",
    "KeyEntry",
    "KeyModelOption",
    "default_base_url",
    "default_model_for_runtime",
    "models_for_runtime",
    "supports_provider",
    "vault_models_for_runtime",
]
