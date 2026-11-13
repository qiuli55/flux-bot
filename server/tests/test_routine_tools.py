"""Bot 自调度工具测试（routine.create / routine.list / routine.delete）。

只验证工具层的接线与边界：run → task → bot 解析、入参校验、拒绝非 Bot 上下文。
不启动 Run 引擎，例程本身由 `BotService` 的既有用例覆盖。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from flux.core.mcp.auth import AgentIdentity
from flux.core.mcp.tools import TOOLS
from flux.core.mcp.tools.base import ToolContext
from flux.core.mcp.tools.routine_tools import routine_create, routine_delete, routine_list
from flux.core.permissions.modes import tool_allowed
from flux.enums import Capability
from flux.errors import ValidationError

AGENT_ID = str(uuid.uuid4())
BOT_ID = uuid.uuid4()
TASK_ID = uuid.uuid4()


class _RunRepo:
    def __init__(self, task_id: uuid.UUID | None) -> None:
        self._task_id = task_id

    async def get(self, run_id: str) -> Any:
        return SimpleNamespace(task_id=self._task_id, agent_id=AGENT_ID)


class _TaskRepo:
    def __init__(self, bot_id: uuid.UUID | None) -> None:
        self._bot_id = bot_id

    async def get(self, task_id: uuid.UUID) -> Any:
        return SimpleNamespace(bot_id=self._bot_id)


class _Bots:
    def __init__(self) -> None:
        self.created: list[tuple[uuid.UUID, dict[str, Any]]] = []
        self.deleted: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def create_routine(self, bot_id: uuid.UUID, **kwargs: Any) -> Any:
        self.created.append((bot_id, kwargs))
        return SimpleNamespace(
            to_dict=lambda: {
                "id": str(uuid.uuid4()),
                "bot_id": str(bot_id),
                "name": kwargs["name"],
                "instruction": kwargs["instruction"],
                "schedule": kwargs["schedule"],
                "enabled": kwargs["enabled"],
            }
        )

    async def list_routines(self, bot_id: uuid.UUID) -> list[Any]:
        return [SimpleNamespace(to_dict=lambda: {"id": "r-1", "bot_id": str(bot_id)})]

    async def delete_routine(self, bot_id: uuid.UUID, routine_id: uuid.UUID) -> None:
        self.deleted.append((bot_id, routine_id))


def _ctx(
    *,
    run_id: str | None = "run-1",
    task_id: uuid.UUID | None = TASK_ID,
    bot_id: uuid.UUID | None = BOT_ID,
) -> ToolContext:
    container = SimpleNamespace(
        run_repo=_RunRepo(task_id),
        task_repo=_TaskRepo(bot_id),
        bots=_Bots(),
    )
    return ToolContext(
        container=container,  # type: ignore[arg-type]
        identity=AgentIdentity(
            agent_id=AGENT_ID,
            token_id="t",
            scopes=frozenset({Capability.FILE_READ, Capability.FILE_WRITE}),
            run_id=run_id,
            permission_mode="unattended",
        ),
        tool_name="routine.create",
    )


def test_routine_tools_are_registered() -> None:
    assert {"routine.create", "routine.list", "routine.delete"} <= set(TOOLS)


def test_routine_writes_are_hidden_in_plan_mode() -> None:
    # Plan 白名单是「显式放行」：例程工具整体不在名单里（读列表也不给）
    assert tool_allowed("plan", "routine.create") is False
    assert tool_allowed("plan", "routine.delete") is False
    assert tool_allowed("plan", "routine.list") is False
    assert tool_allowed("unattended", "routine.create") is True


async def test_create_routine_binds_current_bot() -> None:
    ctx = _ctx()
    result = await routine_create(
        ctx, {"name": "晨报", "instruction": "汇总昨天的任务", "schedule": "0 9 * * *"}
    )
    assert result["name"] == "晨报"
    assert result["schedule"] == "0 9 * * *"
    bot_id, kwargs = ctx.container.bots.created[0]  # type: ignore[attr-defined]
    assert bot_id == BOT_ID
    assert kwargs == {
        "name": "晨报",
        "instruction": "汇总昨天的任务",
        "schedule": "0 9 * * *",
        "enabled": True,
    }


async def test_list_routine_returns_current_bot_routines() -> None:
    result = await routine_list(_ctx(), {})
    assert result["routines"][0]["bot_id"] == str(BOT_ID)


async def test_delete_routine_passes_id() -> None:
    routine_id = uuid.UUID(int=1)
    ctx = _ctx()
    result = await routine_delete(ctx, {"routine_id": str(routine_id)})
    assert result == {"deleted": True, "routine_id": str(routine_id)}
    assert ctx.container.bots.deleted == [(BOT_ID, routine_id)]  # type: ignore[attr-defined]


async def test_create_requires_schedule() -> None:
    with pytest.raises(ValidationError):
        await routine_create(_ctx(), {"name": "缺定时", "instruction": "跑一轮"})


async def test_requires_run_context() -> None:
    with pytest.raises(ValidationError):
        await routine_create(
            _ctx(run_id=None), {"name": "x", "instruction": "y", "schedule": "0 9 * * *"}
        )


async def test_rejects_non_bot_task() -> None:
    with pytest.raises(ValidationError):
        await routine_list(_ctx(task_id=None, bot_id=None), {})


async def test_rejects_foreign_routine_id_format() -> None:
    with pytest.raises(ValidationError):
        await routine_delete(_ctx(), {"routine_id": "not-a-uuid"})
