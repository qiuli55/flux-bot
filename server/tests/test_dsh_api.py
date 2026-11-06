"""DSH Run API 测试（集成方案 §18 Phase 1）。

用注入 fake harness 的 FluxDshClient 跑通全链路，不启动真实 runtime、不调用真实模型。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from deepseek_harness import Notification, RunResult
from fastapi.testclient import TestClient

from flux.config import Settings
from flux.core.agent_runtime.dsh_client import FluxDshClient
from flux.core.agent_runtime.protocol import RUNTIME_BOOTSTRAP
from flux.main import create_app
from tests.fakes import FakeDshHarness, FakeDshHarnessFactory, default_dsh_behavior

PREFIX = "/api/v1"


def _blocking_behavior(
    harness: FakeDshHarness,
    instruction: str,
    session_id: str | None,
    on_notification: object,
) -> RunResult:
    """阻塞到收到 session/cancel 再返回，用来验证 API 的中断链路。"""
    harness.cancel_event.wait(timeout=5)
    sid = session_id or "session"
    return RunResult(
        session_id=sid,
        final_response="",
        finish_reason="cancelled",
        events=[],
        notifications=[],
    )


def test_post_runs_disabled_returns_503(settings: Settings, db_schema: None) -> None:
    """未启用时（默认）POST /dsh/runs 必须 503 configuration_error。"""
    disabled = settings.model_copy(update={"dsh_enabled": False})
    with TestClient(create_app(disabled)) as client:
        response = client.post(f"{PREFIX}/dsh/runs", json={"instruction": "hi"})

    assert response.status_code == 503
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "configuration_error"


def test_status_reports_configuration(settings: Settings, db_schema: None) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get(f"{PREFIX}/dsh/status")

    assert response.status_code == 200
    data = response.json()["data"]
    assert set(data) == {"enabled", "home", "workspace", "provider", "model", "mcp"}


def test_dsh_run_api_chain(settings: Settings, tmp_path, db_schema: None) -> None:
    """POST → GET → interrupt 全链路：注入 fake harness，Run 最终落到 cancelled。"""
    enabled = settings.model_copy(
        update={
            "dsh_enabled": True,
            "dsh_home": str(tmp_path / "dsh-home"),
            "dsh_workspace": str(tmp_path / "dsh-ws"),
        }
    )
    app = create_app(enabled)
    factory = FakeDshHarnessFactory(_blocking_behavior)

    with TestClient(app) as client:
        container = app.state.container
        # 走真实装配路径：MCP 注入默认开启，起 Run 会签发内置 Agent 令牌（需要库表与令牌服务）
        container.dsh = FluxDshClient(
            enabled,
            bus=container.bus,
            harness_factory=factory,
            token_service=container.agent_tokens,
        )

        created = client.post(
            f"{PREFIX}/dsh/runs", json={"instruction": "写一个测试", "session_id": "api-1"}
        )
        assert created.status_code == 200
        run = created.json()["data"]
        # P2-15：起 Run 只登记，实际执行在后台协程里推进（PENDING → STARTING → RUNNING）
        assert run["status"] in {"pending", "starting", "running"}
        assert run["session_id"] == "api-1"

        listed = client.get(f"{PREFIX}/dsh/runs")
        assert listed.status_code == 200
        assert listed.json()["metadata"]["count"] == 1

        detail = client.get(f"{PREFIX}/dsh/runs/{run['run_id']}")
        assert detail.status_code == 200
        assert detail.json()["data"]["run_id"] == run["run_id"]

        # 轮询触发中断，直到 harness 收到 session/cancel（规避 POST 与 harness 注册的竞态）
        for _ in range(300):
            interrupted = client.post(f"{PREFIX}/dsh/runs/{run['run_id']}/interrupt")
            assert interrupted.status_code == 200
            if factory.created and factory.created[0].notifications:
                break
            time.sleep(0.01)
        assert interrupted.json()["data"]["cancel_requested"] is True
        assert factory.created[0].notifications == [("session/cancel", {"sessionId": "api-1"})]

        final = None
        for _ in range(300):
            final = client.get(f"{PREFIX}/dsh/runs/{run['run_id']}").json()["data"]
            if final["status"] == "cancelled":
                break
            time.sleep(0.01)

    assert final is not None
    assert final["status"] == "cancelled"
    assert final["finished_at"] is not None
    assert final["duration_seconds"] is not None


def test_dsh_api_run_prompt_includes_bootstrap(
    settings: Settings, tmp_path, db_schema: None
) -> None:
    """批次①验收（DSH API 路径）：dsh.py → start_run 交给 harness 的 prompt 以 Bootstrap 起始。"""
    enabled = settings.model_copy(
        update={
            "dsh_enabled": True,
            "dsh_home": str(tmp_path / "dsh-home"),
            "dsh_workspace": str(tmp_path / "dsh-ws"),
        }
    )
    app = create_app(enabled)
    captured: list[str] = []

    def behavior(
        harness: FakeDshHarness,
        instruction: str,
        session_id: str | None,
        on_notification: Callable[[Notification], None] | None,
    ) -> RunResult:
        captured.append(instruction)
        return default_dsh_behavior(harness, instruction, session_id, on_notification)

    factory = FakeDshHarnessFactory(behavior)
    with TestClient(app) as client:
        container = app.state.container
        container.dsh = FluxDshClient(
            enabled,
            bus=container.bus,
            harness_factory=factory,
            token_service=container.agent_tokens,
        )
        created = client.post(
            f"{PREFIX}/dsh/runs", json={"instruction": "帮我修登录接口", "session_id": "api-b"}
        )
        assert created.status_code == 200
        run = created.json()["data"]
        # API 快照保持用户原文：Bootstrap 不落进接口响应
        assert run["instruction"] == "帮我修登录接口"

        final = None
        for _ in range(300):
            final = client.get(f"{PREFIX}/dsh/runs/{run['run_id']}").json()["data"]
            if final["status"] == "completed":
                break
            time.sleep(0.01)

    assert final is not None
    assert final["status"] == "completed"
    assert final["instruction"] == "帮我修登录接口"
    assert captured == [f"{RUNTIME_BOOTSTRAP}\n\n帮我修登录接口"]


def test_get_missing_run_returns_404(settings: Settings, tmp_path, db_schema: None) -> None:
    enabled = settings.model_copy(
        update={
            "dsh_enabled": True,
            "dsh_home": str(tmp_path / "dsh-home"),
            "dsh_workspace": str(tmp_path / "dsh-ws"),
        }
    )
    with TestClient(create_app(enabled)) as client:
        response = client.get(f"{PREFIX}/dsh/runs/does-not-exist")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
