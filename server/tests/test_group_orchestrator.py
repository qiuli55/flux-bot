"""群聊编排器行为测试：群主发言权、`[PASS]` 沉默、per-bot 模型与分层记忆。

口径：编排器只依赖仓储与模型通道，测试用内存里的假路由器返回固定脚本，
不触网、不跑真实 Agent；记忆压缩在未注入 summarizer 时走确定性摘录。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from flux.container import Container
from flux.core.group.orchestrator import (
    MEMORY_TRIGGER_CHARS,
    GroupOrchestrator,
    _parse_decision,
)
from flux.core.group.repository import GroupMessageDraft
from flux.core.model_gateway.base import ChatMessage, ChatResult, TokenUsage
from flux.enums import ModelProvider


class _FakeRouter:
    """按 bot 名字返回脚本内容的假路由器，并记录调用与参数。"""

    def __init__(self, script: dict[str, str]) -> None:
        self._script = script
        self.called: list[str] = []
        self.kwargs: list[dict[str, Any]] = []

    async def chat(self, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        system = messages[0].content
        name = system.split("你是「", 1)[1].split("」", 1)[0]
        self.called.append(name)
        self.kwargs.append(kwargs)
        return ChatResult(
            content=self._script.get(name, "[PASS]"),
            provider=ModelProvider.LOCAL,
            model=str(kwargs.get("model") or "fake-model"),
            usage=TokenUsage(),
            latency_ms=0,
        )


async def _make_bot(container: Container, name: str, *, model: str | None = None) -> uuid.UUID:
    bot = await container.bots.create(
        name=name,
        description=f"{name} 的对话",
        model=model,
    )
    return bot.id


async def _make_group(
    container: Container,
    name: str,
    members: list[uuid.UUID],
    *,
    admin: uuid.UUID | None = None,
    max_rounds: int = 2,
) -> uuid.UUID:
    group = await container.group_repo.create(name=name, max_rounds=max_rounds)
    for bot_id in members:
        await container.group_repo.add_member(
            group.id, bot_id, role="admin" if bot_id == admin else "member"
        )
    return group.id


async def _start_epoch(container: Container, group_id: uuid.UUID, content: str) -> uuid.UUID:
    await container.group_repo.add_messages(
        group_id, [GroupMessageDraft(role="user", content=content, author_name="我")]
    )
    epoch = await container.group_repo.create_epoch(group_id, trigger="user", max_rounds=2)
    return epoch.id


def test_parse_decision_treats_pass_as_silence() -> None:
    """`[PASS]` / SILENT / 空回复都算沉默，纯文本回应算发言。"""
    for token in ("[PASS]", "PASS", "SILENT", "[SILENT]", "无", "沉默", ""):
        assert _parse_decision(token).speak is False
    assert _parse_decision('{"speak": false}').speak is False
    assert _parse_decision('{"speak": true, "reply": "收到"}').reply == "收到"
    assert _parse_decision("今天的流量涨了 20%").reply == "今天的流量涨了 20%"


async def test_admin_wave_hands_off_by_mention(container: Container) -> None:
    """有群主时：没点名 → 群主先说；群主 @ 同伴 → 同伴接棒；没人再接就结束。"""
    admin = await _make_bot(container, "群主", model="claude-sonnet-5-5")
    helper = await _make_bot(container, "发布")
    group_id = await _make_group(container, "运营群", [admin, helper], admin=admin)
    epoch_id = await _start_epoch(container, group_id, "今天的数据怎么样？")

    router = _FakeRouter(
        {
            "群主": '{"speak": true, "reply": "@发布 你把今天的发布情况说一下"}',
            "发布": '{"speak": true, "reply": "今天发布了两条，流量涨了 20%"}',
        }
    )
    orchestrator = GroupOrchestrator(container.group_repo, container.task_repo, router)  # type: ignore[arg-type]
    await orchestrator.run_epoch(group_id, epoch_id)

    assert router.called == ["群主", "发布"]
    # per-bot 模型：群主的 model 覆盖透传给路由器
    assert router.kwargs[0]["model"] == "claude-sonnet-5-5"
    assert "model" not in router.kwargs[1]

    messages, _ = await container.group_repo.list_messages(group_id)
    assert [(m.author_name, m.role) for m in messages] == [
        ("我", "user"),
        ("群主", "bot"),
        ("发布", "bot"),
    ]

    epoch = await container.group_repo.get_epoch(epoch_id)
    assert epoch is not None and epoch.status == "done" and epoch.rounds == 2


async def test_pass_ends_wave_without_messages(container: Container) -> None:
    """群主选择 `[PASS]` 时这一波不算发言，epoch 立即收尾。"""
    admin = await _make_bot(container, "群主")
    group_id = await _make_group(container, "静默群", [admin], admin=admin)
    epoch_id = await _start_epoch(container, group_id, "随便聊聊")

    router = _FakeRouter({"群主": "[PASS]"})
    orchestrator = GroupOrchestrator(container.group_repo, container.task_repo, router)  # type: ignore[arg-type]
    await orchestrator.run_epoch(group_id, epoch_id)

    assert router.called == ["群主"]
    messages, _ = await container.group_repo.list_messages(group_id)
    assert [m.role for m in messages] == ["user"]

    epoch = await container.group_repo.get_epoch(epoch_id)
    assert epoch is not None and epoch.status == "done" and epoch.rounds == 1


async def test_member_without_admin_keeps_legacy_rounds(container: Container) -> None:
    """没有群主时保持旧行为：每轮给全员机会，全员沉默即结束。"""
    first = await _make_bot(container, "统计")
    second = await _make_bot(container, "校对")
    group_id = await _make_group(container, "无主群", [first, second])
    epoch_id = await _start_epoch(container, group_id, "大家看一下")

    router = _FakeRouter({"统计": "我统计完了", "校对": "[PASS]"})
    orchestrator = GroupOrchestrator(container.group_repo, container.task_repo, router)  # type: ignore[arg-type]
    await orchestrator.run_epoch(group_id, epoch_id)

    messages, _ = await container.group_repo.list_messages(group_id)
    assert [m.author_name for m in messages] == ["我", "统计"]
    # 第一轮有人说话 → 进入第二轮；第二轮统计重复被去重、校对仍沉默 → 结束
    assert router.called[:2] == ["统计", "校对"]

    epoch = await container.group_repo.get_epoch(epoch_id)
    assert epoch is not None and epoch.status == "done" and epoch.rounds == 2


async def test_epoch_end_compresses_group_and_team_memory(container: Container) -> None:
    """未压缩消息够长时，epoch 结束写成群记忆并沉淀一条团队记忆。"""
    admin = await _make_bot(container, "群主")
    group_id = await _make_group(container, "运营群", [admin], admin=admin, max_rounds=1)
    text = "今天的发布与流量数据：" + "x" * (MEMORY_TRIGGER_CHARS // 5)
    await container.group_repo.add_messages(
        group_id,
        [GroupMessageDraft(role="user", content=text, author_name="我") for _ in range(5)],
    )
    epoch = await container.group_repo.create_epoch(group_id, trigger="user", max_rounds=1)

    router = _FakeRouter({"群主": "[PASS]"})
    orchestrator = GroupOrchestrator(container.group_repo, container.task_repo, router)  # type: ignore[arg-type]
    await orchestrator.run_epoch(group_id, epoch.id)

    memory = await container.group_repo.get_memory(group_id, scope="group")
    assert memory is not None
    assert memory.content.strip()
    assert memory.up_to_seq == 5

    team = await container.group_repo.get_memory(None, scope="team")
    assert team is not None
    assert "运营群" in team.content


async def test_memory_skipped_below_threshold(container: Container) -> None:
    """消息太短时不做压缩，避免为几句话付一次模型调用。"""
    admin = await _make_bot(container, "群主")
    group_id = await _make_group(container, "短群", [admin], admin=admin, max_rounds=1)
    epoch_id = await _start_epoch(container, group_id, "只有一句话")

    router = _FakeRouter({"群主": "[PASS]"})
    orchestrator = GroupOrchestrator(container.group_repo, container.task_repo, router)  # type: ignore[arg-type]
    await orchestrator.run_epoch(group_id, epoch_id)

    assert await container.group_repo.get_memory(group_id, scope="group") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [('```json\n{"speak": false}\n```', False), ('{"speak": true}', False)],
)
def test_parse_decision_tolerates_code_fences(raw: str, expected: bool) -> None:
    assert _parse_decision(raw).speak is expected
