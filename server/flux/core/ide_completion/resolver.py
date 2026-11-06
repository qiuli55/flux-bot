"""补全的模型通道解析（AI_COMPLETION_SPEC §5.1 / §5.3）。

优先级（从高到低）：

1. 显式 `key_id`（可带模型；不带时取该密钥登记的第一个模型）；
2. `<keyId>::<model>` 或裸 `<model>`：先在密钥库里按模型名找可用密钥；
3. `follow-agent`：读当前 Agent 选择（`GET/PUT /agents/active`），带上其中的模型覆盖；
4. 未配置任何偏好：取密钥库第一条启用密钥的第一个模型；
5. 再退回平台环境变量（DeepSeek → OpenAI → Anthropic）。

全部落空时抛 `ConfigurationError` 并给出配置指引——绝不静默降级到一个假模型，
否则用户看到的只是"补全一直不出来"。
"""

from __future__ import annotations

from dataclasses import dataclass

from flux.config import Settings
from flux.core.agent_runtime.manager import AgentManager
from flux.core.agent_runtime.runtimes.base import ModelChannel
from flux.core.ide_completion.prefs import (
    FOLLOW_AGENT,
    ActiveAgentSelection,
    CompletionPrefsStore,
)
from flux.core.key_vault.service import KeyVaultService
from flux.errors import BadRequestError, ConfigurationError

#: 平台环境变量通道的 key_id 占位（不是数据库里的 UUID）
PLATFORM_KEY_ID = "platform"

_MODEL_PROVIDER_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("claude",), "anthropic"),
    (("deepseek",), "deepseek"),
    (("gpt", "o1", "o3", "o4", "chatgpt"), "openai"),
)

_NO_CREDENTIAL_HINT = (
    "先在 Flux 的「密钥」里添加一个可用的 API Key（或设置 FLUX_DEEPSEEK_API_KEY / "
    "FLUX_OPENAI_API_KEY / FLUX_ANTHROPIC_API_KEY），再重试补全"
)


@dataclass(frozen=True, slots=True)
class CompletionTarget:
    """一次补全实际使用的通道。"""

    channel: ModelChannel
    model: str
    provider: str
    key_id: str | None
    #: 解析来源：explicit-key / explicit-model / agent / default
    source: str

    def to_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "provider": self.provider,
            "key_id": self.key_id,
            "source": self.source,
            "label": self.channel.label,
        }


def provider_hint(model: str) -> str | None:
    lowered = (model or "").strip().lower()
    for prefixes, provider in _MODEL_PROVIDER_HINTS:
        if lowered.startswith(prefixes):
            return provider
    return None


