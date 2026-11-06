"""Context 打包测试（文档 §6：分区 / 分级、预算、失效、任务间污染）。

Context 是 Flux 的基础设施，Agent 的主路径靠 `context.get` 拿工程事实，而不是把
历史对话无条件塞进 prompt。这里验证打包器的三条底线：

1. 分层清楚：任务（P0/L1）、项目 Brain（P0/L1）、近期提案（P1/L2）各归各的 entry；
2. 预算受控：必留区（P0）**一条不减**；可压区（P1）装不下先编码（L2），
   编码后仍装不下才整条退化成引用（L3），绝不半截截断；
3. 不串味：A 任务的提案不会出现在 B 任务的上下文里；过期提案带着状态出现，
   提醒 Agent 它是快照而不是事实。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest

from flux.config import Settings
from flux.container import Container
from flux.core.context.budget import Zone
from flux.core.context.tiers import Tier
from flux.core.mcp.context_packager import (
    DEFAULT_BUDGET_CHARS,
    PROVENANCE_NOTE,
    _apply_budget,
    _entry,
    package_context,
)
from flux.core.virtual_workspace.service import EXPIRE_REASON_TIMEOUT
from flux.enums import DecisionMode, DshRunStatus, VirtualChangeStatus
from flux.models.task import Task

ORIGINAL = "def list_todos():\n    return None\n"
PROPOSED = "def list_todos():\n    return []\n"


@pytest.fixture()
async def ctx_container(settings: Settings, db_schema: None) -> AsyncIterator[Container]:
    """用完显式 dispose：避免 aiosqlite 的连接线程活过事件循环。"""
    container = Container(settings)
    try:
        yield container
    finally:
        await container.dispose()


async def _project(container: Container, *, name: str = "待办服务") -> uuid.UUID:
    project = await container.brain.create_project(name=name, repository="/srv/todo")
    return project.id


async def _memory(container: Container, project_id: uuid.UUID, content: str) -> None:
    from flux.enums import BrainSection

    await container.brain.write(project_id, section=BrainSection.CODING_RULES, content=content)


async def _task(container: Container, project_id: uuid.UUID, *, description: str) -> Task:
    return await container.task_repo.create(
        task_id=uuid.uuid4(),
        description=description,
        priority=0,
        project_id=project_id,
        agent_id=None,
        decision_mode=DecisionMode.AUTO,
    )


async def _propose(
    container: Container,
    *,
    file_path: str,
    task_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> str:
    """落一条提案并返回它的 file_path（打包结果里靠路径辨认）。"""
    change = await container.workspace.propose(
        file_path=file_path,
        original_content=ORIGINAL,
        proposed_content=PROPOSED,
        task_id=task_id,
        project_id=project_id,
        agent_source="developer-agent",
        summary=f"改动 {file_path}",
    )
    return change.file_path


async def test_pack_carries_l1_l0_l2_with_provenance(ctx_container: Container) -> None:
    """一个任务包同时带上任务事实、项目 Brain 与近期提案，并声明采信规则。"""
    project = await _project(ctx_container)
    await _memory(ctx_container, project, "测试命令固定为 python3 -m pytest")
    task = await _task(ctx_container, project, description="给待办服务加按完成状态过滤")
    await _propose(ctx_container, file_path="todo_service/store.py", task_id=task.id)

    pack = await package_context(ctx_container, task_id=str(task.id))

    # 任务自带项目归属：不传 project_id 也自动带上该项目的 Brain
    assert pack["task_id"] == str(task.id)
    assert pack["project_id"] == str(project)
    assert [entry["kind"] for entry in pack["entries"]] == ["task", "brain", "proposals"]
    assert [entry["ref"].split(":")[0] for entry in pack["entries"]] == [
        "task",
        "brain",
        "proposals",
    ]

    assert "# 当前任务（L1）" in pack["content"]
    assert "python3 -m pytest" in pack["content"]
    assert "todo_service/store.py" in pack["content"]
    assert pack["budget"] == DEFAULT_BUDGET_CHARS
    # 分区 / 等级（§6.2 / §6.3）：任务与 Brain 是必留区原文，提案是可压区编码形态
    assert [(entry["kind"], entry["zone"], entry["level"]) for entry in pack["entries"]] == [
        ("task", "P0", "L1"),
        ("brain", "P0", "L1"),
        ("proposals", "P1", "L1"),
    ]
    # 分层（§6.5）：任务与 Brain 是当轮事实（热层，永不替换），提案是中段温层（吃紧时先编码）
    assert [entry["tier"] for entry in pack["entries"]] == ["hot", "hot", "warm"]
    # 预算账（§6.1）：必留区 + 余量；装得下时 used 不超预算
    assert pack["required"] == sum(
        entry["chars"] for entry in pack["entries"] if entry["zone"] == "P0"
    )
    assert pack["available"] == pack["budget"] - pack["required"]
    assert pack["used"] == pack["required"] + sum(
        entry["chars"] for entry in pack["entries"] if entry["zone"] == "P1"
    )
    assert pack["used"] <= pack["budget"]
    assert pack["dropped_count"] == 0 and pack["dropped_refs"] == []
    assert pack["dropped"] == []
    # 回补率观测（§6.7）：本次一条没裁，给出去的引用数为 0
    assert pack["backfill"] == {"offered": 0, "fetched": 0, "rate": 0.0, "rollback": False}
    # §5.6 度量：必留项一条不漏、S5 一次不差，两条红线都在（另三项在别处量）
    assert pack["metrics"]["required_hit_rate"] == 1.0
    assert pack["metrics"]["reconstruction_rate"] == 1.0
    assert pack["metrics"]["healthy"] is True
    assert pack["note"] == PROVENANCE_NOTE
    assert "断言不等于事实" in pack["content"] or "断言不等于事实" in pack["note"]


async def test_required_zone_is_never_trimmed_even_when_budget_is_tight(
    ctx_container: Container,
) -> None:
    """必留区（P0）先占预算、一条不减；可压区（P1）装不下就整条退化成引用（§6.2）。"""
    project = await _project(ctx_container)
    await _memory(ctx_container, project, "测试命令固定为 python3 -m pytest")
    task = await _task(ctx_container, project, description="给待办服务加按完成状态过滤")
    await _propose(ctx_container, file_path="todo_service/store.py", task_id=task.id)

    pack = await package_context(ctx_container, task_id=str(task.id), budget=64)

    # 任务与 Brain 一条没少，即使它们自身已经把 64 字符的预算撑爆
    assert [entry["kind"] for entry in pack["entries"]] == ["task", "brain"]
    assert all(entry["level"] == "L1" for entry in pack["entries"])
    assert pack["used"] == pack["required"] > 64
    assert pack["available"] == 0
    # 余量为零：提案既装不下原文、也无字符可供编码，于是整条退化成 L3 引用
    assert pack["dropped_count"] == 1
    assert pack["metrics"]["dropped_count"] == 1
    # 回补率观测（§6.7）：裁掉 1 条即给出 1 个引用，尚无取回
    assert pack["backfill"]["offered"] == 1
    assert pack["backfill"]["fetched"] == 0
    assert pack["dropped_refs"] and all(":" in ref for ref in pack["dropped_refs"])
    assert "todo_service/store.py" not in pack["content"]
    # 每条被裁引用都配一个取回提示：第三档降级有消费方，不再是断的（§7.1 / §11 / §13-3）
    assert pack["dropped"]
    for item in pack["dropped"]:
        assert item["ref"] in pack["dropped_refs"]
        assert item["retrieve"].startswith(("context.get", "context.fetch", "workspace.diff"))


async def test_one_task_proposal_never_leaks_into_another(ctx_container: Container) -> None:
    """连续做多个相关任务时，C 任务不该继承 A 任务的提案（§6 测试重点）。"""
    project = await _project(ctx_container)
    task_a = await _task(ctx_container, project, description="任务 A：改 store")
    task_c = await _task(ctx_container, project, description="任务 C：改接口层")
    await _propose(ctx_container, file_path="todo_service/store.py", task_id=task_a.id)
    await _propose(ctx_container, file_path="api/routes.py", task_id=task_c.id)

    pack_a = await package_context(ctx_container, task_id=str(task_a.id))
    pack_c = await package_context(ctx_container, task_id=str(task_c.id))

    assert "todo_service/store.py" in pack_a["content"]
    assert "api/routes.py" not in pack_a["content"]
    assert "api/routes.py" in pack_c["content"]
    assert "todo_service/store.py" not in pack_c["content"]
    # Project Brain 是项目级事实，同一项目的任务共享它是预期行为
    assert "待办服务" in pack_a["content"] and "待办服务" in pack_c["content"]


async def test_expired_proposal_is_shown_as_stale(ctx_container: Container) -> None:
    """过期提案仍出现在 L2 里，但带着 expired 状态——快照就是快照，不能当事实用。"""
    project = await _project(ctx_container)
    task = await _task(ctx_container, project, description="给待办服务加过滤")
    change = await ctx_container.workspace.propose(
        file_path="todo_service/store.py",
        original_content=ORIGINAL,
        proposed_content=PROPOSED,
        task_id=task.id,
        agent_source="developer-agent",
    )
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    await ctx_container.proposal_repo.set_expires_at(str(change.id), past)
    expired = await ctx_container.workspace.expire(str(change.id), reason=EXPIRE_REASON_TIMEOUT)
    assert expired.status == "expired"

    pack = await package_context(ctx_container, task_id=str(task.id))

    assert "todo_service/store.py" in pack["content"]
    assert "[expired]" in pack["content"]


async def test_pack_carries_previous_run_termination_reason(ctx_container: Container) -> None:
    """上一轮 Run 的终态与失败原因必须回到下一轮的上下文里（否则 Agent 不知道自己为何失败）。"""
    project = await _project(ctx_container)
    task = await _task(ctx_container, project, description="修登录接口")
    run_id = uuid.uuid4().hex
    await ctx_container.run_repo.create(
        run_id=run_id,
        session_id="s",
        instruction="修登录接口",
        status=DshRunStatus.RUNNING,
        task_id=task.id,
    )
    await ctx_container.run_repo.set_status(
        run_id,
        DshRunStatus.FAILED,
        error="pytest 失败：3 errors",
        finish_reason="error",
        finished=True,
    )

    pack = await package_context(ctx_container, task_id=str(task.id))

    kinds = [entry["kind"] for entry in pack["entries"]]
    assert "runs" in kinds
    assert "最近 Run" in pack["content"]
    assert "[failed]" in pack["content"] and run_id in pack["content"]
    assert "pytest 失败：3 errors" in pack["content"]


async def test_pack_without_task_only_carries_project_facts(ctx_container: Container) -> None:
    """只给 project_id 时不带任何任务上下文——调用方要什么就给什么，不擅自补。"""
    project = await _project(ctx_container)
    await _memory(ctx_container, project, "测试命令固定为 python3 -m pytest")
    await _task(ctx_container, project, description="不该出现在包里的任务")
    # 项目级提案（没挂任务）：只给 project_id 时应能带上，但不带任何任务上下文
    await _propose(ctx_container, file_path="todo_service/store.py", project_id=project)

    pack = await package_context(ctx_container, project_id=str(project))

    assert pack["task_id"] is None
    assert [entry["kind"] for entry in pack["entries"]] == ["brain", "proposals"]
    assert "不该出现在包里的任务" not in pack["content"]
    assert "todo_service/store.py" in pack["content"]


def test_apply_budget_orders_hot_then_warm_then_cold() -> None:
    """装配次序按层（§6.5）：即便调用方逆序传入，输出也按 热 → 温 → 冷 归位。

    这是**稳定排序**的直接判据——没有它，输出次序就会跟着调用方给的次序走。
    """
    entries = [
        _entry(kind="steps", title="远段", text="c" * 10, ref="steps:1"),
        _entry(kind="proposals", title="中段", text="w" * 10, ref="proposals:1"),
        _entry(kind="task", title="任务", text="t" * 10, ref="task:1"),
        _entry(kind="brain", title="项目", text="b" * 10, ref="brain:1"),
    ]

    result = _apply_budget(entries, budget=10_000, project_id=None, task_id=None, query="")

    assert [(entry["kind"], entry["tier"]) for entry in result["entries"]] == [
        ("task", "hot"),
        ("brain", "hot"),
        ("proposals", "warm"),
    ]
    # 远段是可选区（P2）：只留 ref，不占投喂
    assert result["dropped_refs"] == ["steps:1"]


def test_apply_budget_prefers_warm_over_cold_under_pressure() -> None:
    """预算只够装一条时，温层先占（§6.5）：冷层最后才降级为 ref。

    把温 / 冷两条都放进可压区（P1），"谁先吃预算"才由**层**而非分区决定；调用方故意
    把冷层排在前面，证明归位靠的是排序而不是传入次序。
    """
    warm = {
        "kind": "proposals",
        "title": "中段",
        "text": "w" * 600,
        "ref": "proposals:warm",
        "zone": Zone.P1,
        "tier": Tier.WARM,
    }
    cold = {
        "kind": "proposals",
        "title": "远段",
        "text": "c" * 600,
        "ref": "proposals:cold",
        "zone": Zone.P1,
        "tier": Tier.COLD,
    }

    result = _apply_budget([cold, warm], budget=600, project_id=None, task_id=None, query="")

    assert [(entry["ref"], entry["tier"], entry["level"]) for entry in result["entries"]] == [
        ("proposals:warm", "warm", "L1")
    ]
    assert result["dropped_refs"] == ["proposals:cold"]


def test_apply_budget_encodes_warm_and_drops_cold() -> None:
    """温层装不下就先编码（L2），冷层留着最后降级为 ref——不半截截断（§5.3 / §6.5）。"""
    sample = "\n".join(
        f"- [proposed] todo_service/store_{index}.py（+1/-1，1 个改动块） id={index}"
        for index in range(40)
    )
    entries = [
        _entry(kind="proposals", title="中段", text=sample, ref="proposals:sample"),
        _entry(kind="steps", title="远段", text="c" * 2_000, ref="steps:1"),
    ]

    result = _apply_budget(entries, budget=1_000, project_id=None, task_id=None, query="改 store")

    # 温层过 S5 自校验后以编码形态（L2）留下；冷层是可选区，只留 ref
    assert [(entry["kind"], entry["tier"], entry["level"]) for entry in result["entries"]] == [
        ("proposals", "warm", "L2")
    ]
    assert result["entries"][0]["chars"] <= 1_000
    assert result["dropped_refs"] == ["steps:1"]
    # §5.6 红线：跑过的自校验一次不差
    assert result["metrics"]["reconstruction_rate"] == 1.0
    assert result["metrics"]["healthy"] is True


async def test_pack_carries_gate_command_as_p0(ctx_container: Container) -> None:
    """配置了 test_command，就必须出现在必留区：agent 交卷前要先自己跑这一条。"""
    ctx_container.settings.test_command = "bash scripts/test.sh"
    project = await _project(ctx_container)
    task = await _task(ctx_container, project, description="加筛选")

    pack = await package_context(ctx_container, task_id=str(task.id))

    kinds = [entry["kind"] for entry in pack["entries"]]
    assert "verify" in kinds
    verify = pack["entries"][kinds.index("verify")]
    assert (verify["zone"], verify["level"], verify["tier"]) == ("P0", "L1", "hot")
    # entry 只暴露元信息，正文在 pack["content"] 里
    assert "# 验证命令（L1，Apply 之后平台会真跑这条）" in pack["content"]
    assert "bash scripts/test.sh" in pack["content"]
    assert "proposal.preview" in pack["content"]


async def test_pack_replays_apply_error_of_failed_proposal(ctx_container: Container) -> None:
    """上一轮挂在测试上的提案，下一轮必须看得到失败原因，否则同一个坑会重犯。"""
    project = await _project(ctx_container)
    task = await _task(ctx_container, project, description="加筛选")
    change = await ctx_container.workspace.propose(
        file_path="todo_service/store.py",
        original_content=ORIGINAL,
        proposed_content=PROPOSED,
        task_id=task.id,
        agent_source="developer-agent",
        summary="加筛选",
    )
    await ctx_container.proposal_repo.set_apply_result(
        change.id,
        status=VirtualChangeStatus.FAILED,
        apply_error="AssertionError: expected 2 items, got 0（exit=1，bash scripts/test.sh）",
    )

    pack = await package_context(ctx_container, task_id=str(task.id))

    assert "expected 2 items, got 0" in pack["content"]
    assert "失败原因" in pack["content"]
