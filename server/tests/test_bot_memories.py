"""Bot 长期记忆测试（2026-11-05）。

口径：
- 记忆属于 Bot（第一人称），按 direct / indirect 分层，无关内容不落库；
- 摘要层合并覆盖（version + 1），全文层只追加（按来源消息去重）；
- 同群 indirect 全权重注入，出群后的 indirect 只有关键词命中才低权重召回；
- 独立开工时记忆只进执行指令，不改写任务描述。

不启动真实 Run：本文件只验证存储、召回与指令拼装契约。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from flux.container import Container
from flux.core.agent_runtime.runtimes.service import _bot_memory_instruction
from flux.core.group.repository import GroupMessageDraft

PREFIX = "/api/v1"


def _create(client: TestClient, **overrides: object) -> dict:
    payload: dict[str, object] = {"name": "小方", "description": "负责巡检"}
    payload.update(overrides)
    response = client.post(f"{PREFIX}/bots", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _remember(client: TestClient, bot_id: str, content: str) -> dict:
    response = client.post(f"{PREFIX}/bots/{bot_id}/memories", json={"content": content})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_manual_memory_crud(client: TestClient) -> None:
    """手工记忆：direct、独立一条、全文可检索、可删除。"""
    bot = _create(client)
    memory = _remember(client, bot["id"], "我习惯用中文汇报")
    assert memory["relevance"] == "direct"
    assert memory["source"] == "manual"

    rows = client.get(f"{PREFIX}/bots/{bot['id']}/memories").json()["data"]
    assert [row["id"] for row in rows] == [memory["id"]]

    detail = client.get(f"{PREFIX}/bots/{bot['id']}/memories/{memory['id']}").json()["data"]
    assert [chunk["content"] for chunk in detail["chunks"]] == ["我习惯用中文汇报"]

    hits = client.get(
        f"{PREFIX}/bots/{bot['id']}/memories/search", params={"q": "中文汇报"}
    ).json()["data"]
    assert [hit["memory_id"] for hit in hits] == [memory["id"]]

    assert client.delete(f"{PREFIX}/bots/{bot['id']}/memories/{memory['id']}").status_code == 200
    assert client.get(f"{PREFIX}/bots/{bot['id']}/memories").json()["data"] == []


def test_memory_belongs_to_one_bot(client: TestClient) -> None:
    """记忆按 Bot 隔离：A 的记忆不能用 B 的路径读到或删掉。"""
    first = _create(client, name="甲")
    second = _create(client, name="乙")
    memory = _remember(client, first["id"], "甲的私事")

    for method in ("get", "delete"):
        response = getattr(client, method)(f"{PREFIX}/bots/{second['id']}/memories/{memory['id']}")
        assert response.status_code == 404, response.text


def test_memory_request_validation(client: TestClient) -> None:
    bot = _create(client)
    empty = client.post(f"{PREFIX}/bots/{bot['id']}/memories", json={"content": ""})
    assert empty.status_code == 422, empty.text
    bad = client.get(f"{PREFIX}/bots/{bot['id']}/memories", params={"relevance": "bogus"})
    assert bad.status_code == 400, bad.text
    blank = client.get(f"{PREFIX}/bots/{bot['id']}/memories/search", params={"q": "  "})
    assert blank.status_code == 400, blank.text


def test_epoch_split_merge_and_low_weight_recall(container: Container) -> None:
    async def _scenario() -> None:
        bot = await container.bots.create(name="小方", description="巡检")
        other = await container.bots.create(name="小北", description="记录")
        group = await container.group_repo.create(name="运维群")
        first = await container.group_repo.add_messages(
            group.id,
            [
                GroupMessageDraft(role="user", author_name="老板", content="以后每天九点检查磁盘"),
                GroupMessageDraft(
                    role="bot",
                    bot_id=bot.id,
                    author_name="小方",
                    content="收到，我会每天九点检查磁盘",
                ),
                GroupMessageDraft(
                    role="bot", bot_id=other.id, author_name="小北", content="我只负责写周报"
                ),
                GroupMessageDraft(role="user", author_name="老板", content="@小北 周报今天发"),
            ],
        )
        members = [(None, bot), (None, other)]
        assert await container.bot_memories.remember_epoch(group, members, first) > 0

        rows = await container.bot_memories.list_memories(bot.id)
        direct = [row for row in rows if row.relevance == "direct"]
        indirect = [row for row in rows if row.relevance == "indirect"]
        assert len(direct) == 1 and len(indirect) == 1
        assert "每天九点检查磁盘" in direct[0].content
        assert "每天九点检查磁盘" in indirect[0].content
        assert "周报今天发" in indirect[0].content
        # 别的 Bot 自己的发言不算我的记忆
        assert "我只负责写周报" not in indirect[0].content

        # 同群：direct 与 indirect 都按正常权重注入
        block = await container.bot_memories.render_block(bot, group_id=group.id, query="磁盘")
        assert "与你直接相关" in block
        assert "本群中与你有关" in block

        # 出群：indirect 不再默认注入，只在关键词命中时低权重召回
        silent = await container.bot_memories.render_block(bot, group_id=None, query="")
        assert "低权重召回" not in silent
        recalled = await container.bot_memories.render_block(bot, group_id=None, query="磁盘")
        assert "低权重召回 · 运维群" in recalled
        assert "与你直接相关" in recalled

        # 第二轮：摘要合并到同一条，version + 1；全文继续追加
        second = await container.group_repo.add_messages(
            group.id,
            [GroupMessageDraft(role="user", author_name="老板", content="磁盘告警先看日志")],
        )
        await container.bot_memories.remember_epoch(group, [(None, bot)], second)
        merged = await container.bot_memories.list_memories(bot.id, relevance="indirect")
        assert len(merged) == 1
        assert merged[0].version == 2
        assert merged[0].chunk_count == 3
        detail = await container.bot_memories.detail(merged[0].id)
        assert {chunk["content"] for chunk in detail["chunks"]} == {
            "以后每天九点检查磁盘",
            "@小北 周报今天发",
            "磁盘告警先看日志",
        }

        # 按来源群清理：只删这个群沉淀的记忆
        assert await container.bot_memories.delete_by_group(bot.id, group.id) == 2
        assert await container.bot_memories.list_memories(bot.id) == []

    asyncio.run(_scenario())


def test_standalone_task_instruction_carries_memory(container: Container) -> None:
    """独立开工（group_id=None）也能注入 Bot 自己的记忆，且只进执行指令。"""

    async def _scenario() -> None:
        bot = await container.bots.create(name="小方", description="巡检")
        await container.bot_memories.add_manual(bot.id, content="我负责磁盘巡检")
        task = SimpleNamespace(bot_id=bot.id, id="task-1", description="磁盘巡检")
        text = await _bot_memory_instruction(container, task)
        assert "我负责磁盘巡检" in text
        # direct 记忆无条件注入，与任务描述无关也记得住
        unrelated = SimpleNamespace(bot_id=bot.id, id="task-2", description="整理周报")
        assert await _bot_memory_instruction(container, unrelated) != ""
        # 非 Bot 任务不注入
        assert await _bot_memory_instruction(container, SimpleNamespace(bot_id=None)) == ""

    asyncio.run(_scenario())
