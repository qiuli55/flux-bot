"""Bot 侧审批规则工具：只能新增/查看，不能删除、停用或放宽。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from flux.core.mcp.auth import AgentIdentity
from flux.core.mcp.tools import TOOLS
from flux.core.mcp.tools.approval_rule_tools import (
    APPROVAL_RULE_TOOL_NAMES,
    approval_rule_add,
    approval_rule_list,
    run_approval_rule_tool,
)
from flux.core.mcp.tools.base import ToolContext
from flux.enums import Capability, DshRunStatus
from flux.errors import BadRequestError, ValidationError


@dataclass
class _BotRun:
    bot: object
    task: object
    ctx: ToolContext


async def _bot_run(container, *, cloud_user_id: str = "2") -> _BotRun:
    bot = await container.bots.create(name=f"规则Bot-{uuid.uuid4().hex[:6]}")
    task = await container.task_repo.create(
        task_id=uuid.uuid4(),
        description="规则工具用例",
        priority=1,
        project_id=None,
        agent_id=None,
        bot_id=bot.id,
        cloud_user_id=cloud_user_id,
    )
    run_id = uuid.uuid4().hex
    agent_id = str(uuid.uuid4())
    await container.run_repo.create(
        run_id=run_id,
        session_id="sess",
        instruction="add a rule",
        status=DshRunStatus.RUNNING,
        agent_id=agent_id,
        task_id=task.id,
        permission_mode="normal",
    )
    ctx = ToolContext(
        container=container,
        identity=AgentIdentity(
            agent_id=agent_id,
            token_id="t",
            scopes=frozenset({Capability.FILE_READ, Capability.FILE_WRITE}),
            run_id=run_id,
        ),
        tool_name="approval_rule.add",
    )
    return _BotRun(bot=bot, task=task, ctx=ctx)


def test_only_add_and_list_tools_exist() -> None:
    assert "approval_rule.add" in TOOLS
    assert "approval_rule.list" in TOOLS
    assert "approval_rule.update" not in TOOLS
    assert "approval_rule.delete" not in TOOLS
    assert {"approval_rule_add", "approval_rule_list"} == APPROVAL_RULE_TOOL_NAMES


async def test_bot_adds_and_lists_rule_for_its_owner(apply_container) -> None:
    seed = await _bot_run(apply_container, cloud_user_id="2")
    added = await approval_rule_add(
        seed.ctx,
        {"type": "command_pattern", "params": {"pattern": "rm -rf *", "style": "glob"}},
    )
    rule = added["rule"]
    assert rule["cloud_user_id"] == "2"
    assert rule["created_by_kind"] == "bot"
    assert rule["created_by_bot_id"] == str(seed.bot.id)
    assert rule["enabled"] is True

    listed = await approval_rule_list(seed.ctx, {})
    assert [row["id"] for row in listed["rules"]] == [rule["id"]]
    assert listed["count"] == 1


async def test_bot_cannot_disable_a_rule(apply_container) -> None:
    seed = await _bot_run(apply_container)
    with pytest.raises(ValidationError) as excinfo:
        await approval_rule_add(
            seed.ctx,
            {"type": "tool_match", "params": {"tool_name": "terminal.execute"}, "enabled": False},
        )
    assert excinfo.value.details.get("bot_may_not_loosen") is True
    assert await apply_container.approval_rules.list("2") == []


async def test_bot_cannot_choose_another_users_scope(apply_container) -> None:
    seed = await _bot_run(apply_container, cloud_user_id="2")
    with pytest.raises(ValidationError):
        await approval_rule_add(
            seed.ctx,
            {
                "type": "tool_match",
                "params": {"tool_name": "terminal.execute"},
                "cloud_user_id": "3",
            },
        )
    assert await apply_container.approval_rules.list("3") == []


async def test_bot_run_without_a_bot_is_rejected(apply_container) -> None:
    task = await apply_container.task_repo.create(
        task_id=uuid.uuid4(),
        description="普通对话",
        priority=1,
        project_id=None,
        agent_id=None,
        cloud_user_id="2",
    )
    run_id = uuid.uuid4().hex
    agent_id = str(uuid.uuid4())
    await apply_container.run_repo.create(
        run_id=run_id,
        session_id="sess",
        instruction="x",
        status=DshRunStatus.RUNNING,
        agent_id=agent_id,
        task_id=task.id,
        permission_mode="normal",
    )
    ctx = ToolContext(
        container=apply_container,
        identity=AgentIdentity(
            agent_id=agent_id,
            token_id="t",
            scopes=frozenset({Capability.FILE_WRITE}),
            run_id=run_id,
        ),
        tool_name="approval_rule.add",
    )
    with pytest.raises(BadRequestError):
        await approval_rule_add(
            ctx, {"type": "tool_match", "params": {"tool_name": "terminal.execute"}}
        )


async def test_list_is_scoped_to_the_bots_owner(apply_container) -> None:
    seed_two = await _bot_run(apply_container, cloud_user_id="2")
    await approval_rule_add(
        seed_two.ctx,
        {"type": "outbound_message", "params": {"channel": "feishu"}},
    )
    await apply_container.approval_rules.create(
        cloud_user_id="3",
        rule_type="outbound_message",
        params={"channel": "wecom"},
    )

    listed = await approval_rule_list(seed_two.ctx, {})
    assert [row["cloud_user_id"] for row in listed["rules"]] == ["2"]


async def test_conversation_path_cannot_update_or_delete(apply_container) -> None:
    seed = await _bot_run(apply_container)
    rule = await run_approval_rule_tool(
        apply_container.approval_rules,
        seed.bot,
        "approval_rule_add",
        {"type": "tool_match", "params": {"tool_name": "terminal.execute"}},
        cloud_user_id="2",
    )
    assert rule["rule"]["created_by_kind"] == "bot"
    assert rule["rule"]["created_by_bot_id"] == str(seed.bot.id)

    listed = await run_approval_rule_tool(
        apply_container.approval_rules, seed.bot, "approval_rule_list", {}, cloud_user_id="2"
    )
    assert listed["count"] == 1

    for forbidden in ("approval_rule_update", "approval_rule_delete", "approval_rule_disable"):
        with pytest.raises(ValidationError):
            await run_approval_rule_tool(
                apply_container.approval_rules,
                seed.bot,
                forbidden,
                {"rule_id": rule["rule"]["id"]},
                cloud_user_id="2",
            )


async def test_conversation_path_requires_a_known_user(apply_container) -> None:
    seed = await _bot_run(apply_container)
    with pytest.raises(BadRequestError):
        await run_approval_rule_tool(
            apply_container.approval_rules,
            seed.bot,
            "approval_rule_add",
            {"type": "tool_match", "params": {"tool_name": "terminal.execute"}},
            cloud_user_id=None,
            owner_cloud_user_id=None,
        )