class CompletionModelResolver:
    """把补全请求里的 model / key_id / agent_id 解析成可直接调用的通道。"""

    def __init__(
        self,
        *,
        settings: Settings,
        key_vault: KeyVaultService,
        agents: AgentManager,
        prefs: CompletionPrefsStore,
    ) -> None:
        self._settings = settings
        self._key_vault = key_vault
        self._agents = agents
        self._prefs = prefs

    @property
    def prefs(self) -> CompletionPrefsStore:
        return self._prefs

    async def resolve(
        self,
        *,
        model: str | None = None,
        key_id: str | None = None,
        agent_id: str | None = None,
    ) -> CompletionTarget:
        wanted = (model or "").strip()
        if wanted.lower() == FOLLOW_AGENT:
            wanted = ""

        # 请求没点名模型/密钥时，偏好里保存的模型就是默认通道；
        # 只有偏好是 follow-agent 才继续走下面的 Agent 解析。
        if not wanted and not key_id:
            stored = str(self._prefs.snapshot().get("model") or "").strip()
            if stored and stored.lower() != FOLLOW_AGENT:
                wanted = stored

        # 0. 显式 agent_id：先确认档案存在（不存在直接 404，而不是悄悄用别的模型）
        handle = None
        if agent_id:
            handle = self._agents.get(agent_id)

        # 1. 显式 key_id
        if key_id:
            return await self._from_key(key_id, wanted)

        # 2. `<keyId>::<model>`
        if "::" in wanted:
            raw_key, raw_model = wanted.split("::", 1)
            if raw_key.strip():
                return await self._from_key(raw_key.strip(), raw_model.strip())

        # 3. 显式模型名
        if wanted:
            return await self._from_model(wanted)

        # 4. follow-agent：当前 Agent 选择里的模型覆盖
        selection = self._prefs.active_agent()
        if handle is not None:
            if selection.agent_id == handle.id_str and (selection.model or selection.key_id):
                return await self.resolve(model=selection.model, key_id=selection.key_id)
            return await self._default(source=f"agent:{handle.id_str}")
        if selection.model or selection.key_id:
            return await self.resolve(model=selection.model, key_id=selection.key_id)

        # 5. 什么都没有：密钥库 → 平台环境变量
        return await self._default(source="default")

    # --- 各条解析路径 ---

    async def _from_key(self, key_id: str, model: str) -> CompletionTarget:
        row = await self._key_vault.get(key_id)
        if not row.enabled:
            raise BadRequestError(f"密钥 {row.name!r} 已停用", details={"key_id": str(row.id)})
        models = [str(item).strip() for item in (row.models or []) if str(item).strip()]
        selected = model or (models[0] if models else "")
        if not selected:
            raise BadRequestError(
                f"密钥 {row.name!r} 没有登记任何模型",
                details={"key_id": str(row.id), "hint": "在密钥编辑里补充可用模型"},
            )
        if models and selected not in models:
            raise BadRequestError(
                f"密钥 {row.name!r} 没有登记模型 {selected}",
                details={"key_id": str(row.id), "model": selected, "allowed": models},
            )
        channel = await self._key_vault.channel_for_key(str(row.id), selected)
        return CompletionTarget(
            channel=channel,
            model=selected,
            provider=channel.provider,
            key_id=str(row.id),
            source="explicit-key",
        )

    async def _from_model(self, model: str) -> CompletionTarget:
        hint = provider_hint(model)
        entries = await self._key_vault.catalog_entries(enabled_only=True)
        matches = [entry for entry in entries if model in (entry.models or ())]
        if matches:
            # 多把密钥登记了同名模型时，优先协议与模型前缀一致的那把
            matches.sort(key=lambda entry: (entry.provider != hint, entry.name))
            entry = matches[0]
            channel = await self._key_vault.channel_for_key(entry.id, model)
            return CompletionTarget(
                channel=channel,
                model=model,
                provider=channel.provider,
                key_id=entry.id,
                source="explicit-model",
            )

        channel = self._platform_channel(hint, model) if hint else None
        if channel is not None:
            return CompletionTarget(
                channel=channel,
                model=model,
                provider=channel.provider,
                key_id=None,
                source="explicit-model",
            )
        raise ConfigurationError(
            f"模型 {model!r} 没有可用的调用通道",
            details={
                "model": model,
                "hint": (
                    "该模型既不在密钥库登记的模型里，也没有对应的平台 API Key；"
                    "请添加密钥或换一个模型"
                ),
            },
        )

    async def _default(self, *, source: str) -> CompletionTarget:
        entries = await self._key_vault.catalog_entries(enabled_only=True)
        for entry in entries:
            models = [str(item).strip() for item in entry.models if str(item).strip()]
            if not models:
                continue
            channel = await self._key_vault.channel_for_key(entry.id, models[0])
            return CompletionTarget(
                channel=channel,
                model=models[0],
                provider=channel.provider,
                key_id=entry.id,
                source=source,
            )

        for provider in ("deepseek", "openai", "anthropic"):
            channel = self._platform_channel(provider, None)
            if channel is not None:
                model = self._platform_model(provider)
                return CompletionTarget(
                    channel=channel,
                    model=model,
                    provider=provider,
                    key_id=None,
                    source=source,
                )
        raise ConfigurationError(
            "没有任何可用于补全的模型通道",
            details={"hint": _NO_CREDENTIAL_HINT},
        )

    # --- 平台环境变量 ---

    def _platform_model(self, provider: str) -> str:
        if provider == "deepseek":
            return self._settings.deepseek_model
        if provider == "openai":
            return self._settings.openai_model
        return self._settings.anthropic_model

    def _platform_channel(self, provider: str | None, model: str | None) -> ModelChannel | None:
        if provider == "deepseek" and self._settings.deepseek_api_key:
            return ModelChannel(
                provider="deepseek",
                base_url=self._settings.deepseek_base_url,
                api_key=self._settings.deepseek_api_key,
                key_id=PLATFORM_KEY_ID,
                label="DeepSeek（平台配置）",
            )
        if provider == "openai" and self._settings.openai_api_key:
            return ModelChannel(
                provider="openai",
                base_url=self._settings.openai_base_url,
                api_key=self._settings.openai_api_key,
                key_id=PLATFORM_KEY_ID,
                label="OpenAI（平台配置）",
            )
        if provider == "anthropic" and self._settings.anthropic_api_key:
            return ModelChannel(
                provider="anthropic",
                base_url=self._settings.anthropic_base_url,
                api_key=self._settings.anthropic_api_key,
                key_id=PLATFORM_KEY_ID,
                label="Anthropic（平台配置）",
            )
        return None

    # --- 供 GET 接口展示 ---

    async def describe_active(self) -> dict[str, object]:
        """`GET/PUT /agents/active` 的响应：当前选择 + 解析结果 + 档案信息。"""
        data = await self.describe()
        for key in ("enabled", "model", "debounce_ms", "max_lines"):
            data.pop(key, None)
        return data

    async def describe(self) -> dict[str, object]:
        """偏好 + 当前 Agent 的完整快照，并尽力给出实际会用的模型。"""
        data = self._prefs.snapshot()
        selection = ActiveAgentSelection.from_mapping(data.get("active_agent"))
        try:
            target = await self.resolve(agent_id=selection.agent_id)
            data["resolved"] = target.to_dict()
        except Exception as exc:  # noqa: BLE001 - 展示接口不因为没配密钥而整体报错
            data["resolved"] = {
                "model": None,
                "provider": None,
                "key_id": None,
                "source": None,
                "error": getattr(exc, "message", str(exc)),
                "code": getattr(exc, "code", None),
            }
        if selection.agent_id:
            try:
                handle = self._agents.get(selection.agent_id)
                data["active_agent"]["resolved"] = {
                    "id": handle.id_str,
                    "name": handle.spec.name,
                    "runtime": handle.spec.runtime,
                }
            except Exception:  # noqa: BLE001 - 档案被删掉时保留原始选择即可
                pass
        return data


__all__ = [
    "PLATFORM_KEY_ID",
    "CompletionModelResolver",
    "CompletionTarget",
    "provider_hint",
]
