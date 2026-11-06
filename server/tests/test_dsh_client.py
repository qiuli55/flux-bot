"""DSH 客户端单元测试（集成方案 §18 Phase 1）。

全部用 fake harness，不启动真实 runtime 子进程、不调用 DeepSeek API。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from deepseek_harness import Notification, RunResult, SdkProtocolError
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from flux.config import Settings
from flux.core.agent_runtime import platforms
from flux.core.agent_runtime.dsh_client import (
    MCP_PLUGIN_NAME,
    MCP_TOKEN_SCOPES,
    TERMINAL_STATUSES,
    DshRun,
    FluxDshClient,
)
from flux.core.agent_runtime.dsh_events import to_flux_event
from flux.core.agent_runtime.manager import AgentManager
from flux.core.agent_runtime.protocol import RUNTIME_BOOTSTRAP, compose_instruction
from flux.core.agent_runtime.repository import AgentRepository
from flux.core.event.bus import EventBus, Events
from flux.core.mcp.auth import AgentTokenService
from flux.core.mcp.tools import CORE_TOOL_NAMES, FORBIDDEN_TOOL_NAMES
from flux.db.session import create_engine, create_session_factory
from flux.enums import Capability, DshRunStatus
from flux.errors import AuthenticationError, ConfigurationError, NotFoundError
from flux.main import create_app
from flux.models import Base
from flux.version import VERSION
from tests.fakes import (
    DshBehavior,
    FakeDshHarness,
    FakeDshHarnessFactory,
    default_dsh_behavior,
)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    """启用态的测试配置：DSH_HOME / 工作区落在 tmp_path 下。

    这些用例只验证 Run 生命周期，不接 MCP：默认关掉注入，避免起 Run 时要求令牌服务。
    MCP 注入本身由本文件末尾的专项用例覆盖。
    """
    base = Settings(
        env="test",
        log_level="WARNING",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'dsh.db'}",
        # 显式关掉工作区根：否则会从本机 .env（FLUX_WORKSPACE_ROOT）读到真实路径，
        # 让"未配置 workspace_root 时回退 dsh_workspace"的断言失去意义。
        workspace_root=None,
        openai_api_key=None,
        anthropic_api_key=None,
        deepseek_api_key=None,
        default_provider="local",
        dsh_enabled=True,
        dsh_home=str(tmp_path / "dsh-home"),
        dsh_workspace=str(tmp_path / "dsh-ws"),
        dsh_mcp_enabled=False,
    )
    return base.model_copy(update=overrides) if overrides else base


async def _wait_terminal(client: FluxDshClient, run_id: str, timeout: float = 3.0) -> DshRun:
    """轮询到 Run 进入终态，避免用固定 sleep 造成 flaky。"""
    deadline = time.monotonic() + timeout
    run = client.get_run(run_id)
    while run.status not in TERMINAL_STATUSES and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return run


def _failing_behavior(
    harness: FakeDshHarness,
    instruction: str,
    session_id: str | None,
    on_notification: object,
) -> RunResult:
    raise SdkProtocolError("协议错误")


def _blocking_behavior(
    harness: FakeDshHarness,
    instruction: str,
    session_id: str | None,
    on_notification: object,
) -> RunResult:
    """阻塞到收到 session/cancel 再返回，用来验证中断路径。"""
    harness.cancel_event.wait(timeout=5)
    sid = session_id or "session"
    return RunResult(
        session_id=sid,
        final_response="",
        finish_reason="cancelled",
        events=[],
        notifications=[],
    )


async def test_start_run_completes_and_maps_events(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    bus = EventBus()
    factory = FakeDshHarnessFactory()
    client = FluxDshClient(settings, bus=bus, harness_factory=factory)

    run = await client.start_run("写一个加法函数", session_id="s-1")
    # P2-15：起 Run 只登记（PENDING），实际执行在后台协程里推进
    assert run.status is DshRunStatus.PENDING
    assert run.session_id == "s-1"
    assert run.started_at is not None
    # 批次①：Run 快照里的 instruction 保持用户原文，Bootstrap 只加在交给 harness 的那份上
    assert run.instruction == "写一个加法函数"

    finished = await _wait_terminal(client, run.run_id)
    assert finished.status is DshRunStatus.COMPLETED
    # 替身回显的是 harness 实际收到的输入：Bootstrap + 原文（注入发生在 harness 边界）
    assert finished.final_response == f"echo: {compose_instruction('写一个加法函数')}"
    assert finished.finish_reason == "completed"
    assert finished.finished_at is not None
    assert finished.duration_seconds is not None and finished.duration_seconds >= 0

    # 通知被压扁成 Flux 事件体，并带上 run_id 关联
    assert finished.events[0]["event_type"] == "assistant/message"
    assert finished.events[0]["text"] == f"echo: {compose_instruction('写一个加法函数')}"
    assert finished.events[0]["run_id"] == run.run_id
    assert finished.events[1]["event_type"] == "turn/end"
    assert finished.events[1]["finish_reason"] == "completed"

    # run 结束后 harness 被关闭并移出活跃表，且看护器已把该 Run 落定为终态
    assert factory.created[0].closed is True
    assert client.supervisor.is_settled(run.run_id) is True

    names = [name for name, _ in bus.history]
    assert Events.DSH_RUN_STARTED in names
    assert Events.DSH_RUN_COMPLETED in names
    client.close()


async def test_start_run_defaults_session_id_from_run_id(tmp_path: Path) -> None:
    client = FluxDshClient(_settings(tmp_path), harness_factory=FakeDshHarnessFactory())
    run = await client.start_run("hi")
    assert run.session_id == f"flux-{run.run_id}"
    await _wait_terminal(client, run.run_id)
    client.close()


async def test_managed_run_prompt_starts_with_bootstrap(tmp_path: Path) -> None:
    """批次①验收：交给 harness 的第一份上下文以 Bootstrap 起始，Run 快照保持用户原文。

    收口点在 `_execute` → `harness.run` 边界，任务路径（tasks.py）与 DSH API 路径
    （dsh.py）共用同一次注入，因此这里的 prompt 形态即两条链路共同的形态。
    """
    captured: list[str] = []

    def behavior(
        harness: FakeDshHarness,
        instruction: str,
        session_id: str | None,
        on_notification: Callable[[Notification], None] | None,
    ) -> RunResult:
        captured.append(instruction)
        return default_dsh_behavior(harness, instruction, session_id, on_notification)

    client = FluxDshClient(_settings(tmp_path), harness_factory=FakeDshHarnessFactory(behavior))
    run = await client.start_run("修好登录页", session_id="s-b")
    finished = await _wait_terminal(client, run.run_id)

    assert finished.status is DshRunStatus.COMPLETED
    assert captured == [f"{RUNTIME_BOOTSTRAP}\n\n修好登录页"]
    # run.instruction 保持用户原文：快照与 API 响应都不被 Bootstrap 污染
    assert finished.instruction == "修好登录页"
    assert finished.to_dict()["instruction"] == "修好登录页"
    client.close()


async def test_run_failure_maps_to_failed(tmp_path: Path) -> None:
    bus = EventBus()
    client = FluxDshClient(
        _settings(tmp_path), bus=bus, harness_factory=FakeDshHarnessFactory(_failing_behavior)
    )
    run = await client.start_run("boom", session_id="s-f")

    finished = await _wait_terminal(client, run.run_id)
    assert finished.status is DshRunStatus.FAILED
    assert finished.error == "协议错误"
    assert finished.final_response == ""
    assert finished.finished_at is not None
    assert finished.duration_seconds is not None

    names = [name for name, _ in bus.history]
    assert Events.DSH_RUN_FAILED in names
    client.close()


def _error_behavior(kind: str) -> DshBehavior:
    """运行库"正常返回但结论是失败"：SDK 不抛异常，失败写在 finish_reason 里。"""

    def behavior(
        harness: FakeDshHarness,
        instruction: str,
        session_id: str | None,
        on_notification: Callable[[Notification], None] | None,
    ) -> RunResult:
        sid = session_id or "session"
        end = {"type": "turn/end", "data": {"reason": {"kind": kind}}}
        if on_notification is not None:
            on_notification(
                Notification(method="session.event", payload={"sessionId": sid, "event": end})
            )
        return RunResult(
            session_id=sid,
            final_response="",
            finish_reason=kind,
            events=[end],
            notifications=[],
        )

    return behavior


@pytest.mark.parametrize(
    ("kind", "expected_status", "expected_event"),
    [
        ("error", DshRunStatus.FAILED, Events.DSH_RUN_FAILED),
        ("failed", DshRunStatus.FAILED, Events.DSH_RUN_FAILED),
        ("timeout", DshRunStatus.TIMEOUT, Events.DSH_RUN_TIMEOUT),
        ("interrupted", DshRunStatus.INTERRUPTED, Events.DSH_RUN_INTERRUPTED),
        ("aborted", DshRunStatus.INTERRUPTED, Events.DSH_RUN_INTERRUPTED),
        ("cancelled", DshRunStatus.CANCELLED, Events.DSH_RUN_CANCELLED),
    ],
)
async def test_error_finish_reason_does_not_map_to_completed(
    tmp_path: Path, kind: str, expected_status: DshRunStatus, expected_event: str
) -> None:
    """回归（P1）：运行库以失败类 finish_reason 收尾时，Run 不能落成 COMPLETED。

    现场：Agent 实际失败了，但 `_execute` 只看有没有抛异常，把 error 当成功 —— 任务在
    UI 上显示"已完成"，用户误以为改动已产出（NEXT_TEST_CLOSURE §14「P1 Critical」判定项）。
    """
    bus = EventBus()
    client = FluxDshClient(
        _settings(tmp_path), bus=bus, harness_factory=FakeDshHarnessFactory(_error_behavior(kind))
    )
    run = await client.start_run("会失败的活", session_id=f"s-{kind}")

    finished = await _wait_terminal(client, run.run_id)
    assert finished.status is expected_status
    assert finished.finish_reason == kind
    assert finished.error is not None and "Agent" in finished.error

    names = [name for name, _ in bus.history]
    assert expected_event in names
    assert Events.DSH_RUN_COMPLETED not in names
    client.close()


async def test_error_finish_reason_surfaces_upstream_detail(tmp_path: Path) -> None:
    """额度类错误要把上游原文带出来：只说"执行出错"，用户不知道该充值还是换模型。"""
    detail = "Insufficient Balance (request_id: abc-123)"

    def behavior(
        harness: FakeDshHarness,
        instruction: str,
        session_id: str | None,
        on_notification: Callable[[Notification], None] | None,
    ) -> RunResult:
        sid = session_id or "session"
        end = {
            "type": "turn/end",
            "data": {"reason": {"kind": "error", "error": {"message": detail, "code": "QUOTA"}}},
        }
        if on_notification is not None:
            on_notification(
                Notification(method="session.event", payload={"sessionId": sid, "event": end})
            )
        return RunResult(
            session_id=sid,
            final_response="",
            finish_reason="error",
            events=[end],
            notifications=[],
        )

    client = FluxDshClient(_settings(tmp_path), harness_factory=FakeDshHarnessFactory(behavior))
    run = await client.start_run("会被额度挡住", session_id="s-quota")

    finished = await _wait_terminal(client, run.run_id)
    assert finished.status is DshRunStatus.FAILED
    assert finished.upstream_error == detail
    assert finished.events[0]["error_detail"] == detail
    assert finished.error is not None
    assert "模型额度或鉴权异常" in finished.error
    assert detail in finished.error
    client.close()


class _InitTimeoutHarness(FakeDshHarness):
    """初始化阶段必超时的替身：模拟 runtime 迟迟不就绪（黑洞 MCP / Provider 无响应）。"""

    def start(self) -> None:
        raise TimeoutError(
            "initialize timed out waiting for DeepSeek Harness runtime\nselected dsh profile 'sdk'"
        )


async def test_init_timeout_maps_to_startup_timeout(tmp_path: Path) -> None:
    """回归（UI-503 现场）：DSH 初始化超时不能落成通用失败 + SDK 英文原文。

    initialize 超时（上限 dsh_init_timeout_seconds）说明 Agent 没启动成功，
    应落 TIMEOUT/startup 并给中文原因，UI 才能向用户说明这是"启动超时"。
    """
    bus = EventBus()
    created: list[_InitTimeoutHarness] = []

    def factory(**kwargs: Any) -> _InitTimeoutHarness:
        harness = _InitTimeoutHarness(default_dsh_behavior, **kwargs)
        created.append(harness)
        return harness

    client = FluxDshClient(_settings(tmp_path), bus=bus, harness_factory=factory)
    run = await client.start_run("初始化超时", session_id="s-t")

    finished = await _wait_terminal(client, run.run_id)
    assert finished.status is DshRunStatus.TIMEOUT
    assert finished.timeout_kind == "startup"
    assert finished.error is not None and "启动超时" in finished.error
    assert created[0].closed is True

    names = [name for name, _ in bus.history]
    assert Events.DSH_RUN_TIMEOUT in names
    client.close()


async def test_cancel_marks_run_cancelled(tmp_path: Path) -> None:
    bus = EventBus()
    factory = FakeDshHarnessFactory(_blocking_behavior)
    client = FluxDshClient(_settings(tmp_path), bus=bus, harness_factory=factory)
    run = await client.start_run("长任务", session_id="s-i")

    # P2-15：cancel 会等进程登记后补发 session/cancel，进程树确认清理干净才算 CANCELLED
    cancelled = await client.cancel(run.run_id)
    assert cancelled.status is DshRunStatus.CANCELLED
    assert cancelled.cancel_requested is True
    assert factory.created[0].notifications == [("session/cancel", {"sessionId": "s-i"})]

    finished = await _wait_terminal(client, run.run_id)
    assert finished.status is DshRunStatus.CANCELLED
    assert finished.finished_at is not None
    names = [name for name, _ in bus.history]
    assert Events.DSH_RUN_CANCELLED in names
    client.close()


async def test_cancel_unknown_run_raises_not_found(tmp_path: Path) -> None:
    client = FluxDshClient(_settings(tmp_path), harness_factory=FakeDshHarnessFactory())
    with pytest.raises(NotFoundError):
        await client.cancel("从来没有过的-run")
    client.close()


async def test_refresh_from_row_tolerates_naive_finished_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归：SQLite 读回的 finished_at 是 naive，不能直接与 aware 的 started_at 相减。

    线上实测：取消失败返回 500 —— `_refresh_from_row` 里 naive − aware 抛 TypeError。
    用例按 SQLite 读回的形态构造库行（时间为 naive），验证刷新不再抛错且时长被算出。
    """
    client = FluxDshClient(_settings(tmp_path), harness_factory=FakeDshHarnessFactory())
    run = await client.start_run("hi")
    finished = await _wait_terminal(client, run.run_id)
    naive_finished = (finished.finished_at or datetime.now(timezone.utc)).replace(tzinfo=None)
    row = SimpleNamespace(
        id=run.run_id,
        status=DshRunStatus.COMPLETED.value,
        error=None,
        finish_reason="stop",
        timeout_kind=None,
        cancel_requested=False,
        pid=None,
        pgid=None,
        agent_id=None,
        task_id=run.task_id,
        last_state_change_at=naive_finished,
        finished_at=naive_finished,
    )

    async def fake_get(_run_id: str) -> object:
        return row

    monkeypatch.setattr(client.supervisor.repository, "get", fake_get)
    refreshed = await client.get_run_async(run.run_id)
    assert refreshed.finished_at is not None
    assert refreshed.finished_at.tzinfo is not None
    assert refreshed.duration_seconds is not None
    client.close()


