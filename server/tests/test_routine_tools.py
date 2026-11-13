"""Bot 自调度工具测试（routine.create/list/update/pause/resume/delete）。

只验证工具层的接线与边界：run → task → bot 解析、入参校验、拒绝非 Bot 上下文，
以及对话 runner 是否把调用转给同一个 BotService。不启动 Run 引擎，
例程本身的调度语义由 `BotService` 的既有用例覆盖。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from flux.core.mcp.auth import AgentIdentity
from flux.core.mcp.tools import TOOLS
from flux.core.mcp.tools.base import ToolContext
from flux.core.mcp.tools.routine_tools import (
    ROUTINE_TOOL_NAMES,
    routine_create,
    routine_delete,
    routine_list,
    routine_pause,
    routine_resume,
    routine_update,
    run_routine_tool,
)
from flux.core.permissions.modes import tool_allowed
from flux.enums import Capability
from flux.errors import ValidationError

AGENT_ID = str(uuid.uuid4())
BOT_ID = uuid.uuid4()
TASK_ID = uuid.uuid4()
ROUTINE_ID = uuid.UUID(int=7)

_WRITE_TOOLS = {
    "routine.create",
    "routine.update",
    "routine.pause",
    "routine.resume",
    "routine.delete",
}


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
        self.updated: list[tuple[uuid.UUID, uuid.UUID, dict[str, Any]]] = []
        self.deleted: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def create_routine(self, bot_id: uuid.UUID, **kwargs: Any) -> Any:
        self.created.append((bot_id, kwargs))
        return SimpleNamespace(to_dict=lambda: {"id": str(ROUTINE_ID), "bot_id": str(bot_id)})

    async def list_routines(self, bot_id: uuid.UUID) -> list[Any]:
        return [
            SimpleNamespace(
                to_dict=lambda: {
                    "id": str(ROUTINE_ID),
                    "bot_id": str(bot_id),
                    "event_triggers": [],
                    "next_run_at": None,
                    "enabled": True,
                }
            )
        ]

    async def update_routine(
        self, bot_id: uuid.UUID, routine_id: uuid.UUID, changes: dict[str, Any]
    ) -> Any:
        self.updated.append((bot_id, routine_id, changes))
        return SimpleNamespace(
            to_dict=lambda: {
                "id": str(routine_id),
                "bot_id": str(bot_id),
                "enabled": changes.get("enabled", True),
            }
        )

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
    assert {
        "routine.create",
        "routine.list",
        "routine.update",
        "routine.pause",
        "routine.resume",
        "routine.delete",
    } <= set(TOOLS)


def test_routine_writes_are_hidden_in_plan_mode() -> None:
    # Plan 白名单是「显式放行」：例程工具整体不在名单里（读列表也不给）
    assert tool_allowed("plan", "routine.list") is False
    for name in _WRITE_TOOLS:
        assert tool_allowed("plan", name) is False
    for name in _WRITE_TOOLS:
        assert tool_allowed("unattended", name) is True
    assert tool_allowed("unattended", "routine.list") is True


async def test_create_cron_routine_binds_current_bot() -> None:
    ctx = _ctx()
    await routine_create(
        ctx, {"name": "晨报", "instruction": "汇总昨天的任务", "schedule": "0 9 * * *"}
    )
    bot_id, kwargs = ctx.container.bots.created[0]  # type: ignore[attr-defined]
    assert bot_id == BOT_ID
    assert kwargs == {
        "name": "晨报",
        "instruction": "汇总昨天的任务",
        "schedule": "0 9 * * *",
        "run_at": None,
        "event_triggers": None,
        "enabled": True,
    }


async def test_create_one_off_routine_passes_run_at() -> None:
    ctx = _ctx()
    await routine_create(
        ctx,
        {
            "name": "只跑一次",
            "instruction": "发一封邮件",
            "run_at": "2026-12-01T09:00:00+08:00",
        },
    )
    _, kwargs = ctx.container.bots.created[0]  # type: ignore[attr-defined]
    assert kwargs["schedule"] is None
    assert kwargs["run_at"] == "2026-12-01T09:00:00+08:00"


async def test_create_event_triggered_routine_passes_events() -> None:
    ctx = _ctx()
    await routine_create(
        ctx,
        {
            "name": "收到邮件",
            "instruction": "总结邮件",
            "event_triggers": ["email.received", "webhook.deploy"],
        },
    )
    _, kwargs = ctx.container.bots.created[0]  # type: ignore[attr-defined]
    assert kwargs["event_triggers"] == ["email.received", "webhook.deploy"]
    assert kwargs["schedule"] is None
    assert kwargs["run_at"] is None


async def test_create_requires_one_trigger() -> None:
    with pytest.raises(ValidationError):
        await routine_create(_ctx(), {"name": "缺触发", "instruction": "跑一轮"})


async def test_create_rejects_schedule_and_run_at_together() -> None:
    with pytest.raises(ValidationError):
        await routine_create(
            _ctx(),
            {
                "name": "冲突",
                "instruction": "跑一轮",
                "schedule": "0 9 * * *",
                "run_at": "2026-12-01T09:00:00+08:00",
            },
        )


async def test_list_routine_returns_current_bot_routines() -> None:
    result = await routine_list(_ctx(), {})
    assert result["routines"][0]["bot_id"] == str(BOT_ID)
    assert {"event_triggers", "next_run_at", "enabled"} <= set(result["routines"][0])


async def test_update_routine_passes_id_and_changes() -> None:
    ctx = _ctx()
    await routine_update(
        ctx,
        {
            "routine_id": str(ROUTINE_ID),
            "name": "新名字",
            "schedule": None,
            "enabled": False,
        },
    )
    assert ctx.container.bots.updated == [  # type: ignore[attr-defined]
        (BOT_ID, ROUTINE_ID, {"name": "新名字", "schedule": None, "enabled": False})
    ]


async def test_update_routine_requires_a_field() -> None:
    with pytest.raises(ValidationError):
        await routine_update(_ctx(), {"routine_id": str(ROUTINE_ID)})


async def test_pause_and_resume_toggle_enabled() -> None:
    ctx = _ctx()
    await routine_pause(ctx, {"routine_id": str(ROUTINE_ID)})
    await routine_resume(ctx, {"routine_id": str(ROUTINE_ID)})
    assert ctx.container.bots.updated == [  # type: ignore[attr-defined]
        (BOT_ID, ROUTINE_ID, {"enabled": False}),
        (BOT_ID, ROUTINE_ID, {"enabled": True}),
    ]


async def test_delete_routine_passes_id() -> None:
    ctx = _ctx()
    result = await routine_delete(ctx, {"routine_id": str(ROUTINE_ID)})
    assert result == {"deleted": True, "routine_id": str(ROUTINE_ID)}
    assert ctx.container.bots.deleted == [(BOT_ID, ROUTINE_ID)]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("handler", "params"),
    [
        (routine_update, {"routine_id": "not-a-uuid", "name": "x"}),
        (routine_pause, {"routine_id": "not-a-uuid"}),
        (routine_resume, {"routine_id": "not-a-uuid"}),
        (routine_delete, {"routine_id": "not-a-uuid"}),
    ],
)
async def test_rejects_foreign_routine_id_format(handler: Any, params: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        await handler(_ctx(), params)


async def test_requires_run_context() -> None:
    with pytest.raises(ValidationError):
        await routine_create(
            _ctx(run_id=None), {"name": "x", "instruction": "y", "schedule": "0 9 * * *"}
        )


async def test_rejects_non_bot_task() -> None:
    with pytest.raises(ValidationError):
        await routine_list(_ctx(task_id=None, bot_id=None), {})


# --- 对话 runner ---


class _ConversationBots:
    def __init__(self) -> None:
        self.created: list[tuple[uuid.UUID, dict[str, Any]]] = []
        self.updated: list[tuple[uuid.UUID, uuid.UUID, dict[str, Any]]] = []
        self.deleted: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def create_routine(self, bot_id: uuid.UUID, **kwargs: Any) -> Any:
        self.created.append((bot_id, kwargs))
        return SimpleNamespace(to_dict=lambda: {"id": str(ROUTINE_ID)})

    async def list_routines(self, bot_id: uuid.UUID) -> list[Any]:
        return [SimpleNamespace(to_dict=lambda: {"id": str(ROUTINE_ID)})]

    async def update_routine(
        self, bot_id: uuid.UUID, routine_id: uuid.UUID, changes: dict[str, Any]
    ) -> Any:
        self.updated.append((bot_id, routine_id, changes))
        return SimpleNamespace(to_dict=lambda: {"id": str(routine_id)})

    async def delete_routine(self, bot_id: uuid.UUID, routine_id: uuid.UUID) -> None:
        self.deleted.append((bot_id, routine_id))


async def test_conversation_runner_uses_same_service() -> None:
    service = _ConversationBots()
    bot = SimpleNamespace(id=BOT_ID)

    await run_routine_tool(
        service,
        bot,
        "routine_create",
        {"name": "晨报", "instruction": "汇总", "schedule": "0 9 * * *"},
    )
    await run_routine_tool(service, bot, "routine_list", {})
    await run_routine_tool(
        service, bot, "routine_update", {"routine_id": str(ROUTINE_ID), "name": "新名字"}
    )
    await run_routine_tool(service, bot, "routine_pause", {"routine_id": str(ROUTINE_ID)})
    await run_routine_tool(service, bot, "routine_resume", {"routine_id": str(ROUTINE_ID)})
    await run_routine_tool(service, bot, "routine_delete", {"routine_id": str(ROUTINE_ID)})

    assert service.created[0][0] == BOT_ID
    assert service.created[0][1]["schedule"] == "0 9 * * *"
    assert service.updated == [
        (BOT_ID, ROUTINE_ID, {"name": "新名字"}),
        (BOT_ID, ROUTINE_ID, {"enabled": False}),
        (BOT_ID, ROUTINE_ID, {"enabled": True}),
    ]
    assert service.deleted == [(BOT_ID, ROUTINE_ID)]


async def test_conversation_runner_rejects_unknown_tool() -> None:
    with pytest.raises(ValidationError):
        await run_routine_tool(_ConversationBots(), SimpleNamespace(id=BOT_ID), "routine_nope", {})


def test_conversation_tool_names_are_underscored() -> None:
    assert {
        "routine_create",
        "routine_list",
        "routine_update",
        "routine_pause",
        "routine_resume",
        "routine_delete",
    } == ROUTINE_TOOL_NAMES
