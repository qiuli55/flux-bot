"""契约：每个 Agent runtime 必须显式声明是否原生逐 token 流式。

用户要求：
- 现有三个 Agent 尽可能原生逐 token，不许用「整段生成 → 后端切块回放」冒充流式；
- 不支持原生流式的 Agent 仍可正常绑定和使用，但档案必须带 `native_streaming=False`
  供客户端标注。

因此新增 runtime 必须显式登记取值（True/False），漏登记会被契约测试拦住。
"""

from __future__ import annotations

import pytest

from flux.config import Settings
from flux.core.agent_runtime.manager import AgentManager, AgentSpec
from flux.core.agent_runtime.runtimes.base import (
    DEFAULT_RUNTIME_LIMITS,
    KNOWN_RUNTIMES,
    RUNTIME_CODEX,
    RUNTIME_DSH,
    RUNTIME_NATIVE_STREAMING,
    RUNTIME_OPENCODE,
    runtime_limits,
)
from flux.core.agent_runtime.runtimes.dsh_runtime import DshRuntime
from flux.enums import AgentRole


def test_every_known_runtime_declares_streaming_explicitly() -> None:
    assert set(RUNTIME_NATIVE_STREAMING) == set(KNOWN_RUNTIMES)


@pytest.mark.parametrize("runtime", [RUNTIME_DSH, RUNTIME_CODEX, RUNTIME_OPENCODE])
def test_current_runtimes_are_native_streaming(runtime: str) -> None:
    assert RUNTIME_NATIVE_STREAMING[runtime] is True


def test_builtin_dsh_runtime_advertises_native_streaming() -> None:
    # 内置 Agent 的增量由 llm_stream_tap 从模型 SSE 旁路取得
    assert DshRuntime.runtime_id == RUNTIME_DSH
    assert DshRuntime.native_streaming is True


def test_all_known_runtimes_pass_validation() -> None:
    for runtime in KNOWN_RUNTIMES:
        AgentManager._validate_runtime(runtime)


def test_unregistered_runtime_falls_back_to_the_generic_limits() -> None:
    """未来接入的 runtime 不登记时限也能跑：回落通用默认，而不是 KeyError。"""
    assert runtime_limits("future-runtime") == DEFAULT_RUNTIME_LIMITS


@pytest.mark.parametrize("runtime", KNOWN_RUNTIMES)
def test_settings_overrides_never_drop_runtime_capabilities(runtime: str) -> None:
    """Settings 只收敛时限，不得抹掉 runtime 自己声明的能力（异步回合一类的开关）。"""
    declared = runtime_limits(runtime)
    limits = Settings(agent_turn_idle_timeout_seconds=42.0).agent_limits(runtime)
    assert limits.idle_timeout_seconds == 42.0
    assert limits.async_turn is declared.async_turn
    assert limits.reusable_process is declared.reusable_process


def test_process_reuse_can_be_switched_off_globally() -> None:
    """进程复用是通用能力：总开关关闭后，即使 runtime 声明了也不进池。"""
    for runtime in (RUNTIME_OPENCODE, RUNTIME_CODEX):
        assert runtime_limits(runtime).reusable_process is True
        limits = Settings(agent_process_reuse=False).agent_limits(runtime)
        assert limits.reusable_process is False


@pytest.mark.parametrize("runtime", [*KNOWN_RUNTIMES, "future-runtime"])
def test_context_budget_is_a_global_knob(runtime: str) -> None:
    """上下文预算是通用能力口径：所有 runtime（含未登记的）都拿到同一个预算值。

    真正怎么压缩由各 runtime 自己实现；没实现的直接忽略，不影响接入。
    """
    assert runtime_limits(runtime).context_compact_tokens == 0
    limits = Settings(agent_context_compact_tokens=5000).agent_limits(runtime)
    assert limits.context_compact_tokens == 5000


@pytest.mark.parametrize("runtime", KNOWN_RUNTIMES)
def test_context_budget_can_be_switched_off(runtime: str) -> None:
    assert (
        Settings(agent_context_compact_tokens=0).agent_limits(runtime).context_compact_tokens == 0
    )


async def test_non_streaming_agent_is_allowed_and_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不能原生流式的 runtime 也能创建 Agent，只是档案要标注 native_streaming=False。"""
    monkeypatch.setitem(RUNTIME_NATIVE_STREAMING, RUNTIME_CODEX, False)
    AgentManager._validate_runtime(RUNTIME_CODEX)

    handle = await AgentManager().create(
        AgentSpec(name="legacy-agent", role=AgentRole.DEVELOPER, runtime=RUNTIME_CODEX)
    )
    payload = handle.to_dict()
    spec = payload["spec"]
    assert isinstance(spec, dict)
    assert spec["native_streaming"] is False


async def test_streaming_agent_is_marked_true() -> None:
    handle = await AgentManager().create(
        AgentSpec(name="native-agent", role=AgentRole.DEVELOPER, runtime=RUNTIME_OPENCODE)
    )
    spec = handle.to_dict()["spec"]
    assert isinstance(spec, dict)
    assert spec["native_streaming"] is True