async def test_start_run_disabled_raises_configuration_error(tmp_path: Path) -> None:
    client = FluxDshClient(_settings(tmp_path, dsh_enabled=False))
    with pytest.raises(ConfigurationError):
        await client.start_run("x")


def test_ensure_ready_creates_directories(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    FluxDshClient(settings).ensure_ready()
    assert Path(settings.dsh_home).is_dir()
    assert Path(settings.dsh_workspace).is_dir()


def test_get_run_missing_raises_not_found(tmp_path: Path) -> None:
    client = FluxDshClient(_settings(tmp_path))
    with pytest.raises(NotFoundError):
        client.get_run("nope")


def test_to_flux_event_keeps_unknown_notification() -> None:
    event, body = to_flux_event(
        Notification(method="session/status", payload={"sessionId": "s", "status": "idle"})
    )
    assert event == Events.DSH_EVENT
    assert body["method"] == "session/status"
    assert body["session_id"] == "s"
    assert body["event_type"] is None


def test_to_flux_event_reads_data_content_without_message_wrapper() -> None:
    notification = Notification(
        method="session.event",
        payload={
            "sessionId": "s",
            "event": {
                "type": "assistant/message",
                "data": {"content": [{"type": "text", "text": "hi"}, {"type": "tool_use"}]},
            },
        },
    )
    _, body = to_flux_event(notification)
    assert body["text"] == "hi"


def test_default_behavior_is_a_valid_dsh_behavior() -> None:
    """确认默认替身行为满足 DshBehavior 契约（类型层面外的运行时自检）。"""
    behavior: DshBehavior = default_dsh_behavior
    assert callable(behavior)


# --- MCP 注入（P0-01）---
#
# 这些用例走真实令牌签发（真实 SQLite），断言生成的 patch 能被 DSH 读取、
# 令牌能真的通过 /mcp 鉴权——不是"生成了个文件"就算数。


async def _token_service(settings: Settings) -> tuple[AgentTokenService, AsyncEngine]:
    """真实令牌服务 + 真实 Agent 注册表（P3-16：身份只能来自 Registry 的 canonical UUID）。"""
    engine = create_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    agents = AgentManager(repository=AgentRepository(session_factory))
    return AgentTokenService(session_factory, agents), engine


async def _builtin_agent_id(service: AgentTokenService) -> str:
    """内置 Agent 的 canonical UUID（按名字解析，不存在则补建，与生产 ensure_agent 同路径）。"""
    handle = await service.ensure_agent(name="flux-builtin", permissions=MCP_TOKEN_SCOPES)
    return handle.id_str  # type: ignore[attr-defined]


def _mcp_settings(tmp_path: Path, **overrides: object) -> Settings:
    merged: dict[str, object] = {
        "dsh_mcp_enabled": True,
        "dsh_mcp_url": "http://127.0.0.1:8012/mcp",
    }
    merged.update(overrides)
    return _settings(tmp_path, **merged)


def _patch_payload(path: Path) -> dict:
    """读回生成的 patch：取 insert 列表里的那条 MCP 插件条目。

    顶层必须是 `insert` 条目——写成 `{id: ...}` 会被 DSH 当成"覆盖已有条目"，
    找不到同名条目就静默跳过，MCP 插件根本不会被加载（P0-01 曾因此整条链路失效）。
    """
    entries = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(entries, list) and len(entries) == 1
    patch_entry = entries[0]
    assert list(patch_entry.keys()) == ["insert"], "顶层必须是 insert 条目，不能是 id 覆盖"
    inserted = patch_entry["insert"]
    assert isinstance(inserted, list) and len(inserted) == 1
    entry = inserted[0]
    assert entry["name"] == MCP_PLUGIN_NAME
    return entry


async def test_prepare_mcp_patch_writes_dsh_patch_with_a_working_token(tmp_path: Path) -> None:
    settings = _mcp_settings(tmp_path)
    service, engine = await _token_service(settings)
    client = FluxDshClient(settings, token_service=service)

    path = await client.prepare_mcp_patch()

    assert path == Path(settings.dsh_home) / "patches" / "flux-mcp.patch.yml"
    assert path is not None and path.is_file()
    # 内含令牌明文 → 权限必须收紧到仅属主可访问。
    # POSIX 直接用 st_mode 校验 0600；Windows 的 stat 不反映 chmod（实测 0666），
    # 改为校验平台原语 secure_file（封装 icacls）确实收权成功，而不是删掉这条断言。
    if platforms.IS_WINDOWS:
        assert platforms.secure_file(path) is True
    else:
        assert (path.stat().st_mode & 0o777) == 0o600

    entry = _patch_payload(path)
    assert entry["id"] == "flux-mcp-flux"
    config = entry["config"]
    assert config["transport"] == "streamable-http"
    assert config["serverName"] == "flux"
    assert config["url"] == "http://127.0.0.1:8012/mcp"
    assert config["toolCallTimeoutMs"] == 60000
    assert config["failOnStartupError"] is True

    raw = config["headers"]["Authorization"].removeprefix("Bearer ")
    assert raw.startswith("fxt_")
    builtin_id = await _builtin_agent_id(service)
    identity = await service.authenticate(raw)
    assert identity.agent_id == builtin_id
    assert identity.name == "flux-builtin"
    # 内置 Agent 的能力：读项目 + 提提案 + 经 Flux 终端执行命令（Agent Terminal Console §4）；
    # apply / git / secret 一律没有
    assert identity.scopes == {
        Capability.FILE_READ,
        Capability.FILE_WRITE,
        Capability.TERMINAL_EXECUTE,
    }
    await engine.dispose()


async def test_prepare_mcp_patch_reuses_the_same_token_within_a_process(tmp_path: Path) -> None:
    settings = _mcp_settings(tmp_path)
    service, engine = await _token_service(settings)
    client = FluxDshClient(settings, token_service=service)

    first = await client.prepare_mcp_patch()
    token_first = _patch_payload(first)["config"]["headers"]["Authorization"]
    second = await client.prepare_mcp_patch()
    token_second = _patch_payload(second)["config"]["headers"]["Authorization"]

    assert token_first == token_second
    assert len(await service.list(agent_id=await _builtin_agent_id(service))) == 1
    await engine.dispose()


async def test_prepare_mcp_patch_reissues_after_revocation(tmp_path: Path) -> None:
    """令牌被撤销后（进程重启拿不回明文）→ 旧的一律撤销、重签一枚并覆盖 patch。"""
    settings = _mcp_settings(tmp_path)
    service, engine = await _token_service(settings)
    client = FluxDshClient(settings, token_service=service)
    builtin_id = await _builtin_agent_id(service)
    path = await client.prepare_mcp_patch()
    old_raw = _patch_payload(path)["config"]["headers"]["Authorization"].removeprefix("Bearer ")
    await service.revoke((await service.list(agent_id=builtin_id))[0].id)

    # 模拟进程重启：新客户端没有明文缓存，只能重签
    restarted = FluxDshClient(settings, token_service=service)
    await restarted.prepare_mcp_patch()
    new_raw = _patch_payload(path)["config"]["headers"]["Authorization"].removeprefix("Bearer ")

    assert new_raw != old_raw
    assert (await service.authenticate(new_raw)).agent_id == builtin_id
    with pytest.raises(AuthenticationError):
        await service.authenticate(old_raw)
    recorded = await service.list(agent_id=builtin_id)
    assert len(recorded) == 2 and sum(1 for t in recorded if t.revoked_at is None) == 1
    await engine.dispose()


async def test_start_run_injects_the_patch_into_the_harness(tmp_path: Path) -> None:
    settings = _mcp_settings(tmp_path)
    service, engine = await _token_service(settings)
    factory = FakeDshHarnessFactory()
    client = FluxDshClient(settings, harness_factory=factory, token_service=service)

    run = await client.start_run("看下项目", session_id="mcp-1")
    await _wait_terminal(client, run.run_id)  # harness 在 Run 执行协程里创建，等它跑完再断言

    # patch 文件名带 run_id：同一 Agent 的并发 Run 各用各的 patch，不互相覆盖归属
    patch = Path(settings.dsh_home) / "patches" / f"flux-mcp-{run.run_id}.patch.yml"
    assert factory.created[0].kwargs["patches"] == (str(patch),)
    # 且该 patch 的 MCP header 里带上 X-Flux-Run-Id，供端点把终端会话绑到本次 Run
    assert _patch_payload(patch)["config"]["headers"]["X-Flux-Run-Id"] == run.run_id
    client.close()
    await engine.dispose()


async def test_start_run_fails_closed_without_token_service(tmp_path: Path) -> None:
    """启用了 MCP 注入却没装配令牌服务：必须明确报错，而不是发一个"瞎眼"的 Run。"""
    client = FluxDshClient(_mcp_settings(tmp_path), harness_factory=FakeDshHarnessFactory())
    with pytest.raises(ConfigurationError) as excinfo:
        await client.start_run("hi")
    assert "AgentTokenService" in excinfo.value.message
    assert client.list_runs() == []


async def test_mcp_disabled_skips_patch_and_token(tmp_path: Path) -> None:
    settings = _settings(tmp_path)  # dsh_mcp_enabled=False
    service, engine = await _token_service(settings)
    factory = FakeDshHarnessFactory()
    client = FluxDshClient(settings, harness_factory=factory, token_service=service)

    run = await client.start_run("hi")
    await _wait_terminal(client, run.run_id)

    assert factory.created[0].kwargs["patches"] == ()
    # 未启用注入就不该为一个"用不到的 Agent"建档案、签令牌
    assert await service.list() == []
    assert not (Path(settings.dsh_home) / "patches" / "flux-mcp.patch.yml").exists()
    client.close()
    await engine.dispose()


async def test_prepare_mcp_patch_rejects_bad_server_name_and_url(tmp_path: Path) -> None:
    settings = _mcp_settings(tmp_path, dsh_mcp_server_name="有 空格")
    service, engine = await _token_service(settings)
    client = FluxDshClient(settings, token_service=service)
    with pytest.raises(ConfigurationError) as excinfo:
        await client.prepare_mcp_patch()
    assert "serverName" in excinfo.value.message

    bad_url = FluxDshClient(
        _mcp_settings(tmp_path, dsh_mcp_url="localhost:8012/mcp"), token_service=service
    )
    with pytest.raises(ConfigurationError) as excinfo:
        await bad_url.prepare_mcp_patch()
    assert "http(s)" in excinfo.value.message
    await engine.dispose()


def test_generated_patch_token_is_accepted_by_the_real_mcp_endpoint(tmp_path: Path) -> None:
    """P0-01 的硬判据：patch 里的令牌拿到真实 /mcp 面能用——不是占位符。"""
    project = tmp_path / "project"
    project.mkdir()
    (project / "todo.py").write_text("def done():\n    return False\n", encoding="utf-8")
    settings = _mcp_settings(tmp_path, workspace_root=str(project))
    engine = create_engine(settings.database_url)

    async def _schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_schema())
    asyncio.run(engine.dispose())

    async def _prepare() -> tuple[Path, str]:
        own_engine = create_engine(settings.database_url)
        session_factory = create_session_factory(own_engine)
        agents = AgentManager(repository=AgentRepository(session_factory))
        await agents.load_from_db()
        service = AgentTokenService(session_factory, agents)
        dsh = FluxDshClient(settings, token_service=service)
        path = await dsh.prepare_mcp_patch()
        handle = agents.find_by_name("flux-builtin")
        assert handle is not None
        builtin_id = handle.id_str
        await own_engine.dispose()
        return path, builtin_id

    # 先建档案 + 签令牌（落库），再起 app：启动时 load_from_db 才能把身份装进注册表，
    # 否则鉴权回查注册表会 fail-closed 拒绝——这正是 P3-16 要的行为。
    patch, builtin_id = asyncio.run(_prepare())

    app = create_app(settings)
    with TestClient(app) as client:
        raw = _patch_payload(patch)["config"]["headers"]["Authorization"].removeprefix("Bearer ")
        headers = {"Authorization": f"Bearer {raw}"}

        listed = client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers
        )
        assert listed.status_code == 200, listed.text
        # 核心工具必须都在（required ⊆ advertised），但不要求精确相等：
        # 新增 MCP 工具不该把这条授权链路测试判死（§5）。
        names = {tool["name"] for tool in listed.json()["result"]["tools"]}
        assert names >= CORE_TOOL_NAMES
        assert names & FORBIDDEN_TOOL_NAMES == set()

        created = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "proposal.create",
                    "arguments": {
                        "payload": {
                            "summary": "让 done() 说真话",
                            "changes": [
                                {"path": "todo.py", "content": "def done():\n    return True\n"}
                            ],
                        }
                    },
                },
            },
            headers=headers,
        )
        assert created.status_code == 200, created.text
        result = created.json()["result"]
        assert result["isError"] is False, result["content"][0]["text"]
        proposal = json.loads(result["content"][0]["text"])
        assert proposal["agent"] == builtin_id
        assert proposal["changes"][0]["status"] == "pending"
        # 提案只进审核队列，磁盘上必须还是旧内容（Agent 没有直接落盘能力）
        on_disk = (project / "todo.py").read_text(encoding="utf-8")
        assert on_disk == "def done():\n    return False\n"


