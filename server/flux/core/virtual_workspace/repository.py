"""virtual_changes 表的读写入口（主规格 §7.3 / §11.2；实施计划 §5）。

提案的权威存储在这里：进程重启后人工审核队列不能丢，Apply 前的 hash 复验也必须
读库里的 original_hash，而不是 Agent 手里的副本。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.enums import RecoveryResolution, VirtualChangeStatus
from flux.errors import BadRequestError, ConflictError, NotFoundError
from flux.logging import get_logger
from flux.models.project import Project
from flux.models.workspace import VirtualChange

logger = get_logger(__name__)


class ProposalRepository:
    """virtual_changes 表的读写入口。每次调用自开一个会话并提交。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        file_path: str,
        original_content: str,
        proposed_content: str | None,
        original_hash: str,
        diff: str,
        kind: str = "modify",
        added_lines: int,
        removed_lines: int,
        hunks: int,
        project_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        group_id: uuid.UUID | None = None,
        agent_source: str | None = None,
        reason: str | None = None,
        summary: str | None = None,
        status: VirtualChangeStatus = VirtualChangeStatus.PENDING,
        expires_at: datetime | None = None,
        change_id: uuid.UUID | None = None,
    ) -> VirtualChange:
        """写入一条提案。project_id 指向不存在的项目时抛 NotFoundError。

        与 TaskRepository 保持同一语义：合法 UUID 但关联实体不存在一律 404，
        不把问题留给数据库外键（那会退化成 400，同一类问题两种错误码）。
        """
        async with self._session_factory() as session:
            if project_id is not None:
                exists = await session.scalar(select(Project.id).where(Project.id == project_id))
                if exists is None:
                    raise NotFoundError(
                        f"项目 {project_id} 不存在", details={"project_id": str(project_id)}
                    )
            change = VirtualChange(
                id=change_id or uuid.uuid4(),
                project_id=project_id,
                task_id=task_id,
                group_id=group_id,
                file_path=file_path,
                kind=kind,
                original_hash=original_hash,
                original_content=original_content,
                proposed_content=proposed_content,
                diff=diff,
                added_lines=added_lines,
                removed_lines=removed_lines,
                hunks=hunks,
                reason=reason,
                summary=summary,
                agent_source=agent_source,
                status=status.value,
                expires_at=expires_at,
            )
            session.add(change)
            await self._commit(session)
            return change

    async def get(self, change_id: str | uuid.UUID) -> VirtualChange:
        key = self._as_uuid(change_id)
        async with self._session_factory() as session:
            change = await session.get(VirtualChange, key)
            if change is None:
                raise NotFoundError(
                    f"虚拟改动 {change_id} 不存在", details={"change_id": str(change_id)}
                )
            return change

    async def list(
        self,
        *,
        project_id: str | uuid.UUID | None = None,
        task_id: str | uuid.UUID | None = None,
        group_id: str | uuid.UUID | None = None,
        file_path: str | None = None,
        status: str | VirtualChangeStatus | None = None,
    ) -> list[VirtualChange]:
        statement = select(VirtualChange).order_by(VirtualChange.created_at, VirtualChange.id)
        if project_id is not None:
            statement = statement.where(VirtualChange.project_id == self._as_uuid(project_id))
        if task_id is not None:
            statement = statement.where(VirtualChange.task_id == self._as_uuid(task_id))
        if group_id is not None:
            statement = statement.where(VirtualChange.group_id == self._as_uuid(group_id))
        if file_path is not None:
            statement = statement.where(VirtualChange.file_path == file_path)
        if status is not None:
            statement = statement.where(VirtualChange.status == str(status))
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def list_expirable(self) -> list[VirtualChange]:
        """所有"设了截止时间且仍在可审状态"的提案，供服务层做惰性/批量过期。

        时间比较放在 Python 侧做：SQLite 不存时区，库里读出来的时间可能没有 tzinfo，
        在 SQL 里直接比较会因绑定参数带时区而结果不稳定（详见 service._as_utc）。
        """
        statement = (
            select(VirtualChange)
            .where(
                VirtualChange.status.in_(
                    [VirtualChangeStatus.PENDING.value, VirtualChangeStatus.ACCEPTED.value]
                ),
                VirtualChange.expires_at.is_not(None),
            )
            .order_by(VirtualChange.created_at, VirtualChange.id)
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def set_status(
        self, change_id: str | uuid.UUID, status: VirtualChangeStatus
    ) -> VirtualChange:
        key = self._as_uuid(change_id)
        async with self._session_factory() as session:
            change = await session.get(VirtualChange, key)
            if change is None:
                raise NotFoundError(
                    f"虚拟改动 {change_id} 不存在", details={"change_id": str(change_id)}
                )
            change.status = status.value
            await self._commit(session)
            return change

    async def set_expires_at(
        self, change_id: str | uuid.UUID, expires_at: datetime | None
    ) -> VirtualChange:
        """改写提案的审核截止时间（续期 / 取消 TTL / 测试预置过期）。"""
        key = self._as_uuid(change_id)
        async with self._session_factory() as session:
            change = await session.get(VirtualChange, key)
            if change is None:
                raise NotFoundError(
                    f"虚拟改动 {change_id} 不存在", details={"change_id": str(change_id)}
                )
            change.expires_at = expires_at
            await self._commit(session)
            return change

    async def mark_expired(
        self, change_ids: Sequence[uuid.UUID], *, reason: str
    ) -> list[VirtualChange]:
        """把一批提案统一置为 expired 终态，并记下失效原因。

        不在这里做状态合法性判断——那是服务层的职责（服务层已经按当前状态筛过一轮）；
        本方法只负责"这一批原子地写进终态"，任何一个 ID 不存在则整批回滚不落库。
        """
        if not change_ids:
            return []
        async with self._session_factory() as session:
            loaded: list[VirtualChange] = []
            for key in change_ids:
                change = await session.get(VirtualChange, key)
                if change is None:
                    raise NotFoundError(f"虚拟改动 {key} 不存在", details={"change_id": str(key)})
                loaded.append(change)
            for change in loaded:
                change.status = VirtualChangeStatus.EXPIRED.value
                change.expired_reason = reason
            await self._commit(session)
            return loaded

    async def set_apply_result(
        self,
        change_id: str | uuid.UUID,
        *,
        status: VirtualChangeStatus,
        backup_path: str | None = None,
        apply_error: str | None = None,
    ) -> VirtualChange:
        """记录一次 Apply 的结果（成功写 backup_path，失败写 apply_error）。

        与 set_status 分开是为了让审计字段与状态在同一个事务里落库：状态是 applied
        却没有备份路径，或状态是 failed 却没有原因，都属于不可审计的中间态。
        """
        key = self._as_uuid(change_id)
        async with self._session_factory() as session:
            change = await session.get(VirtualChange, key)
            if change is None:
                raise NotFoundError(
                    f"虚拟改动 {change_id} 不存在", details={"change_id": str(change_id)}
                )
            change.status = status.value
            change.backup_path = backup_path
            change.apply_error = apply_error
            await self._commit(session)
            return change

    async def set_recovery_resolution(
        self, change_id: str | uuid.UUID, resolution: RecoveryResolution
    ) -> VirtualChange:
        """写下崩溃恢复挂起项的人工决策结果（cover / keep）。一旦写下即为终态。"""
        key = self._as_uuid(change_id)
        async with self._session_factory() as session:
            change = await session.get(VirtualChange, key)
            if change is None:
                raise NotFoundError(
                    f"虚拟改动 {change_id} 不存在", details={"change_id": str(change_id)}
                )
            change.recovery_resolution = resolution.value
            await self._commit(session)
            return change

    async def claim_applying(self, change_id: str | uuid.UUID) -> VirtualChange:
        """原子地抢占一条 accepted 提案（P0-1）：只有当前状态确实是 accepted 才置 applying。

        两个并发 Apply 都读到 accepted 时，先写库的那个赢；后到的在这里被拒，
        不会出现"同一提案被两批同时落盘"。抢占失败的错误是冲突语义（409）。
        """
        key = self._as_uuid(change_id)
        async with self._session_factory() as session:
            change = await session.get(VirtualChange, key)
            if change is None:
                raise NotFoundError(
                    f"虚拟改动 {change_id} 不存在", details={"change_id": str(change_id)}
                )
            if change.status != VirtualChangeStatus.ACCEPTED.value:
                raise ConflictError(
                    f"提案 {change_id} 当前状态为 {change.status}，不能开始 Apply",
                    details={"change_id": str(change_id), "status": change.status},
                )
            change.status = VirtualChangeStatus.APPLYING.value
            await self._commit(session)
            return change

    async def _commit(self, session: AsyncSession) -> None:
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            logger.warning("提案写入违反数据库约束：%s", exc.orig)
            raise BadRequestError(f"提案写入违反数据库约束：{exc.orig}") from exc

    @staticmethod
    def _as_uuid(value: str | uuid.UUID) -> uuid.UUID:
        if isinstance(value, uuid.UUID):
            return value
        try:
            return uuid.UUID(str(value))
        except ValueError as exc:
            raise NotFoundError(
                f"虚拟改动 {value} 不存在", details={"change_id": str(value)}
            ) from exc
