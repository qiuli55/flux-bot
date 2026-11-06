"""terminal_sessions / terminal_events 的读写入口（Agent Terminal Console §8）。

seq 的分配放在一次会话读改写里完成；并发安全由服务层的每会话锁保证（单进程装配，
与 Apply 的进程内锁同一取舍）。事件是只增不改的观察数据。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.enums import (
    TerminalEventKind,
    TerminalSessionKind,
    TerminalSessionStatus,
    TerminalSource,
)
from flux.errors import NotFoundError
from flux.models.terminal import TerminalEvent, TerminalSession


class TerminalRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_session(
        self,
        *,
        workspace_root: str,
        run_id: uuid.UUID | None = None,
        kind: TerminalSessionKind = TerminalSessionKind.AGENT,
    ) -> TerminalSession:
        session = TerminalSession(
            id=uuid.uuid4(),
            run_id=run_id,
            workspace_root=workspace_root,
            kind=kind.value,
            status=TerminalSessionStatus.ACTIVE.value,
        )
        async with self._session_factory() as db:
            db.add(session)
            await db.commit()
            return session

    async def get_session(self, session_id: str | uuid.UUID) -> TerminalSession:
        async with self._session_factory() as db:
            session = await db.get(TerminalSession, self._as_uuid(session_id))
            if session is None:
                raise NotFoundError(
                    f"终端会话 {session_id} 不存在", details={"session_id": str(session_id)}
                )
            return session

    async def list_sessions(
        self,
        *,
        limit: int = 20,
        kind: TerminalSessionKind | None = None,
        status: TerminalSessionStatus | None = None,
        run_id: uuid.UUID | None = None,
    ) -> list[TerminalSession]:
        statement = select(TerminalSession)
        if kind is not None:
            statement = statement.where(TerminalSession.kind == kind.value)
        if status is not None:
            statement = statement.where(TerminalSession.status == status.value)
        if run_id is not None:
            statement = statement.where(TerminalSession.run_id == run_id)
        statement = statement.order_by(
            TerminalSession.created_at.desc(), TerminalSession.id.desc()
        ).limit(limit)
        async with self._session_factory() as db:
            return list(await db.scalars(statement))

    async def list_sessions_with_cmd_pgid(self) -> list[TerminalSession]:
        """仍未结束、且记录着命令进程组 id 的会话——重启后据此回收孤儿命令进程。"""
        statement = select(TerminalSession).where(
            TerminalSession.cmd_pgid.is_not(None),
            TerminalSession.status == TerminalSessionStatus.ACTIVE.value,
        )
        async with self._session_factory() as db:
            return list(await db.scalars(statement))

    async def set_cmd_pgid(self, session_id: str | uuid.UUID, pgid: int | None) -> TerminalSession:
        """记录/清空会话正在执行的命令进程组 id（命令结束时传 None 清空）。"""
        key = self._as_uuid(session_id)
        async with self._session_factory() as db:
            session = await db.get(TerminalSession, key)
            if session is None:
                raise NotFoundError(
                    f"终端会话 {session_id} 不存在", details={"session_id": str(session_id)}
                )
            session.cmd_pgid = pgid
            await db.commit()
            return session

    async def append_event(
        self,
        session_id: str | uuid.UUID,
        *,
        kind: TerminalEventKind,
        source: TerminalSource,
        command: str | None = None,
        chunk: str | None = None,
        exit_code: int | None = None,
    ) -> TerminalEvent:
        """追加一条事件并分配 seq（读改写在同一事务里提交）。"""
        key = self._as_uuid(session_id)
        async with self._session_factory() as db:
            session = await db.get(TerminalSession, key)
            if session is None:
                raise NotFoundError(
                    f"终端会话 {session_id} 不存在", details={"session_id": str(session_id)}
                )
            event = TerminalEvent(
                id=uuid.uuid4(),
                session_id=key,
                seq=session.next_seq,
                kind=kind.value,
                source=source.value,
                command=command,
                chunk=chunk,
                exit_code=exit_code,
            )
            session.next_seq = session.next_seq + 1
            db.add(event)
            await db.commit()
            return event

    async def list_events(
        self, session_id: str | uuid.UUID, *, after_seq: int = 0, limit: int = 2000
    ) -> list[TerminalEvent]:
        statement = (
            select(TerminalEvent)
            .where(TerminalEvent.session_id == self._as_uuid(session_id))
            .where(TerminalEvent.seq > after_seq)
            .order_by(TerminalEvent.seq)
            .limit(limit)
        )
        async with self._session_factory() as db:
            return list(await db.scalars(statement))

    async def set_status(
        self, session_id: str | uuid.UUID, status: TerminalSessionStatus
    ) -> TerminalSession:
        key = self._as_uuid(session_id)
        async with self._session_factory() as db:
            session = await db.get(TerminalSession, key)
            if session is None:
                raise NotFoundError(
                    f"终端会话 {session_id} 不存在", details={"session_id": str(session_id)}
                )
            session.status = status.value
            if status is not TerminalSessionStatus.ACTIVE:
                session.finished_at = datetime.now(timezone.utc)
            await db.commit()
            return session

    @staticmethod
    def _as_uuid(value: str | uuid.UUID) -> uuid.UUID:
        if isinstance(value, uuid.UUID):
            return value
        try:
            return uuid.UUID(str(value))
        except ValueError as exc:
            raise NotFoundError(
                f"终端会话 {value} 不存在", details={"session_id": str(value)}
            ) from exc