# --- Runtime Identity 注入（P0-1）---


def test_agent_env_injects_runtime_identity(tmp_path: Path) -> None:
    """FLUX_* 只说明"我在哪 / 属于哪个 Run"，不是安全凭证（最终方案 §5）。"""
    workspace = tmp_path / "ws"
    settings = _settings(tmp_path, workspace_root=str(workspace), dsh_mcp_enabled=True)
    client = FluxDshClient(settings, harness_factory=FakeDshHarnessFactory())
    run = DshRun(
        session_id="s",
        instruction="hi",
        run_id="run-abc",
        agent_id="agent-1",
        task_id="task-1",
    )

    env = client._agent_env(run)

    assert env == {
        "FLUX_RUNTIME": "1",
        "FLUX_VERSION": VERSION,
        "FLUX_RUN_ID": "run-abc",
        "FLUX_WORKSPACE": str(workspace),
        "FLUX_TASK_ID": "task-1",
        "FLUX_AGENT_ID": "agent-1",
        "FLUX_MCP_ENDPOINT": settings.dsh_mcp_url,
        # 三模式：Run 启动快照的权限模式随环境下发给 Agent
        "FLUX_PERMISSION_MODE": "normal",
    }
    # 身份不是凭证：环境里绝不出现令牌 / 密钥
    assert not any("token" in key.lower() or "key" in key.lower() for key in env)
    client.close()


