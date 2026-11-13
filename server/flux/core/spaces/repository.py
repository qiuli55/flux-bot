"""`spaces` / `space_members` 的持久化读写。

一次调用一个会话、一次提交，与其它仓储一致。创建空间与写入 owner 成员放在同一
事务里，避免出现「有空间、没 owner」的中间态。
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.errors import BadRequestError, NotFoundError
from flux.logging import get_logger
from flux.models.space import Space, SpaceMember

logger = get_logger(__name__)


class SpaceRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_with_owner(
        self,
        *,
        name: str,
        cloud_user_id: str,
        role: str = "owner",
    ) -> Space:
        async with self._session_factory() as session:
            space = Space(
                id=uuid.uuid4(),
                name=name,
                created_by_cloud_user_id=cloud_user_id,
            )
            session.add(space)
            session.add(
                SpaceMember(
                    id=uuid.uuid4(),
                    space_id=space.id,
                    cloud_user_id=cloud_user_id,
                    role=role,
                )
            )
            await self._commit(session)
            await session.refresh(space)
            return space

    async def get(self, space_id: uuid.UUID) -> Space:
        async with self._session_factory() as session:
            space = await session.get(Space, space_id)
            if space is None:
                raise NotFoundError(
                    f"共享空间 {space_id} 不存在", details={"space_id": str(space_id)}
                )
            return space

    async def list_for_user(self, cloud_user_id: str) -> list[tuple[Space, str]]:
        """当前用户加入的空间与角色，按创建时间升序。"""
        statement = (
            select(Space, SpaceMember.role)
            .join(SpaceMember, SpaceMember.space_id == Space.id)
            .where(SpaceMember.cloud_user_id == cloud_user_id)
            .order_by(Space.created_at.asc(), Space.id.asc())
        )
        async with self._session_factory() as session:
            rows = await session.execute(statement)
            return [(space, role) for space, role in rows.all()]

    async def member(self, space_id: uuid.UUID, cloud_user_id: str) -> SpaceMember | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(SpaceMember).where(
                    SpaceMember.space_id == space_id,
                    SpaceMember.cloud_user_id == cloud_user_id,
                )
            )

    async def members(self, space_id: uuid.UUID) -> list[SpaceMember]:
        statement = (
            select(SpaceMember)
            .where(SpaceMember.space_id == space_id)
            .order_by(SpaceMember.created_at.asc(), SpaceMember.id.asc())
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def count_owners(self, space_id: uuid.UUID) -> int:
        async with self._session_factory() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(SpaceMember)
                    .where(
                        SpaceMember.space_id == space_id,
                        SpaceMember.role == "owner",
                    )
                )
                or 0
            )

    async def upsert_member(
        self, space_id: uuid.UUID, cloud_user_id: str, role: str
    ) -> SpaceMember:
        """新增成员；已存在则更新角色（owner 可以因此把 member 提升为 owner）。"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SpaceMember).where(
                    SpaceMember.space_id == space_id,
                    SpaceMember.cloud_user_id == cloud_user_id,
                )
            )
            if row is None:
                row = SpaceMember(
                    id=uuid.uuid4(),
                    space_id=space_id,
                    cloud_user_id=cloud_user_id,
                    role=role,
                )
                session.add(row)
            else:
                row.role = role
            await self._commit(session)
            await session.refresh(row)
            return row

    async def remove_member(self, space_id: uuid.UUID, cloud_user_id: str) -> None:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(SpaceMember).where(
                    SpaceMember.space_id == space_id,
                    SpaceMember.cloud_user_id == cloud_user_id,
                )
            )
            if result.rowcount == 0:
                await session.rollback()
                raise NotFoundError(
                    f"成员 {cloud_user_id} 不在共享空间 {space_id}",
                    details={"space_id": str(space_id), "cloud_user_id": cloud_user_id},
                )
            await session.commit()

    async def _commit(self, session: AsyncSession) -> None:
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            logger.warning("共享空间写入违反数据库约束：%s", exc.orig)
            raise BadRequestError(f"共享空间写入违反数据库约束：{exc.orig}") from exc
