"""共享空间服务：建空间、管成员、作为 shared 记忆的访问闸门。

身份口径与 Phase 2 一致：`cloud_user_id` 是 Flux 云账号 id。非成员访问空间一律
按「不存在」（404）处理——这两种情况对调用方没有区别，避免泄露空间是否存在。
只有 owner 能增删成员；owner / member 都能读写 shared 记忆。
"""

from __future__ import annotations

import uuid
from typing import Any

from flux.core.spaces.repository import SpaceRepository
from flux.core.user_scope import normalize_cloud_user_id
from flux.enums import SpaceRole
from flux.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from flux.logging import get_logger
from flux.models.space import Space, SpaceMember

logger = get_logger(__name__)

MAX_NAME_CHARS = 64
ROLES = frozenset({SpaceRole.OWNER.value, SpaceRole.MEMBER.value})


class SpaceService:
    def __init__(self, repository: SpaceRepository) -> None:
        self._repo = repository

    # --- 空间 ---

    async def create(self, *, name: str, cloud_user_id: str) -> Space:
        user = _require_user(cloud_user_id)
        clean = (name or "").strip()
        if not clean:
            raise ValidationError("共享空间名称不能为空", details={"field": "name"})
        if len(clean) > MAX_NAME_CHARS:
            raise ValidationError(
                f"共享空间名称超过 {MAX_NAME_CHARS} 字符",
                details={"field": "name", "max_length": MAX_NAME_CHARS},
            )
        space = await self._repo.create_with_owner(name=clean, cloud_user_id=user)
        logger.info("space.created id=%s owner=%s", space.id, user)
        return space

    async def list(self, cloud_user_id: str) -> list[dict[str, Any]]:
        user = _require_user(cloud_user_id)
        return [
            {**space.to_dict(), "role": role}
            for space, role in await self._repo.list_for_user(user)
        ]

    async def get(self, space_id: uuid.UUID | str, cloud_user_id: str) -> Space:
        return (await self._member_view(space_id, cloud_user_id))[0]

    async def view(self, space_id: uuid.UUID | str, cloud_user_id: str) -> dict[str, Any]:
        space, member = await self._member_view(space_id, cloud_user_id)
        return {**space.to_dict(), "role": member.role}

    async def memberships(self, cloud_user_id: str) -> list[tuple[Space, str]]:
        """当前用户加入的空间与角色（供 shared 记忆按空间分组）。"""
        user = normalize_cloud_user_id(cloud_user_id)
        if user is None:
            return []
        return await self._repo.list_for_user(user)

    async def members(self, space_id: uuid.UUID | str, cloud_user_id: str) -> list[SpaceMember]:
        space = await self.get(space_id, cloud_user_id)
        return await self._repo.members(space.id)

    async def role(self, space_id: uuid.UUID | str, cloud_user_id: str) -> str | None:
        user = normalize_cloud_user_id(cloud_user_id)
        space = await self._space_or_none(space_id)
        if user is None or space is None:
            return None
        member = await self._repo.member(space.id, user)
        return member.role if member is not None else None

    async def is_member(self, space_id: uuid.UUID | str, cloud_user_id: str) -> bool:
        return await self.role(space_id, cloud_user_id) is not None

    async def require_member(self, space_id: uuid.UUID | str, cloud_user_id: str) -> Space:
        return await self.get(space_id, cloud_user_id)

    async def require_owner(self, space_id: uuid.UUID | str, cloud_user_id: str) -> Space:
        space, member = await self._member_view(space_id, cloud_user_id)
        if member.role != SpaceRole.OWNER.value:
            raise PermissionDeniedError(
                "只有共享空间的 owner 能管理成员",
                details={"space_id": str(space.id), "role": member.role},
            )
        return space

    # --- 成员 ---

    async def add_member(
        self,
        space_id: uuid.UUID | str,
        *,
        actor_cloud_user_id: str,
        cloud_user_id: str,
        role: str = SpaceRole.MEMBER.value,
    ) -> SpaceMember:
        space = await self.require_owner(space_id, actor_cloud_user_id)
        target = _require_user(cloud_user_id)
        clean_role = (role or SpaceRole.MEMBER.value).strip().lower()
        if clean_role not in ROLES:
            raise ValidationError(
                f"未知的成员角色：{role}",
                details={"field": "role", "allowed": sorted(ROLES)},
            )
        existing = await self._repo.member(space.id, target)
        if (
            existing is not None
            and existing.role == SpaceRole.OWNER.value
            and clean_role != SpaceRole.OWNER.value
            and await self._repo.count_owners(space.id) <= 1
        ):
            # 与移除成员同一条底线：不能把空间改成没有 owner。
            raise ConflictError(
                "共享空间至少需要保留一个 owner",
                details={"space_id": str(space.id), "cloud_user_id": target},
            )
        row = await self._repo.upsert_member(space.id, target, clean_role)
        logger.info("space.member.upsert space=%s user=%s role=%s", space.id, target, clean_role)
        return row

    async def remove_member(
        self,
        space_id: uuid.UUID | str,
        *,
        actor_cloud_user_id: str,
        cloud_user_id: str,
    ) -> None:
        space = await self.require_owner(space_id, actor_cloud_user_id)
        target = _require_user(cloud_user_id)
        member = await self._repo.member(space.id, target)
        if member is None:
            raise NotFoundError(
                f"成员 {target} 不在共享空间 {space.id}",
                details={"space_id": str(space.id), "cloud_user_id": target},
            )
        if member.role == SpaceRole.OWNER.value and await self._repo.count_owners(space.id) <= 1:
            # 不允许把空间留成「没有 owner」：否则之后没人能再管理成员。
            raise ConflictError(
                "共享空间至少需要保留一个 owner",
                details={"space_id": str(space.id), "cloud_user_id": target},
            )
        await self._repo.remove_member(space.id, target)
        logger.info("space.member.removed space=%s user=%s", space.id, target)

    # --- 内部 ---

    async def _member_view(
        self, space_id: uuid.UUID | str, cloud_user_id: str
    ) -> tuple[Space, SpaceMember]:
        user = _require_user(cloud_user_id)
        space = await self._space_or_none(space_id)
        member = await self._repo.member(space.id, user) if space is not None else None
        if space is None or member is None:
            # 非成员与不存在返回同一个错误，不泄露空间是否存在。
            raise NotFoundError(f"共享空间 {space_id} 不存在", details={"space_id": str(space_id)})
        return space, member

    async def _space_or_none(self, space_id: uuid.UUID | str) -> Space | None:
        key = _as_uuid(space_id)
        if key is None:
            return None
        try:
            return await self._repo.get(key)
        except NotFoundError:
            return None


def _require_user(value: object) -> str:
    user = normalize_cloud_user_id(value)
    if user is None:
        raise PermissionDeniedError(
            "该操作需要 Flux 账号登录",
            code="cloud_login_required",
        )
    return user


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


__all__ = ["SpaceService"]
