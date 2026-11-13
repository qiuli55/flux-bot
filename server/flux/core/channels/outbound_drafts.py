"""出站草稿的持久化服务（频道无关）。

`create` 只落一条 pending 草稿，绝不发送；`claim` 用一条带条件的 UPDATE 做原子
认领（pending → sending），保证同一条草稿即使收到多次「允许」也只发送一次；
发送结果再由 `mark_sent` / `mark_failed` 落终态。失败时保留正文，方便用户之后
在草稿箱里查看或重新发起，而不是像进程内表那样直接丢失。

日志只记录 id / channel / status / body_hash / 目标键 / 字符数，不打印正文。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.enums import OutboundDraftStatus
from flux.errors import BadRequestError, ConflictError, NotFoundError
from flux.logging import get_logger
from flux.models.outbound_draft import OutboundDraft

logger = get_logger(__name__)

__all__ = ["OutboundDraftService"]

#: 落库的失败原因上限：只放异常类型/短消息，绝不放正文或令牌
_MAX_ERROR_CHARS = 200
#: 终态集合：这些状态不会再被发送
_TERMINAL = frozenset(
    {
        OutboundDraftStatus.SENT.value,
        OutboundDraftStatus.DENIED.value,
        OutboundDraftStatus.CANCELLED.value,
        OutboundDraftStatus.FAILED.value,
    }
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash_body(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _error_text(error: object) -> str:
    """把异常折叠成短错误串：去掉换行，截断，避免把整段响应写进库。"""
    text = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
    flattened = " ".join(text.split())
    return flattened[:_MAX_ERROR_CHARS]


class OutboundDraftService:
    """`outbound_drafts` 的创建、查询与状态迁移入口。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        channel: str,
        body: str,
        dest: dict[str, Any] | None = None,
        bot_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        turn_id: uuid.UUID | None = None,
        decision_id: str | None = None,
    ) -> OutboundDraft:
        """新建 pending 草稿；这里只落库，绝不触发发送。"""
        normalized_channel = (channel or "").strip()
        if not normalized_channel:
            raise BadRequestError("channel 不能为空", details={"field": "channel"})
        now = _utcnow()
        row = OutboundDraft(
            id=uuid.uuid4(),
            channel=normalized_channel,
            status=OutboundDraftStatus.PENDING.value,
            body=body,
            body_hash=_hash_body(body),
            dest=dict(dest or {}),
            bot_id=bot_id,
            task_id=task_id,
            turn_id=turn_id,
            decision_id=decision_id,
            error=None,
            sent_at=None,
            resolved_at=None,
            created_at=now,
            updated_at=now,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        logger.info(
            "outbound_draft.created id=%s channel=%s status=%s hash=%s dest_keys=%s chars=%s",
            row.id,
            row.channel,
            row.status,
            row.body_hash,
            sorted(row.dest or {}),
            len(body or ""),
        )
        return row

    async def get(self, draft_id: uuid.UUID) -> OutboundDraft:
        async with self._session_factory() as session:
            row = await session.get(OutboundDraft, draft_id)
            if row is None:
                raise NotFoundError(
                    f"出站草稿 {draft_id} 不存在", details={"draft_id": str(draft_id)}
                )
            return row

    async def find(self, draft_id: uuid.UUID) -> OutboundDraft | None:
        async with self._session_factory() as session:
            return await session.get(OutboundDraft, draft_id)

    async def list(
        self,
        *,
        bot_id: uuid.UUID | None = None,
        channel: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[OutboundDraft]:
        """按 Bot / 频道 / 状态过滤，最新在前。"""
        statement = select(OutboundDraft)
        if bot_id is not None:
            statement = statement.where(OutboundDraft.bot_id == bot_id)
        if channel:
            statement = statement.where(OutboundDraft.channel == channel)
        if status:
            statement = statement.where(OutboundDraft.status == status)
        statement = statement.order_by(OutboundDraft.created_at.desc()).limit(limit)
        async with self._session_factory() as session:
            rows = await session.scalars(statement)
            return list(rows)

    async def attach_decision(self, draft_id: uuid.UUID, decision_id: str | None) -> bool:
        """回填审批记录 id；审批卡与草稿因此可以互相定位。"""
        if not decision_id:
            return False
        async with self._session_factory() as session:
            result = await session.execute(
                update(OutboundDraft)
                .where(OutboundDraft.id == draft_id)
                .values(decision_id=decision_id, updated_at=_utcnow())
            )
            await session.commit()
            return result.rowcount == 1

    async def next_queued(self, *, channel: str, task_id: uuid.UUID) -> OutboundDraft | None:
        """同一任务/频道里最早排队、尚未挂审批卡的 pending 草稿。

        `decision_id IS NULL` 是「已落库但还没展示审批卡」的排队标记；卡片一旦
        挂出就会回填 decision_id，因此这里不会把当前正在显示的草稿再取一次。
        """
        statement = (
            select(OutboundDraft)
            .where(
                OutboundDraft.channel == channel,
                OutboundDraft.task_id == task_id,
                OutboundDraft.status == OutboundDraftStatus.PENDING.value,
                OutboundDraft.decision_id.is_(None),
            )
            .order_by(OutboundDraft.created_at.asc(), OutboundDraft.id.asc())
            .limit(1)
        )
        async with self._session_factory() as session:
            return await session.scalar(statement)

    async def claim_for_approval(
        self, draft_id: uuid.UUID, decision_id: str
    ) -> OutboundDraft | None:
        """原子地把排队草稿（decision_id 为空）绑定到一张即将挂出的审批卡。

        多个事件处理器同时想提升同一条草稿时，只有第一个能把 decision_id 从
        NULL 写进去，其余拿到 None 并继续看下一条，保证一张卡只挂一次。
        """
        if not decision_id:
            return None
        now = _utcnow()
        async with self._session_factory() as session:
            result = await session.execute(
                update(OutboundDraft)
                .where(
                    OutboundDraft.id == draft_id,
                    OutboundDraft.status == OutboundDraftStatus.PENDING.value,
                    OutboundDraft.decision_id.is_(None),
                )
                .values(decision_id=decision_id, updated_at=now)
            )
            if result.rowcount != 1:
                await session.rollback()
                return None
            row = await session.get(OutboundDraft, draft_id)
            await session.commit()
            if row is None:  # pragma: no cover - 同一事务内被删除的极端情况
                return None
            logger.info(
                "outbound_draft.approval_claimed id=%s channel=%s decision=%s",
                row.id,
                row.channel,
                decision_id,
            )
            return row

    async def release_approval(self, draft_id: uuid.UUID, decision_id: str) -> bool:
        """挂卡失败时把草稿退回队列；只撤自己刚写入的那次绑定。"""
        if not decision_id:
            return False
        async with self._session_factory() as session:
            result = await session.execute(
                update(OutboundDraft)
                .where(
                    OutboundDraft.id == draft_id,
                    OutboundDraft.status == OutboundDraftStatus.PENDING.value,
                    OutboundDraft.decision_id == decision_id,
                )
                .values(decision_id=None, updated_at=_utcnow())
            )
            await session.commit()
            return result.rowcount == 1

    async def claim(self, draft_id: uuid.UUID) -> OutboundDraft | None:
        """原子认领 pending → sending；已被认领/已是终态时返回 None。"""
        now = _utcnow()
        async with self._session_factory() as session:
            result = await session.execute(
                update(OutboundDraft)
                .where(
                    OutboundDraft.id == draft_id,
                    OutboundDraft.status == OutboundDraftStatus.PENDING.value,
                )
                .values(status=OutboundDraftStatus.SENDING.value, updated_at=now)
            )
            if result.rowcount != 1:
                await session.rollback()
                return None
            row = await session.get(OutboundDraft, draft_id)
            await session.commit()
            if row is None:  # pragma: no cover - 同一事务内被删除的极端情况
                return None
            logger.info(
                "outbound_draft.claimed id=%s channel=%s status=%s hash=%s chars=%s",
                row.id,
                row.channel,
                row.status,
                row.body_hash,
                len(row.body or ""),
            )
            return row

    async def mark_sent(self, draft_id: uuid.UUID) -> bool:
        """sending → sent；只对已认领的草稿生效。"""
        now = _utcnow()
        async with self._session_factory() as session:
            result = await session.execute(
                update(OutboundDraft)
                .where(
                    OutboundDraft.id == draft_id,
                    OutboundDraft.status == OutboundDraftStatus.SENDING.value,
                )
                .values(
                    status=OutboundDraftStatus.SENT.value,
                    sent_at=now,
                    resolved_at=now,
                    error=None,
                    updated_at=now,
                )
            )
            await session.commit()
        if result.rowcount == 1:
            logger.info("outbound_draft.sent id=%s", draft_id)
        return result.rowcount == 1

    async def mark_failed(self, draft_id: uuid.UUID, error: object) -> bool:
        """sending → failed：保留正文，不自动重试。"""
        now = _utcnow()
        safe_error = _error_text(error)
        async with self._session_factory() as session:
            result = await session.execute(
                update(OutboundDraft)
                .where(
                    OutboundDraft.id == draft_id,
                    OutboundDraft.status == OutboundDraftStatus.SENDING.value,
                )
                .values(
                    status=OutboundDraftStatus.FAILED.value,
                    error=safe_error,
                    resolved_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
        if result.rowcount == 1:
            logger.warning("outbound_draft.failed id=%s error=%s", draft_id, safe_error)
        return result.rowcount == 1

    async def mark_denied(self, draft_id: uuid.UUID) -> bool:
        """pending → denied；重复拒绝保持幂等。"""
        now = _utcnow()
        async with self._session_factory() as session:
            result = await session.execute(
                update(OutboundDraft)
                .where(
                    OutboundDraft.id == draft_id,
                    OutboundDraft.status == OutboundDraftStatus.PENDING.value,
                )
                .values(
                    status=OutboundDraftStatus.DENIED.value,
                    resolved_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
        if result.rowcount == 1:
            logger.info("outbound_draft.denied id=%s", draft_id)
        return result.rowcount == 1

    async def cancel(self, draft_id: uuid.UUID) -> OutboundDraft:
        """用户主动取消：只有 pending 可以取消，其余状态返回 409。"""
        now = _utcnow()
        async with self._session_factory() as session:
            result = await session.execute(
                update(OutboundDraft)
                .where(
                    OutboundDraft.id == draft_id,
                    OutboundDraft.status == OutboundDraftStatus.PENDING.value,
                )
                .values(
                    status=OutboundDraftStatus.CANCELLED.value,
                    resolved_at=now,
                    updated_at=now,
                )
            )
            if result.rowcount == 1:
                row = await session.get(OutboundDraft, draft_id)
                await session.commit()
                logger.info("outbound_draft.cancelled id=%s", draft_id)
                if row is None:  # pragma: no cover - 同一事务内被删除的极端情况
                    raise NotFoundError(
                        f"出站草稿 {draft_id} 不存在", details={"draft_id": str(draft_id)}
                    )
                return row
            await session.rollback()
            row = await session.get(OutboundDraft, draft_id)
            if row is None:
                raise NotFoundError(
                    f"出站草稿 {draft_id} 不存在", details={"draft_id": str(draft_id)}
                )
            raise ConflictError(
                f"草稿当前状态为 {row.status}，只有 pending 可以取消",
                details={"draft_id": str(draft_id), "status": row.status},
            )

    @staticmethod
    def is_terminal(status: str) -> bool:
        return status in _TERMINAL
