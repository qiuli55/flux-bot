"""Project Brain 持久化读写（主规格 §5.6 / §11.2；实施计划 ⑪）。

两张既有表：

- `projects`：项目本体（名称、仓库地址、metadata）
- `project_memory`：记忆条目（type = `flux.enums.BrainSection` 的取值）

第一版是**结构化**记忆，不上 pgvector：`embedding` 列先留空，语义检索按主规格
§11.4 与实施计划 ⑪ 的边界放到 M5，届时只补一个检索层，不改这张表。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.enums import BrainSection
from flux.errors import BadRequestError, NotFoundError
from flux.logging import get_logger
from flux.models.project import Project, ProjectMemory

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProjectBrainRepository:
    """projects / project_memory 的读写入口。每次调用自开一个会话并提交。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # --- 项目 ---

    async def create_project(
        self,
        *,
        name: str,
        repository: str | None = None,
        project_id: uuid.UUID | None = None,
        meta: dict | None = None,
    ) -> Project:
        project = Project(
            id=project_id or uuid.uuid4(),
            name=name,
            repository=repository,
            meta=meta or {},
        )
        async with self._session_factory() as session:
            session.add(project)
            await self._persist(session, project, "项目")
            return project

    async def get_project(self, project_id: str | uuid.UUID) -> Project:
        async with self._session_factory() as session:
            project = await session.get(Project, _as_uuid(project_id))
            if project is None:
                raise NotFoundError(
                    f"项目 {project_id} 不存在", details={"project_id": str(project_id)}
                )
            return project

    async def list_projects(self) -> list[Project]:
        statement = select(Project).order_by(Project.created_at, Project.id)
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    # --- 记忆 ---

    async def replace_section(
        self,
        project_id: str | uuid.UUID,
        *,
        section: BrainSection,
        content: str,
        meta: dict | None = None,
    ) -> ProjectMemory:
        """写入"项目现状"型分区：同一分区只保留一份最新内容（覆盖式）。

        覆盖而不是追加，是为了让 `context()` 拿到的一定是当前事实，
        不用在读取时猜"哪条才是最新的"。历史决策另有 `DECISIONS` 分区承载。
        """
        key = _as_uuid(project_id)
        now = _utcnow()
        async with self._session_factory() as session:
            await self._require_project(session, key, project_id)
            existing = await session.scalar(
                select(ProjectMemory)
                .where(ProjectMemory.project_id == key, ProjectMemory.type == section.value)
                .order_by(ProjectMemory.created_at, ProjectMemory.id)
                .limit(1)
            )
            if existing is None:
                entry = ProjectMemory(
                    id=uuid.uuid4(),
                    project_id=key,
                    type=section.value,
                    content=content,
                    meta=meta or {},
                    created_at=now,
                    updated_at=now,
                )
                session.add(entry)
            else:
                existing.content = content
                existing.meta = meta or {}
                existing.updated_at = now
                entry = existing
            await self._persist(session, entry, "记忆条目")
            return entry

    async def append_entry(
        self,
        project_id: str | uuid.UUID,
        *,
        section: BrainSection,
        content: str,
        meta: dict | None = None,
    ) -> ProjectMemory:
        """写入"累积"型分区：每次新增一条，历史不可被覆盖。"""
        key = _as_uuid(project_id)
        now = _utcnow()
        async with self._session_factory() as session:
            await self._require_project(session, key, project_id)
            entry = ProjectMemory(
                id=uuid.uuid4(),
                project_id=key,
                type=section.value,
                content=content,
                meta=meta or {},
                created_at=now,
                updated_at=now,
            )
            session.add(entry)
            await self._persist(session, entry, "记忆条目")
            return entry

    async def prune_section(
        self,
        project_id: str | uuid.UUID,
        *,
        section: BrainSection,
        keep: int,
    ) -> int:
        """容量清理：某累积分区只保留最新 keep 条，多余的最旧条目删除，返回删除数。

        写入时间是新条目入库前取的微秒级 UTC 时间戳（见 append_entry），
        "谁更新"在任何数据库上都可确定排序。
        """
        key = _as_uuid(project_id)
        async with self._session_factory() as session:
            stale = list(
                await session.scalars(
                    select(ProjectMemory.id)
                    .where(ProjectMemory.project_id == key, ProjectMemory.type == section.value)
                    .order_by(ProjectMemory.created_at.desc(), ProjectMemory.id.desc())
                    .offset(keep)
                )
            )
            if not stale:
                return 0
            await session.execute(delete(ProjectMemory).where(ProjectMemory.id.in_(stale)))
            await session.commit()
            return len(stale)

    async def entries(
        self,
        project_id: str | uuid.UUID,
        *,
        section: BrainSection | None = None,
    ) -> list[ProjectMemory]:
        statement = (
            select(ProjectMemory)
            .where(ProjectMemory.project_id == _as_uuid(project_id))
            .order_by(ProjectMemory.created_at, ProjectMemory.id)
        )
        if section is not None:
            statement = statement.where(ProjectMemory.type == section.value)
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    # --- 内部 ---

    @staticmethod
    async def _require_project(session: AsyncSession, key: uuid.UUID, raw: str | uuid.UUID) -> None:
        exists = await session.scalar(select(Project.id).where(Project.id == key))
        if exists is None:
            raise NotFoundError(f"项目 {raw} 不存在", details={"project_id": str(raw)})

    @staticmethod
    async def _persist(session: AsyncSession, entity: Project | ProjectMemory, label: str) -> None:
        """提交并重新读回实体。

        `created_at` / `updated_at` 是数据库端默认值（`server_default` / `onupdate`），
        提交后属性会过期；这里显式 refresh，保证调用方（尤其是返回给 API 的 to_dict）
        拿到的是真实时间戳，而不是触发 DetachedInstanceError。
        """
        await ProjectBrainRepository._commit(session, label)
        await session.refresh(entity)

    @staticmethod
    async def _commit(session: AsyncSession, label: str) -> None:
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            logger.warning("%s写入违反数据库约束：%s", label, exc.orig)
            raise BadRequestError(f"{label}写入违反数据库约束：{exc.orig}") from exc


def _as_uuid(value: str | uuid.UUID) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise NotFoundError(f"项目 {value} 不存在", details={"project_id": str(value)}) from exc