def test_agent_env_omits_optional_fields_when_absent(tmp_path: Path) -> None:
    """独立起 Run（无 task / agent / MCP）时不该注入空值变量。"""
    settings = _settings(tmp_path)
    client = FluxDshClient(settings, harness_factory=FakeDshHarnessFactory())
    env = client._agent_env(DshRun(session_id="s", instruction="hi", run_id="run-x"))

    assert set(env) == {
        "FLUX_RUNTIME",
        "FLUX_VERSION",
        "FLUX_RUN_ID",
        "FLUX_WORKSPACE",
        "FLUX_PERMISSION_MODE",
    }
    assert env["FLUX_WORKSPACE"] == settings.dsh_workspace
    client.close()


async def test_agent_env_reaches_the_harness(tmp_path: Path) -> None:
    """注入不止停在函数里：起 Run 时 env 真的交给了 harness（P0-1 落点）。"""
    workspace = tmp_path / "ws"
    settings = _settings(tmp_path, workspace_root=str(workspace))
    factory = FakeDshHarnessFactory()
    client = FluxDshClient(settings, harness_factory=factory)

    run = await client.start_run("hello", task_id="task-9")
    await _wait_terminal(client, run.run_id)

    env = factory.created[0].kwargs["env"]
    assert env["FLUX_RUN_ID"] == run.run_id
    assert env["FLUX_TASK_ID"] == "task-9"
    assert env["FLUX_WORKSPACE"] == str(workspace)
    client.close()
