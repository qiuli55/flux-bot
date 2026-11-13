"""制品页面两张表的读写入口。

一次调用一个会话、一个工作单元：创建页面与首个版本、追加版本与推进
`current_revision` 都在同一事务里完成，避免出现"版本写进去了但当前版本没动"
的半截状态。仓储层不做业务校验（长度 / 字符集 / 状态机），那些在服务层。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.errors import ConflictError, NotFoundError
from flux.models.artifact_page import ArtifactPage, ArtifactPageRevision
from flux.models.bot import Bot


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ArtifactPageRepository:
    """`artifact_pages` / `artifact_page_revisions` 的持久化读写。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def bot_exists(self, bot_id: uuid.UUID) -> bool:
        async with self._session_factory() as session:
            return await session.get(Bot, bot_id) is not None

    async def create(
        self,
        *,
        title: str,
        body: str,
        content_type: str,
        slug: str,
        share_token: str,
        bot_id: uuid.UUID | None,
        status: str,
    ) -> ArtifactPage:
        """创建页面 + 第 1 版；两者要么都成功，要么都不落库。"""
        now = _utcnow()
        page = ArtifactPage(
            id=uuid.uuid4(),
            bot_id=bot_id,
            title=title,
            slug=slug,
            share_token=share_token,
            status=status,
            current_revision=1,
            content_type=content_type,
            created_at=now,
            updated_at=now,
        )
        revision = ArtifactPageRevision(
            id=uuid.uuid4(),
            page_id=page.id,
            revision=1,
            title=title,
            body=body,
            content_type=content_type,
            author_bot_id=bot_id,
            created_at=now,
            updated_at=now,
        )
        async with self._session_factory() as session:
            session.add(page)
            session.add(revision)
            try:
                await session.commit()
            except IntegrityError as exc:
                # slug / share_token 撞车：两者都由服务层生成或校验，正常不会发生；
                # 真的撞上时给调用方一个明确的 409，而不是 500。
                await session.rollback()
                raise ConflictError("页面标识冲突，请重试") from exc
            await session.refresh(page)
            return page

    async def get(self, page_id: uuid.UUID) -> ArtifactPage | None:
        async with self._session_factory() as session:
            return await session.get(ArtifactPage, page_id)

    async def get_by_token(self, share_token: str) -> ArtifactPage | None:
        statement = select(ArtifactPage).where(ArtifactPage.share_token == share_token)
        async with self._session_factory() as session:
            return await session.scalar(statement)

    async def list(
        self,
        *,
        bot_id: uuid.UUID | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ArtifactPage]:
        statement = select(ArtifactPage)
        if bot_id is not None:
            statement = statement.where(ArtifactPage.bot_id == bot_id)
        if status is not None:
            statement = statement.where(ArtifactPage.status == status)
        statement = (
            statement.order_by(ArtifactPage.updated_at.desc(), ArtifactPage.id.desc())
            .limit(limit)
            .offset(offset)
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def add_revision(
        self,
        page_id: uuid.UUID,
        *,
        title: str,
        body: str,
        content_type: str,
        author_bot_id: uuid.UUID | None = None,
    ) -> tuple[ArtifactPage, ArtifactPageRevision]:
        """追加一版并把 `current_revision` 加一；旧版本行永不修改。"""
        now = _utcnow()
        async with self._session_factory() as session:
            page = await session.get(ArtifactPage, page_id)
            if page is None:
                raise NotFoundError(f"页面 {page_id} 不存在", details={"page_id": str(page_id)})
            number = int(page.current_revision) + 1
            revision = ArtifactPageRevision(
                id=uuid.uuid4(),
                page_id=page.id,
                revision=number,
                title=title,
                body=body,
                content_type=content_type,
                author_bot_id=author_bot_id,
                created_at=now,
                updated_at=now,
            )
            page.title = title
            page.content_type = content_type
            page.current_revision = number
            page.updated_at = now
            session.add(revision)
            try:
                await session.commit()
            except IntegrityError as exc:
                # (page_id, revision) 唯一约束：并发保存同一页时后到的一版重试即可，
                # 不能覆盖先落库的版本。
                await session.rollback()
                raise ConflictError("页面正被并发更新，请重试") from exc
            await session.refresh(page)
            await session.refresh(revision)
            return page, revision

    async def get_revision(self, page_id: uuid.UUID, revision: int) -> ArtifactPageRevision | None:
        statement = select(ArtifactPageRevision).where(
            ArtifactPageRevision.page_id == page_id,
            ArtifactPageRevision.revision == revision,
        )
        async with self._session_factory() as session:
            return await session.scalar(statement)

    async def list_revisions(self, page_id: uuid.UUID) -> list[ArtifactPageRevision]:
        statement = (
            select(ArtifactPageRevision)
            .where(ArtifactPageRevision.page_id == page_id)
            .order_by(ArtifactPageRevision.revision.asc())
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def set_status(self, page_id: uuid.UUID, status: str) -> ArtifactPage:
        async with self._session_factory() as session:
            page = await session.get(ArtifactPage, page_id)
            if page is None:
                raise NotFoundError(f"页面 {page_id} 不存在", details={"page_id": str(page_id)})
            page.status = status
            page.updated_at = _utcnow()
            await session.commit()
            await session.refresh(page)
            return page

    async def delete(self, page_id: uuid.UUID) -> bool:
        """删除页面及其全部版本。先显式删子表，不依赖数据库级联是否开启。"""
        async with self._session_factory() as session:
            page = await session.get(ArtifactPage, page_id)
            if page is None:
                return False
            await session.execute(
                delete(ArtifactPageRevision).where(ArtifactPageRevision.page_id == page_id)
            )
            await session.execute(delete(ArtifactPage).where(ArtifactPage.id == page_id))
            await session.commit()
            return True
