"""技能库持久化：技能包 CRUD 与 Bot 分配。

仓储层只做读写；名称校验、删除冲突与注入预算都在 `flux.core.skills.service`。
技能正文（body）不在这里打日志，避免大段提示词进日志。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.models.skill_package import BotSkill, SkillPackage


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SkillLibraryRepository:
    """`skill_packages` / `bot_skills` 的读写入口。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # --- 技能包 ---

    async def create(
        self,
        *,
        name: str,
        description: str,
        version: str | None,
        spec: dict[str, Any],
        body: str,
        enabled: bool,
    ) -> SkillPackage:
        now = _utcnow()
        row = SkillPackage(
            name=name,
            description=description,
            version=version,
            spec=spec,
            body=body,
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def get(self, skill_id: uuid.UUID) -> SkillPackage | None:
        async with self._session_factory() as session:
            return await session.get(SkillPackage, skill_id)

    async def get_by_name(self, name: str) -> SkillPackage | None:
        async with self._session_factory() as session:
            return await session.scalar(select(SkillPackage).where(SkillPackage.name == name))

    async def get_many(self, skill_ids: list[uuid.UUID]) -> dict[uuid.UUID, SkillPackage]:
        if not skill_ids:
            return {}
        statement = select(SkillPackage).where(SkillPackage.id.in_(skill_ids))
        async with self._session_factory() as session:
            rows = list(await session.scalars(statement))
        return {row.id: row for row in rows}

    async def list(self, *, enabled: bool | None = None, limit: int = 50) -> list[SkillPackage]:
        statement = select(SkillPackage).order_by(
            SkillPackage.created_at.desc(), SkillPackage.id.desc()
        )
        if enabled is not None:
            statement = statement.where(SkillPackage.enabled.is_(enabled))
        statement = statement.limit(limit)
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def update(self, skill_id: uuid.UUID, values: dict[str, Any]) -> SkillPackage | None:
        async with self._session_factory() as session:
            row = await session.get(SkillPackage, skill_id)
            if row is None:
                return None
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_at = _utcnow()
            await session.commit()
            await session.refresh(row)
            return row

    async def delete(self, skill_id: uuid.UUID) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(delete(SkillPackage).where(SkillPackage.id == skill_id))
            await session.commit()
            return bool(result.rowcount)

    # --- Bot 分配 ---

    async def assigned_bot_ids(self, skill_id: uuid.UUID) -> list[uuid.UUID]:
        statement = select(BotSkill.bot_id).where(BotSkill.skill_id == skill_id)
        async with self._session_factory() as session:
            return [row for row in await session.scalars(statement)]

    async def list_for_bot(
        self, bot_id: uuid.UUID, *, enabled_only: bool = False
    ) -> list[tuple[BotSkill, SkillPackage]]:
        statement = (
            select(BotSkill, SkillPackage)
            .join(SkillPackage, BotSkill.skill_id == SkillPackage.id)
            .where(BotSkill.bot_id == bot_id)
            .order_by(
                BotSkill.position.asc(),
                BotSkill.created_at.asc(),
                SkillPackage.name.asc(),
            )
        )
        if enabled_only:
            statement = statement.where(
                BotSkill.enabled.is_(True),
                SkillPackage.enabled.is_(True),
            )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        return [(row[0], row[1]) for row in rows]

    async def replace_for_bot(
        self,
        bot_id: uuid.UUID,
        entries: list[tuple[uuid.UUID, int, bool]],
    ) -> None:
        """整表替换该 Bot 的分配：先删后插，同一事务提交。"""
        now = _utcnow()
        async with self._session_factory() as session:
            await session.execute(delete(BotSkill).where(BotSkill.bot_id == bot_id))
            for skill_id, position, enabled in entries:
                session.add(
                    BotSkill(
                        bot_id=bot_id,
                        skill_id=skill_id,
                        position=position,
                        enabled=enabled,
                        created_at=now,
                        updated_at=now,
                    )
                )
            await session.commit()

    async def delete_assignment(self, bot_id: uuid.UUID, skill_id: uuid.UUID) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(BotSkill).where(
                    BotSkill.bot_id == bot_id,
                    BotSkill.skill_id == skill_id,
                )
            )
            await session.commit()
            return bool(result.rowcount)
