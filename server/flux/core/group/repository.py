"""群聊表的读写入口。

与 TaskRepository 同一风格：一次调用一个会话、一个工作单元；群内 seq 与 epoch 序号
都在同一事务里分配，避免并发发言拿到重复序号。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.errors import BadRequestError, NotFoundError
from flux.logging import get_logger
from flux.models.bot import Bot
from flux.models.group import (
    ChatGroup,
    ChatGroupEpoch,
    ChatGroupMember,
    ChatGroupMemberState,
    ChatGroupMemory,
    ChatGroupMessage,
)

logger = get_logger(__name__)

#: 群成员可选角色。admin = 群主（用户没点名别人时默认由它先回应）。
MEMBER_ROLES = frozenset({"admin", "member"})

#: 单个成员笔记的字符上限（只留最近的部分，避免无限膨胀）。
MEMBER_MEMORY_MAX_CHARS = 4000


def _tail(text: str, limit: int) -> str:
    """保留文本末尾 limit 个字符（按行起点截断，避免半句）。"""
    if len(text) <= limit:
        return text
    clipped = text[-limit:]
    newline = clipped.find("\n")
    return clipped[newline + 1 :] if 0 <= newline < 200 else clipped


@dataclass(frozen=True)
class GroupMessageDraft:
    """待写入的一条群消息（seq 由仓储在同一事务里分配）。"""

    role: str
    content: str
    author_name: str = ""
    bot_id: uuid.UUID | None = None
    kind: str = "text"
    payload: dict[str, Any] | None = None
    epoch_id: uuid.UUID | None = None


class GroupRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # --- 群 ---

    async def create(
        self,
        *,
        name: str,
        description: str | None = None,
        project_id: uuid.UUID | None = None,
        max_rounds: int = 2,
    ) -> ChatGroup:
        async with self._session_factory() as session:
            group = ChatGroup(
                id=uuid.uuid4(),
                name=name.strip(),
                description=(description or "").strip() or None,
                project_id=project_id,
                max_rounds=max_rounds,
                status="active",
            )
            session.add(group)
            await self._commit(session)
            await session.refresh(group)
            return group

    async def get(self, group_id: uuid.UUID) -> ChatGroup:
        async with self._session_factory() as session:
            group = await session.get(ChatGroup, group_id)
            if group is None:
                raise NotFoundError(f"群聊 {group_id} 不存在", details={"group_id": str(group_id)})
            return group

    async def list(self, *, limit: int = 50) -> list[ChatGroup]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(ChatGroup)
                .order_by(
                    func.coalesce(ChatGroup.last_message_at, ChatGroup.created_at).desc(),
                    ChatGroup.id.desc(),
                )
                .limit(limit)
            )
            return list(rows)

    async def update(
        self,
        group_id: uuid.UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        max_rounds: int | None = None,
        status: str | None = None,
    ) -> ChatGroup:
        async with self._session_factory() as session:
            group = await self._require(session, group_id)
            if name is not None:
                group.name = name.strip()
            if description is not None:
                group.description = description.strip() or None
            if max_rounds is not None:
                group.max_rounds = max_rounds
            if status is not None:
                group.status = status
            await self._commit(session)
            return group

    async def delete(self, group_id: uuid.UUID) -> None:
        async with self._session_factory() as session:
            group = await self._require(session, group_id)
            await session.delete(group)
            await self._commit(session)

    # --- 成员 ---

    async def add_member(
        self, group_id: uuid.UUID, bot_id: uuid.UUID, *, role: str = "member"
    ) -> ChatGroupMember:
        if role not in MEMBER_ROLES:
            raise BadRequestError(
                f"成员角色非法：{role}", details={"role": role, "allowed": sorted(MEMBER_ROLES)}
            )
        async with self._session_factory() as session:
            await self._require(session, group_id)
            if await session.get(Bot, bot_id) is None:
                raise NotFoundError(f"Bot {bot_id} 不存在", details={"bot_id": str(bot_id)})
            if role == "admin":
                await self._clear_admins(session, group_id)
            existing = await session.scalar(
                select(ChatGroupMember).where(
                    ChatGroupMember.group_id == group_id,
                    ChatGroupMember.bot_id == bot_id,
                )
            )
            if existing is not None:
                existing.role = role
                await self._commit(session)
                await session.refresh(existing)
                return existing
            member = ChatGroupMember(id=uuid.uuid4(), group_id=group_id, bot_id=bot_id, role=role)
            session.add(member)
            await self._commit(session)
            await session.refresh(member)
            return member

    async def set_member_role(
        self, group_id: uuid.UUID, bot_id: uuid.UUID, role: str
    ) -> ChatGroupMember:
        """设置成员角色；一个群最多一个 admin（设新 admin 会撤销旧的）。"""
        if role not in MEMBER_ROLES:
            raise BadRequestError(
                f"成员角色非法：{role}", details={"role": role, "allowed": sorted(MEMBER_ROLES)}
            )
        async with self._session_factory() as session:
            await self._require(session, group_id)
            member = await session.scalar(
                select(ChatGroupMember).where(
                    ChatGroupMember.group_id == group_id,
                    ChatGroupMember.bot_id == bot_id,
                )
            )
            if member is None:
                raise NotFoundError(
                    f"群 {group_id} 里没有成员 {bot_id}",
                    details={"group_id": str(group_id), "bot_id": str(bot_id)},
                )
            if role == "admin":
                await self._clear_admins(session, group_id)
            member.role = role
            await self._commit(session)
            await session.refresh(member)
            return member

    async def _clear_admins(self, session: AsyncSession, group_id: uuid.UUID) -> None:
        admins = await session.scalars(
            select(ChatGroupMember).where(
                ChatGroupMember.group_id == group_id,
                ChatGroupMember.role == "admin",
            )
        )
        for member in admins:
            member.role = "member"

    async def remove_member(self, group_id: uuid.UUID, bot_id: uuid.UUID) -> bool:
        async with self._session_factory() as session:
            await self._require(session, group_id)
            result = await session.execute(
                delete(ChatGroupMember).where(
                    ChatGroupMember.group_id == group_id,
                    ChatGroupMember.bot_id == bot_id,
                )
            )
            await self._commit(session)
            return bool(result.rowcount)

    async def list_members(self, group_id: uuid.UUID) -> list[tuple[ChatGroupMember, Bot]]:
        """成员 + 对应的 Bot（按入群顺序）。"""
        async with self._session_factory() as session:
            await self._require(session, group_id)
            rows = await session.execute(
                select(ChatGroupMember, Bot)
                .join(Bot, Bot.id == ChatGroupMember.bot_id)
                .where(ChatGroupMember.group_id == group_id)
                .order_by(ChatGroupMember.created_at.asc(), ChatGroupMember.id.asc())
            )
            return [(member, bot) for member, bot in rows.all()]

    async def member_count_map(self, group_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not group_ids:
            return {}
        async with self._session_factory() as session:
            rows = await session.execute(
                select(ChatGroupMember.group_id, func.count(ChatGroupMember.id))
                .where(ChatGroupMember.group_id.in_(tuple(group_ids)))
                .group_by(ChatGroupMember.group_id)
            )
            return {group_id: int(count) for group_id, count in rows.all()}

    async def list_members_map(
        self, group_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[tuple[ChatGroupMember, Bot]]]:
        """批量取多个群的成员（按入群顺序），供列表页一次取齐头像。"""
        if not group_ids:
            return {}
        async with self._session_factory() as session:
            rows = await session.execute(
                select(ChatGroupMember, Bot)
                .join(Bot, Bot.id == ChatGroupMember.bot_id)
                .where(ChatGroupMember.group_id.in_(tuple(group_ids)))
                .order_by(ChatGroupMember.created_at.asc(), ChatGroupMember.id.asc())
            )
            result: dict[uuid.UUID, list[tuple[ChatGroupMember, Bot]]] = {
                group_id: [] for group_id in group_ids
            }
            for member, bot in rows.all():
                result.setdefault(member.group_id, []).append((member, bot))
            return result

    # --- 成员状态（每成员 × 每群） ---

    async def get_member_state(
        self, group_id: uuid.UUID, bot_id: uuid.UUID
    ) -> ChatGroupMemberState | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(ChatGroupMemberState).where(
                    ChatGroupMemberState.group_id == group_id,
                    ChatGroupMemberState.bot_id == bot_id,
                )
            )

    async def record_member_turn(
        self,
        group_id: uuid.UUID,
        bot_id: uuid.UUID,
        *,
        seq: int,
        note: str = "",
    ) -> ChatGroupMemberState:
        """记一次发言：累加 turns、推进 last_seen_seq、追加个人笔记。"""
        async with self._session_factory() as session:
            state = await session.scalar(
                select(ChatGroupMemberState).where(
                    ChatGroupMemberState.group_id == group_id,
                    ChatGroupMemberState.bot_id == bot_id,
                )
            )
            if state is None:
                state = ChatGroupMemberState(
                    id=uuid.uuid4(),
                    group_id=group_id,
                    bot_id=bot_id,
                    last_seen_seq=0,
                    turns=0,
                )
                session.add(state)
            state.turns += 1
            state.last_seen_seq = max(state.last_seen_seq, seq)
            note = note.strip()
            if note:
                merged = f"{state.memory}\n{note}".strip() if state.memory else note
                state.memory = _tail(merged, MEMBER_MEMORY_MAX_CHARS)
            await self._commit(session)
            await session.refresh(state)
            return state

    # --- 分层记忆（群共享 / 团队共享） ---

    async def get_memory(
        self, group_id: uuid.UUID | None, *, scope: str = "group"
    ) -> ChatGroupMemory | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(ChatGroupMemory).where(
                    ChatGroupMemory.scope == scope,
                    ChatGroupMemory.group_id.is_(None)
                    if group_id is None
                    else ChatGroupMemory.group_id == group_id,
                )
            )

    async def upsert_memory(
        self,
        group_id: uuid.UUID | None,
        *,
        scope: str = "group",
        content: str,
        up_to_seq: int = 0,
    ) -> ChatGroupMemory:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ChatGroupMemory).where(
                    ChatGroupMemory.scope == scope,
                    ChatGroupMemory.group_id.is_(None)
                    if group_id is None
                    else ChatGroupMemory.group_id == group_id,
                )
            )
            if row is None:
                row = ChatGroupMemory(
                    id=uuid.uuid4(), group_id=group_id, scope=scope, content="", up_to_seq=0
                )
                session.add(row)
            row.content = content.strip()
            row.up_to_seq = max(row.up_to_seq, up_to_seq)
            await self._commit(session)
            await session.refresh(row)
            return row

    # --- 消息 ---

    async def add_messages(
        self, group_id: uuid.UUID, drafts: Sequence[GroupMessageDraft]
    ) -> list[ChatGroupMessage]:
        """整批写入，返回带 seq 与 created_at 的消息。"""
        if not drafts:
            return []
        async with self._session_factory() as session:
            group = await self._require(session, group_id)
            last_seq = await session.scalar(
                select(func.max(ChatGroupMessage.seq)).where(ChatGroupMessage.group_id == group_id)
            )
            seq = int(last_seq or 0)
            created: list[ChatGroupMessage] = []
            for draft in drafts:
                seq += 1
                message = ChatGroupMessage(
                    id=uuid.uuid4(),
                    group_id=group_id,
                    seq=seq,
                    bot_id=draft.bot_id,
                    author_name=draft.author_name[:64],
                    role=draft.role,
                    kind=draft.kind,
                    content=draft.content,
                    payload=draft.payload,
                    epoch_id=draft.epoch_id,
                )
                session.add(message)
                created.append(message)
            group.last_message_at = datetime.now(timezone.utc)
            await self._commit(session)
            for message in created:
                await session.refresh(message)
            return created

    async def list_messages(
        self, group_id: uuid.UUID, *, after_seq: int = 0, limit: int = 100
    ) -> tuple[list[ChatGroupMessage], bool]:
        """按 seq 升序取 after_seq 之后的消息，以及是否还有更多。"""
        async with self._session_factory() as session:
            await self._require(session, group_id)
            statement = select(ChatGroupMessage).where(
                ChatGroupMessage.group_id == group_id,
                ChatGroupMessage.seq > after_seq,
            )
            rows = list(
                await session.scalars(
                    statement.order_by(ChatGroupMessage.seq.asc()).limit(limit + 1)
                )
            )
        has_more = len(rows) > limit
        return rows[:limit], has_more

    async def recent_messages(
        self, group_id: uuid.UUID, *, limit: int = 40
    ) -> list[ChatGroupMessage]:
        """取最近 limit 条群消息（按 seq 升序），供编排器拼装上下文。"""
        async with self._session_factory() as session:
            await self._require(session, group_id)
            rows = list(
                await session.scalars(
                    select(ChatGroupMessage)
                    .where(ChatGroupMessage.group_id == group_id)
                    .order_by(ChatGroupMessage.seq.desc())
                    .limit(limit)
                )
            )
        rows.reverse()
        return rows

    async def messages_for_epoch(
        self, group_id: uuid.UUID, epoch_id: uuid.UUID
    ) -> list[ChatGroupMessage]:
        """取某个 epoch 里落库的全部消息（按 seq 升序），供 Bot 记忆提炼。"""
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(ChatGroupMessage)
                .where(
                    ChatGroupMessage.group_id == group_id,
                    ChatGroupMessage.epoch_id == epoch_id,
                )
                .order_by(ChatGroupMessage.seq.asc())
            )
            return list(rows)

    async def latest_seq(self, group_id: uuid.UUID) -> int:
        async with self._session_factory() as session:
            await self._require(session, group_id)
            value = await session.scalar(
                select(func.max(ChatGroupMessage.seq)).where(ChatGroupMessage.group_id == group_id)
            )
            return int(value or 0)

    # --- epoch ---

    async def create_epoch(
        self, group_id: uuid.UUID, *, trigger: str, max_rounds: int
    ) -> ChatGroupEpoch:
        async with self._session_factory() as session:
            await self._require(session, group_id)
            last_index = await session.scalar(
                select(func.max(ChatGroupEpoch.epoch_index)).where(
                    ChatGroupEpoch.group_id == group_id
                )
            )
            epoch = ChatGroupEpoch(
                id=uuid.uuid4(),
                group_id=group_id,
                epoch_index=int(last_index or 0) + 1,
                trigger_kind=trigger,
                status="running",
                rounds=0,
                max_rounds=max_rounds,
            )
            session.add(epoch)
            await self._commit(session)
            await session.refresh(epoch)
            return epoch

    async def get_epoch(self, epoch_id: uuid.UUID) -> ChatGroupEpoch | None:
        async with self._session_factory() as session:
            return await session.get(ChatGroupEpoch, epoch_id)

    async def active_epoch(self, group_id: uuid.UUID) -> ChatGroupEpoch | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(ChatGroupEpoch)
                .where(
                    ChatGroupEpoch.group_id == group_id,
                    ChatGroupEpoch.status == "running",
                )
                .order_by(ChatGroupEpoch.epoch_index.desc())
                .limit(1)
            )

    async def set_epoch_status(
        self,
        epoch_id: uuid.UUID,
        status: str,
        *,
        rounds: int | None = None,
        finished: bool = False,
    ) -> ChatGroupEpoch | None:
        async with self._session_factory() as session:
            epoch = await session.get(ChatGroupEpoch, epoch_id)
            if epoch is None:
                return None
            epoch.status = status
            if rounds is not None:
                epoch.rounds = rounds
            if finished:
                epoch.finished_at = datetime.now(timezone.utc)
            await self._commit(session)
            return epoch

    async def interrupt_active_epoch(self, group_id: uuid.UUID) -> ChatGroupEpoch | None:
        """把正在跑的 epoch 置 interrupted；没有则返回 None。"""
        async with self._session_factory() as session:
            epoch = await session.scalar(
                select(ChatGroupEpoch)
                .where(
                    ChatGroupEpoch.group_id == group_id,
                    ChatGroupEpoch.status == "running",
                )
                .order_by(ChatGroupEpoch.epoch_index.desc())
                .limit(1)
            )
            if epoch is None:
                return None
            epoch.status = "interrupted"
            epoch.finished_at = datetime.now(timezone.utc)
            await self._commit(session)
            return epoch

    # --- 内部 ---

    async def _require(self, session: AsyncSession, group_id: uuid.UUID) -> ChatGroup:
        group = await session.get(ChatGroup, group_id)
        if group is None:
            raise NotFoundError(f"群聊 {group_id} 不存在", details={"group_id": str(group_id)})
        return group

    async def _commit(self, session: AsyncSession) -> None:
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            logger.warning("群聊写入违反数据库约束：%s", exc.orig)
            raise BadRequestError(f"群聊写入违反数据库约束：{exc.orig}") from exc
