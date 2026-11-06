"""Agent 档案仓储（P3-16：Agent Registry 是身份的唯一来源）。

agents 表在 M0 建好了却没有任何读写代码——运行时档案只活在 AgentManager 的内存里。
这里补上持久化，让 Agent 的 canonical UUID 在重启后依然成立，令牌、Proposal attribution、
任务归属都指向同一个身份。

skills / tools / description / permissions 不是独立列，统一放 `config` JSON：
它们是档案的附属属性，查询永远按 id 或 name 走，不需要为此加列（M1 再按需拆表）。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.models.agent import Agent

#: agents 表里模型字段的占位值：Flux 不执行 Agent，模型属于 Agent 自己的运行时
_MODEL_PLACEHOLDER = "unspecified"


class AgentRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(
        self,
        *,
        agent_id: uuid.UUID,
        name: str,
        role: str,
        status: str,
        description: str = "",
        skills: tuple[str, ...] = (),
        tools: tuple[str, ...] = (),
        permissions: tuple[str, ...] = (),
        runtime: str = "dsh",
    ) -> Agent:
        """按 canonical id 落库（不存在则插入，存在则更新档案字段）。"""
        config: dict[str, Any] = {
            "description": description,
            "skills": list(skills),
            "tools": list(tools),
            "permissions": list(permissions),
            # P2-1：执行该档案的 runtime（dsh / codex / opencode），重启后据此分发
            "runtime": runtime,
        }
        async with self._session_factory() as session:
            agent = await session.get(Agent, agent_id)
            if agent is None:
                agent = Agent(
                    id=agent_id,
                    name=name,
                    role=role,
                    status=status,
                    model_provider=_MODEL_PLACEHOLDER,
                    model_name=_MODEL_PLACEHOLDER,
                    config=config,
                )
                session.add(agent)
            else:
                agent.name = name
                agent.role = role
                agent.status = status
                agent.config = config
            await session.commit()
        return agent

    async def get(self, agent_id: uuid.UUID) -> Agent | None:
        async with self._session_factory() as session:
            return await session.get(Agent, agent_id)

    async def get_by_name(self, name: str) -> Agent | None:
        statement = (
            select(Agent).where(Agent.name == name).order_by(Agent.created_at, Agent.id).limit(1)
        )
        async with self._session_factory() as session:
            return await session.scalar(statement)

    async def list(self) -> list[Agent]:
        statement = select(Agent).order_by(Agent.created_at, Agent.id)
        async with self._session_factory() as session:
            return list(await session.scalars(statement))


__all__ = ["AgentRepository"]
