"""wechat_sessions 的读写入口。

只做三件事：按 `(source_key, external_id)` 查会话、首次消息时绑定新建任务、
以及入站消息的幂等认领 / 归还。消息正文不进本模块——它们落在 `task_messages`。
"""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.models.wechat import WechatSession

__all__ = ["WechatSessionRepository"]


class WechatSessionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, source_key: str, external_id: str) -> WechatSession | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(WechatSession).where(
                    WechatSession.source_key == source_key,
                    WechatSession.external_id == external_id,
                )
            )

    async def bind(
        self,
        *,
        source_key: str,
        external_id: str,
        sender_id: str,
        task_id: uuid.UUID,
    ) -> WechatSession:
        """绑定会话 → 任务；并发首次消息时返回先落库的那一行（唯一约束兜底）。"""
        async with self._session_factory() as session:
            row = WechatSession(
                source_key=source_key,
                external_id=external_id,
                sender_id=sender_id,
                task_id=task_id,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(WechatSession).where(
                        WechatSession.source_key == source_key,
                        WechatSession.external_id == external_id,
                    )
                )
                if existing is None:
                    raise
                return existing
            await session.refresh(row)
            return row

    async def claim_message(self, session_id: uuid.UUID, message_id: str) -> bool:
        """认领一条入站消息：只有消息 id 与 last_message_id 不同才成功。

        OpenClaw 重试同一条消息时返回 False，调用方直接回放上次回复，
        不重复调用模型。认领是原子的，并发重试只有一个能成功。
        """
        async with self._session_factory() as session:
            result = await session.execute(
                update(WechatSession)
                .where(
                    WechatSession.id == session_id,
                    or_(
                        WechatSession.last_message_id.is_(None),
                        WechatSession.last_message_id != message_id,
                    ),
                )
                .values(last_message_id=message_id)
            )
            await session.commit()
            return bool(result.rowcount and result.rowcount > 0)

    async def release_message(self, session_id: uuid.UUID, message_id: str) -> None:
        """处理失败时归还认领，让同一条消息可以被重试。

        只在 last_message_id 仍是这条消息时归还，避免覆盖后续消息的认领。
        """
        async with self._session_factory() as session:
            await session.execute(
                update(WechatSession)
                .where(
                    WechatSession.id == session_id,
                    WechatSession.last_message_id == message_id,
                )
                .values(last_message_id=None)
            )
            await session.commit()

    async def record_messages(self, session_id: uuid.UUID, added: int) -> None:
        """累计已处理的对话条数（user + assistant 各算一条）。"""
        if added <= 0:
            return
        async with self._session_factory() as session:
            await session.execute(
                update(WechatSession)
                .where(WechatSession.id == session_id)
                .values(message_count=WechatSession.message_count + added)
            )
            await session.commit()
