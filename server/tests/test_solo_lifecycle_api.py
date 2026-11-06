"""Solo 任务生命周期 API 测试：P0-06 需求确认、P0-05 开始执行、§5 决策模式。

三条链路各自对应的"必须测试"项：

- P0-06：AI 先追问不动代码；六维度确认卡用户可改；改完的版本就是执行依据（TC-103）；
- P0-05：确认后「开始执行」真正起一次 DSH Run，任务状态随 Run 终态收敛；
- §5：任务级策略 auto / manual 分流，manual 挂 `waiting_for_user_decision` 等人选，
  选完续跑；Run 结束时不得把"等人决策"改写成完成（TC-203）。

DSH 一律注入 fake harness：不启动真实 runtime、不调用真实模型（`default_provider="local"`）。
决策类用例需要"Agent 还在执行中"这个前提，因此默认替身行为是**一直不结束**，
由夹具在用例收尾时统一放行——否则 Run 会在毫秒级跑完，任务已终态，决策点无从谈起。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from deepseek_harness import Notification, RunResult
from fastapi.testclient import TestClient

from flux.config import Settings
from flux.container import Container
from flux.core.agent_runtime.dsh_client import FluxDshClient
from flux.core.agent_runtime.protocol import RUNTIME_BOOTSTRAP
from flux.core.event.bus import Events
from flux.core.task_engine.assistant import CONFIRMATION_DIMENSIONS
from flux.enums import DecisionMode, DshRunStatus, TaskStatus
from flux.main import create_app
from tests.fakes import (
    DshBehavior,
    FakeDshHarness,
    FakeDshHarnessFactory,
    default_dsh_behavior,
)

PREFIX = "/api/v1"


@pytest.fixture(autouse=True)
def _schema(db_schema: None) -> None:
    """本模块自建 app 实例（不走 conftest 的 client 夹具），因此显式声明建表依赖。"""


#: 六个维度的完整确认卡（label 必须正好是这六个，顺序即声明顺序）
SIX_DIMENSIONS = [
    {"label": dimension, "value": f"{dimension}：测试用例给出的结论"}
    for dimension in CONFIRMATION_DIMENSIONS
]

_OPTIONS: list[dict[str, Any]] = [
    {
        "label": "方案 A：加索引",
        "description": "改动最小",
        "impact": "查询变快",
        "recommended": True,
    },
    {
        "label": "方案 B：改表结构",
        "description": "彻底但要迁移",
        "impact": "需停机",
        "recommended": False,
    },
]


def _hold_behavior(
    harness: FakeDshHarness,
    instruction: str,
    session_id: str | None,
    on_notification: Callable[[Notification], None] | None,
) -> RunResult:
    """替身行为：Run 一直不结束，模拟"执行中的 Agent"（夹具收尾时放行）。"""
    harness.cancel_event.wait(timeout=10)
    return RunResult(
        session_id=session_id or "session",
        final_response="",
        finish_reason="cancelled",
        events=[],
        notifications=[],
    )


@contextmanager
def _solo(
    settings: Settings, tmp_path: Path, *, behavior: DshBehavior = _hold_behavior
) -> Iterator[tuple[TestClient, FakeDshHarnessFactory]]:
    """带 fake DSH 的 app：任务「开始执行」时起的是替身 Run，不碰真实 runtime。"""
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    enabled = settings.model_copy(
        update={
            "dsh_enabled": True,
            "dsh_home": str(tmp_path / "dsh-home"),
            "dsh_workspace": str(tmp_path / "dsh-ws"),
            "workspace_root": str(root),
        }
    )
    app = create_app(enabled)
    factory = FakeDshHarnessFactory(behavior)
    with TestClient(app) as client:
        container = app.state.container
        container.dsh = FluxDshClient(
            enabled,
            bus=container.bus,
            harness_factory=factory,
            token_service=container.agent_tokens,
        )
        try:
            yield client, factory
        finally:
            # 放行所有还在"执行中"的替身 Run，避免退出时留下阻塞线程
            for harness in factory.created:
                harness.cancel_event.set()


def _create(client: TestClient, *, mode: str = "auto", description: str = "改造待办服务") -> dict:
    response = client.post(
        f"{PREFIX}/tasks", json={"description": description, "decision_mode": mode}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _start(client: TestClient, task_id: str, **extra: Any) -> dict:
    response = client.post(f"{PREFIX}/tasks/{task_id}/start", json=extra)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _task(client: TestClient, task_id: str) -> dict:
    return client.get(f"{PREFIX}/tasks/{task_id}").json()["data"]


def _wait_status(client: TestClient, task_id: str, wanted: str, *, timeout: float = 5.0) -> dict:
    """轮询任务状态直到收敛（Run 在后台线程里跑，HTTP 返回时它还没结束）。"""
    deadline = time.monotonic() + timeout
    task: dict = {}
    while time.monotonic() < deadline:
        task = _task(client, task_id)
        if task["status"] == wanted:
            return task
        time.sleep(0.01)
    raise AssertionError(f"任务未在 {timeout}s 内进入 {wanted}，当前 {task.get('status')}")


def _wait_harnesses(factory: Any, count: int, *, timeout: float = 5.0) -> None:
    """Run 的 harness 在后台任务里创建：HTTP 返回时可能还没到位，等它出现再断言。"""
    deadline = time.monotonic() + timeout
    while len(factory.created) < count and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(factory.created) == count


def _messages(client: TestClient, task_id: str) -> list[dict]:
    return client.get(f"{PREFIX}/tasks/{task_id}/messages").json()["data"]


def _wait_last_message(
    client: TestClient, task_id: str, kind: str, *, timeout: float = 5.0
) -> dict:
    """等最后一条消息落库。

    状态与结果消息是两次写入（桥接器先改状态、再追加消息），因此"状态已终态"不代表
    "结果消息已可见"——这里显式等消息，避免断言踩进那个毫秒级窗口。
    """
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        messages = _messages(client, task_id)
        last = messages[-1] if messages else {}
        if last.get("kind") == kind:
            return last
        time.sleep(0.01)
    raise AssertionError(f"最后一条消息未在 {timeout}s 内变成 {kind}，当前 {last.get('kind')}")


def _raise_decision(client: TestClient, task_id: str, **overrides: Any) -> dict:
    body = {"question": "索引怎么加？", "options": _OPTIONS}
    body.update(overrides)
    response = client.post(f"{PREFIX}/tasks/{task_id}/decisions", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


# --- P0-06：需求确认六维度 ---


def test_task_defaults_to_auto_with_no_confirmation(settings: Settings, tmp_path: Path) -> None:
    """新建任务：默认 auto 策略，确认卡与决策点都是空的（还没谈、也没开始执行）。"""
    with _solo(settings, tmp_path) as (client, _):
        task = _create(client)
        assert task["decision_mode"] == "auto"
        assert task["confirmation"] is None
        assert task["decisions"] == [] and task["pending_decision"] is None
        assert task["run_id"] is None


def test_assistant_replies_without_touching_the_code(settings: Settings, tmp_path: Path) -> None:
    """AI 先对话不动代码：消息落库、任务进入 running，但不会自己起 Run。"""
    with _solo(settings, tmp_path) as (client, factory):
        task = _create(client, description="给待办服务加一个按完成状态过滤的接口")
        replied = client.post(
            f"{PREFIX}/tasks/{task['id']}/messages", json={"content": "只做后端，别动前端"}
        )
        assert replied.status_code == 200, replied.text
        body = replied.json()["data"]
        assert body["task"]["status"] == "running"
        assert body["messages"][0]["role"] == "user"
        assert body["messages"][1]["role"] == "assistant"
        assert body["messages"][1]["content"]

        # 追问阶段绝不执行：没有任何 Run 被起起来
        assert factory.created == []
        assert _task(client, task["id"])["run_id"] is None


def test_user_can_edit_confirmation_and_it_becomes_the_basis(
    settings: Settings, tmp_path: Path
) -> None:
    """用户改过的确认卡就是执行依据，且改动留在对话轨迹里（TC-103）。"""
    with _solo(settings, tmp_path) as (client, _):
        task = _create(client)
        edited = [dict(item) for item in SIX_DIMENSIONS]
        edited[0] = {"label": "目标", "value": "只支持按完成状态过滤，不做排序"}

        updated = client.put(f"{PREFIX}/tasks/{task['id']}/confirmation", json={"items": edited})
        assert updated.status_code == 200, updated.text
        data = updated.json()["data"]
        assert data["task"]["confirmation"]["actor"] == "user"
        assert [item["label"] for item in data["task"]["confirmation"]["items"]] == list(
            CONFIRMATION_DIMENSIONS
        )
        assert data["task"]["confirmation"]["items"][0]["value"].startswith("只支持按完成状态过滤")
        assert data["message"]["kind"] == "confirmation"
        assert "只支持按完成状态过滤" in data["message"]["content"]

        # 刷新后仍在（确认卡落库，不是内存里的一时状态）
        fetched = _task(client, task["id"])
        assert fetched["confirmation"]["items"][0]["value"].startswith("只支持按完成状态过滤")


def test_confirmation_requires_exactly_the_six_dimensions(
    settings: Settings, tmp_path: Path
) -> None:
    """确认卡不是自由文本：缺维度 / 多维度 / 重复 / 空值一律 422，不静默补齐。"""
    with _solo(settings, tmp_path) as (client, _):
        task = _create(client)
        url = f"{PREFIX}/tasks/{task['id']}/confirmation"

        missing = client.put(url, json={"items": SIX_DIMENSIONS[:5]})
        assert missing.status_code == 422
        assert "缺少维度" in missing.json()["message"]

        unknown = client.put(
            url, json={"items": [*SIX_DIMENSIONS[:5], {"label": "技术栈", "value": "React"}]}
        )
        assert unknown.status_code == 422
        assert "确认维度非法" in unknown.json()["message"]

        duplicated = client.put(url, json={"items": [*SIX_DIMENSIONS[:5], SIX_DIMENSIONS[0]]})
        assert duplicated.status_code == 422
        assert "重复" in duplicated.json()["message"]

        empty = client.put(
            url,
            json={
                "items": [
                    *SIX_DIMENSIONS[:5],
                    {"label": CONFIRMATION_DIMENSIONS[5], "value": "  "},
                ]
            },
        )
        assert empty.status_code == 422

        # 被拒之后任务上不留半张确认卡
        assert _task(client, task["id"])["confirmation"] is None


def test_confirmation_is_rejected_on_a_finished_task(settings: Settings, tmp_path: Path) -> None:
    with _solo(settings, tmp_path) as (client, _):
        task = _create(client)
        assert client.post(f"{PREFIX}/tasks/{task['id']}/cancel").status_code == 200
        response = client.put(
            f"{PREFIX}/tasks/{task['id']}/confirmation", json={"items": SIX_DIMENSIONS}
        )
        assert response.status_code == 409


# --- P0-05：开始执行 ---


def test_start_runs_the_agent_and_settles_the_task(settings: Settings, tmp_path: Path) -> None:
    """确认后「开始执行」：起一次 DSH Run，Run 结束把任务收敛到 completed。"""
    with _solo(settings, tmp_path, behavior=default_dsh_behavior) as (client, factory):
        task = _create(client, description="给待办服务加过滤接口")
        client.put(f"{PREFIX}/tasks/{task['id']}/confirmation", json={"items": SIX_DIMENSIONS})

        started = _start(client, task["id"])
        assert started["task"]["status"] == "running"
        run_id = started["task"]["run_id"]
        assert run_id and started["run"]["run_id"] == run_id
        # 开始执行不再落「已开始执行」气泡：执行过程只走流事件
        assert started["message"] is None

        # 指令里带着用户确认过的需求与能力边界（Agent 拿到的就是这份）
        instruction = started["run"]["instruction"]
        for dimension in CONFIRMATION_DIMENSIONS:
            assert dimension in instruction
        assert "不要尝试直接写文件" in instruction

        settled = _wait_status(client, task["id"], "completed")
        assert settled["run_id"] == run_id
        assert len(factory.created) == 1
        # 批次①：任务路径（tasks.py → start_run）交给 harness 的 prompt 以 Bootstrap 起始，
        # 而 run / 消息里存的仍是用户原文（上面的 instruction 取样即存库原文）
        assert factory.created[0].prompts == [f"{RUNTIME_BOOTSTRAP}\n\n{instruction}"]
        assert _wait_last_message(client, task["id"], "run_finished")["content"].startswith("echo:")


def test_start_can_take_the_confirmation_with_the_request(
    settings: Settings, tmp_path: Path
) -> None:
    """前端"改完直接执行"：确认卡随 start 一起提交，落库的就是这份。"""
    with _solo(settings, tmp_path, behavior=default_dsh_behavior) as (client, _):
        task = _create(client)
        tuned = [dict(item) for item in SIX_DIMENSIONS]
        tuned[3] = {"label": "修改范围", "value": "只改 todo_service/store.py"}

        data = _start(client, task["id"], confirmation=tuned)
        assert data["task"]["confirmation"]["actor"] == "user"
        assert "只改 todo_service/store.py" in data["run"]["instruction"]
        _wait_status(client, task["id"], "completed")


def test_start_twice_is_rejected(settings: Settings, tmp_path: Path) -> None:
    """重复开始执行是明确冲突，不会起第二个 Run。"""
    with _solo(settings, tmp_path) as (client, factory):
        task = _create(client)
        _start(client, task["id"])
        again = client.post(f"{PREFIX}/tasks/{task['id']}/start", json={})
        assert again.status_code == 409
        assert "已开始执行" in again.json()["message"]
        _wait_harnesses(factory, 1)


def test_start_fails_loudly_when_dsh_is_disabled(settings: Settings, tmp_path: Path) -> None:
    """未启用 DSH 时是 503，不是"假装开始执行"的 200。"""
    disabled = settings.model_copy(update={"dsh_enabled": False, "workspace_root": str(tmp_path)})
    with TestClient(create_app(disabled)) as client:
        task = _create(client)
        response = client.post(f"{PREFIX}/tasks/{task['id']}/start", json={})
        assert response.status_code == 503
        assert response.json()["code"] == "configuration_error"
        # 失败之后任务上不留 run_id，可以修好配置再点一次
        assert _task(client, task["id"])["run_id"] is None


def test_cancel_stops_the_task_and_its_run(settings: Settings, tmp_path: Path) -> None:
    """取消任务要连它正在跑的 Run 一起停（留个还在干活的 Agent 与"已取消"是矛盾的）。

    替身 harness 收到 session/cancel 的确定性验证在 test_dsh_api 里；这里验证任务链路：
    取消后任务与它的 Run 都进入 cancelled，且中断请求已下发。
    """
    with _solo(settings, tmp_path) as (client, factory):
        task = _create(client)
        run_id = _start(client, task["id"])["task"]["run_id"]

        cancelled = client.post(f"{PREFIX}/tasks/{task['id']}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["data"]["status"] == "cancelled"

        run = client.get(f"{PREFIX}/dsh/runs/{run_id}").json()["data"]
        assert run["cancel_requested"] is True
        for _ in range(500):
            run = client.get(f"{PREFIX}/dsh/runs/{run_id}").json()["data"]
            if run["status"] == "cancelled":
                break
            # 替身 Run 卡在"执行中"，需要真收到 cancel 才会返回
            factory.created[0].cancel_event.set()
            time.sleep(0.01)
        assert run["status"] == "cancelled"
        assert _task(client, task["id"])["status"] == "cancelled"


def test_cancel_after_run_completed_keeps_task_completed(
    settings: Settings, tmp_path: Path
) -> None:
    """竞态窗口：Run 已成功完成、任务尚未被桥接收尾时收到取消。

    真实现场（2026-10-09 桩链路实测，run 22bbe5e6）：取消读任务时它还是 running，
    而 Run 的终态已是 completed；旧实现把"非 cancelled"一律记 failed，把一次成功的
    执行改判成失败。修复后按 Run 的真实结局收尾：任务与响应都停留在 completed。
    """
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    enabled = settings.model_copy(
        update={
            "dsh_enabled": True,
            "dsh_home": str(tmp_path / "dsh-home"),
            "dsh_workspace": str(tmp_path / "dsh-ws"),
            "workspace_root": str(root),
        }
    )
    app = create_app(enabled)
    factory = FakeDshHarnessFactory(_hold_behavior)
    with TestClient(app) as client:
        container = app.state.container
        container.dsh = FluxDshClient(
            enabled,
            bus=container.bus,
            harness_factory=factory,
            token_service=container.agent_tokens,
        )
        try:
            task = _create(client)
            _start(client, task["id"])

            async def cancel_reports_completed(run_id: str) -> Any:
                """替身：取消发出的瞬间 Run 已经成功结束（终态先行、桥接收尾在后）。"""
                return SimpleNamespace(status=DshRunStatus.COMPLETED)

            container.dsh.cancel = cancel_reports_completed  # type: ignore[method-assign]
            response = client.post(f"{PREFIX}/tasks/{task['id']}/cancel")
            assert response.status_code == 200
            assert response.json()["data"]["status"] == "completed"
            assert _task(client, task["id"])["status"] == "completed"
        finally:
            for harness in factory.created:
                harness.cancel_event.set()


def test_continue_reopens_a_finished_conversation(settings: Settings, tmp_path: Path) -> None:
    """终态对话收到新消息：重开同一任务并起第二轮 Run，历史与用户消息都保留。"""
    with _solo(settings, tmp_path, behavior=default_dsh_behavior) as (client, factory):
        task = _create(client, description="给待办服务加过滤接口")
        started = _start(client, task["id"])
        first_run_id = started["task"]["run_id"]
        _wait_status(client, task["id"], "completed")
        _wait_last_message(client, task["id"], "run_finished")
        history_before = len(_messages(client, task["id"]))

        continued = client.post(
            f"{PREFIX}/tasks/{task['id']}/continue", json={"content": "再补一个按关键字搜索的参数"}
        )
        assert continued.status_code == 200, continued.text
        data = continued.json()["data"]
        assert data["task"]["status"] == "running"
        assert data["task"]["run_id"] and data["task"]["run_id"] != first_run_id
        assert data["messages"][0]["role"] == "user"
        assert "再补一个按关键字搜索的参数" in data["messages"][0]["content"]
        # 续跑只回用户消息：执行过程由流事件呈现，不再落 run_started 气泡
        assert len(data["messages"]) == 1

        # 起了第二轮 Run，且这一轮的指令就是新消息（不再叠加上一轮的确认卡）
        _wait_harnesses(factory, 2)
        # Run 在后台线程里执行，prompt 可能在 HTTP 返回后才记下来，这里等一小会儿
        deadline = time.monotonic() + 5
        while not factory.created[1].prompts and time.monotonic() < deadline:
            time.sleep(0.01)
        followup_instruction = factory.created[1].prompts[0]
        assert "再补一个按关键字搜索的参数" in followup_instruction
        assert "## 本轮任务" in followup_instruction
        assert "需求确认" not in followup_instruction
        # 两轮必须用不同 session：DSH 复用同一个 id 起第二轮会报 already exists
        assert factory.created[0].session_ids and factory.created[1].session_ids
        assert factory.created[0].session_ids[0] != factory.created[1].session_ids[0]

        # 历史没被清掉：旧消息还在，新消息追加在后面
        messages = _messages(client, task["id"])
        assert len(messages) > history_before
        assert messages[history_before]["role"] == "user"

        _wait_status(client, task["id"], "completed")
        assert _task(client, task["id"])["run_id"] == data["task"]["run_id"]


def test_continue_rejects_a_conversation_still_running(settings: Settings, tmp_path: Path) -> None:
    """还在执行的对话不需要续跑：直接追加消息即可，续跑接口明确 409。"""
    with _solo(settings, tmp_path) as (client, factory):
        task = _create(client)
        _start(client, task["id"])
        response = client.post(
            f"{PREFIX}/tasks/{task['id']}/continue", json={"content": "再改一处"}
        )
        assert response.status_code == 409
        assert "仍在进行中" in response.json()["message"]
        _wait_harnesses(factory, 1)


def test_continue_works_after_the_conversation_was_cancelled(
    settings: Settings, tmp_path: Path
) -> None:
    """取消也是终态：用户后悔了可以接着聊，不用重建一个新对话。"""
    with _solo(settings, tmp_path) as (client, factory):
        task = _create(client)
        _start(client, task["id"])
        cancelled = client.post(f"{PREFIX}/tasks/{task['id']}/cancel", json={})
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["data"]["status"] == "cancelled"

        continued = client.post(
            f"{PREFIX}/tasks/{task['id']}/continue", json={"content": "还是继续做吧"}
        )
        assert continued.status_code == 200, continued.text
        assert continued.json()["data"]["task"]["status"] == "running"
        _wait_harnesses(factory, 2)


# --- §5 决策模式 ---


def test_decision_mode_can_be_switched_per_task(settings: Settings, tmp_path: Path) -> None:
    with _solo(settings, tmp_path) as (client, _):
        task = _create(client, mode="manual")
        assert task["decision_mode"] == "manual"

        switched = client.post(f"{PREFIX}/tasks/{task['id']}/decision-mode", json={"mode": "auto"})
        assert switched.status_code == 200
        assert switched.json()["data"]["decision_mode"] == "auto"

        invalid = client.post(f"{PREFIX}/tasks/{task['id']}/decision-mode", json={"mode": "随便"})
        assert invalid.status_code == 422


def test_decision_before_start_is_rejected(settings: Settings, tmp_path: Path) -> None:
    """还没执行就没有"执行中的决策点"——空谈的决策点没有意义。"""
    with _solo(settings, tmp_path) as (client, _):
        task = _create(client, mode="manual")
        response = client.post(
            f"{PREFIX}/tasks/{task['id']}/decisions",
            json={"question": "用哪个方案？", "options": _OPTIONS},
        )
        assert response.status_code == 409
        assert "尚未开始执行" in response.json()["message"]


def test_manual_mode_waits_for_the_user_then_resumes(settings: Settings, tmp_path: Path) -> None:
    """模式 B：任务停在等人决策，用户选完回到 running，决策记录留痕。"""
    with _solo(settings, tmp_path) as (client, _):
        task = _create(client, mode="manual")
        _start(client, task["id"])

        raised = _raise_decision(
            client,
            task["id"],
            context="store.list 全表扫描",
            recommendation="方案 A 改动最小",
        )
        assert raised["task"]["status"] == "waiting_for_user_decision"
        assert raised["decision"]["status"] == "pending" and raised["decision"]["chosen"] is None
        assert raised["message"]["kind"] == "decision"
        assert raised["task"]["pending_decision"]["question"] == "索引怎么加？"
        # 首次决策不设推荐项时，候选方案原样带给用户（含影响与推荐标记）
        assert [option["label"] for option in raised["decision"]["options"]] == [
            "方案 A：加索引",
            "方案 B：改表结构",
        ]

        # 同一时刻只允许一个待决策项，避免"两个问题抢一个答案"
        second = client.post(
            f"{PREFIX}/tasks/{task['id']}/decisions",
            json={"question": "另一个问题？", "options": _OPTIONS},
        )
        assert second.status_code == 409

        chosen = client.post(
            f"{PREFIX}/tasks/{task['id']}/decisions/choose",
            json={
                "decision_id": raised["decision"]["id"],
                "action": "choose",
                "option": "方案 B：改表结构",
            },
        )
        assert chosen.status_code == 200, chosen.text
        data = chosen.json()["data"]
        assert data["task"]["status"] == "running"
        assert data["task"]["pending_decision"] is None
        assert data["decision"]["status"] == "resolved"
        assert data["decision"]["chosen"] == "方案 B：改表结构"
        assert data["message"]["kind"] == "decision_result"
        assert data["message"]["role"] == "user"


def test_manual_mode_rejects_bad_choices(settings: Settings, tmp_path: Path) -> None:
    """选择必须落在候选方案里，否则 422（不能凭空执行一个没人提过的方案）。"""
    with _solo(settings, tmp_path) as (client, _):
        task = _create(client, mode="manual")
        _start(client, task["id"])
        raised = _raise_decision(client, task["id"])
        url = f"{PREFIX}/tasks/{task['id']}/decisions/choose"
        decision_id = raised["decision"]["id"]

        unknown = client.post(
            url, json={"decision_id": decision_id, "action": "choose", "option": "方案 C"}
        )
        assert unknown.status_code == 422

        no_option = client.post(url, json={"decision_id": decision_id, "action": "choose"})
        assert no_option.status_code == 400

        stale = client.post(
            url,
            json={"decision_id": uuid.uuid4().hex, "action": "choose", "option": "方案 A：加索引"},
        )
        assert stale.status_code == 404

        # 三次失败都没把任务从等待态挪走
        assert _task(client, task["id"])["status"] == "waiting_for_user_decision"


def test_user_can_reject_all_options_and_agent_keeps_going(
    settings: Settings, tmp_path: Path
) -> None:
    """候选都不接受：决策点转 rejected，任务回到 running，Agent 可以重新给方案。"""
    with _solo(settings, tmp_path) as (client, _):
        task = _create(client, mode="manual")
        _start(client, task["id"])
        raised = _raise_decision(client, task["id"])

        rejected = client.post(
            f"{PREFIX}/tasks/{task['id']}/decisions/choose",
            json={
                "decision_id": raised["decision"]["id"],
                "action": "reject",
                "note": "两个方案都太重，请给更轻量的",
            },
        )
        assert rejected.status_code == 200, rejected.text
        data = rejected.json()["data"]
        assert data["decision"]["status"] == "rejected" and data["decision"]["chosen"] is None
        assert data["task"]["status"] == "running" and data["task"]["pending_decision"] is None
        assert "请给更轻量的" in data["message"]["content"]

        # 拒绝之后可以再问一次（同一个任务、新的决策点）
        again = _raise_decision(client, task["id"], question="那用缓存层？")
        assert again["task"]["status"] == "waiting_for_user_decision"
        assert len(again["task"]["decisions"]) == 2


def test_auto_mode_decides_by_itself_and_keeps_running(settings: Settings, tmp_path: Path) -> None:
    """模式 A：按推荐项自行拍板，不打断用户，但记录照样留痕。"""
    with _solo(settings, tmp_path) as (client, _):
        task = _create(client, mode="auto")
        _start(client, task["id"])

        raised = _raise_decision(client, task["id"], recommendation="方案 A")
        assert raised["decision"]["status"] == "auto_resolved"
        assert raised["decision"]["chosen"] == "方案 A：加索引"
        assert raised["message"]["kind"] == "decision_auto"

        # 任务不会被挂起等人选
        now = _task(client, task["id"])
        assert now["pending_decision"] is None
        assert now["status"] == TaskStatus.RUNNING.value


def test_auto_mode_falls_back_to_the_first_option(settings: Settings, tmp_path: Path) -> None:
    """没人标推荐时用第一项——总要有个结论，不能因为"没标"就卡住。"""
    with _solo(settings, tmp_path) as (client, _):
        task = _create(client, mode="auto")
        _start(client, task["id"])
        raised = _raise_decision(
            client, task["id"], question="选哪个？", options=[{"label": "甲"}, {"label": "乙"}]
        )
        assert raised["decision"]["chosen"] == "甲"


# --- Run 终态回写（TC-203，直接打事件总线）---


@pytest.fixture()
async def solo_container(settings: Settings, db_schema: None) -> AsyncIterator[Container]:
    """异步用例的容器：用完显式 dispose，避免 aiosqlite 线程活过事件循环。"""
    container = Container(settings)
    try:
        yield container
    finally:
        await container.dispose()


async def _create_task(container: Container, *, description: str, mode: DecisionMode):
    return await container.task_repo.create(
        task_id=uuid.uuid4(),
        description=description,
        priority=0,
        project_id=None,
        agent_id=None,
        decision_mode=mode,
    )


async def test_run_finish_does_not_erase_the_waiting_state(solo_container: Container) -> None:
    """TC-203：Run 结束时任务正等人决策，状态必须停在等待态，不能被写成完成。"""
    task = await _create_task(solo_container, description="等人决定的活", mode=DecisionMode.MANUAL)
    await solo_container.task_repo.set_run_id(task.id, "run-waiting")
    await solo_container.task_repo.raise_decision(
        task.id,
        {
            "id": uuid.uuid4().hex,
            "question": "选哪个？",
            "options": [{"label": "甲"}],
            "status": "pending",
        },
    )
    await solo_container.task_repo.set_status(task.id, TaskStatus.WAITING_FOR_USER_DECISION)

    await solo_container.bus.publish(
        Events.DSH_RUN_COMPLETED, {"run_id": "run-waiting", "final_response": "做完了"}
    )

    after = await solo_container.task_repo.get(task.id)
    assert after.status == TaskStatus.WAITING_FOR_USER_DECISION.value
    # 也不该冒出"执行完成"的消息：那会让用户以为已经不用选了
    messages, _ = await solo_container.task_repo.list_messages(task.id)
    assert [message.kind for message in messages] == []


async def test_run_finish_settles_a_plain_running_task(solo_container: Container) -> None:
    """对照组：没有等待决策时，Run 终态正常回写并留一条结果消息。"""
    task = await _create_task(solo_container, description="普通任务", mode=DecisionMode.AUTO)
    await solo_container.task_repo.set_run_id(task.id, "run-plain")
    await solo_container.task_repo.set_status(task.id, TaskStatus.RUNNING)

    await solo_container.bus.publish(
        Events.DSH_RUN_FAILED, {"run_id": "run-plain", "error": "runtime 崩了"}
    )

    after = await solo_container.task_repo.get(task.id)
    assert after.status == TaskStatus.FAILED.value
    messages, _ = await solo_container.task_repo.list_messages(task.id)
    assert messages[-1].kind == "run_finished"
    assert "runtime 崩了" in messages[-1].content


async def test_run_finish_for_an_unknown_run_is_ignored(solo_container: Container) -> None:
    """DSH Run 也可以独立于任务发起：没有对应任务的事件必须被安静忽略。"""
    await solo_container.bus.publish(Events.DSH_RUN_COMPLETED, {"run_id": "run-nobody"})
    assert await solo_container.task_repo.get_by_run_id("run-nobody") is None
