"""内部多 bot 群聊 API。

与微信/飞书那种「外部群」无关：这里的群成员是本平台的常驻 Bot（bots）。
用户发言后服务端起一个后台 epoch，按入群顺序给每个 bot 一次发言机会；
前端只按 seq 轮询拉取整条消息，不做流式（群聊里逐字渲染太拥挤）。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Depends

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.core.group.repository import GroupMessageDraft
from flux.errors import BadRequestError
from flux.logging import get_logger
from flux.models.bot import Bot
from flux.models.group import ChatGroup, ChatGroupMember, ChatGroupMessage
from flux.schemas.api import (
    GroupCreateRequest,
    GroupMemberAddRequest,
    GroupMemberUpdateRequest,
    GroupMessageCreateRequest,
)

router = APIRouter(prefix="/groups", tags=["groups"])

logger = get_logger(__name__)

#: 后台 epoch 任务的强引用：避免 asyncio 把还在跑的任务回收掉。
_RUNNING: set[asyncio.Task[None]] = set()


def _parse_uuid(value: str, *, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise BadRequestError(f"{field} 不是合法 UUID：{value}", details={field: value}) from exc


def _group_id(value: str) -> uuid.UUID:
    return _parse_uuid(value, field="group_id")


def _bot_name(bot: Bot) -> str:
    name = (bot.name or "").strip()
    return name or "未命名 Bot"


def _member_dict(member: ChatGroupMember, bot: Bot) -> dict[str, Any]:
    return {
        "bot_id": str(bot.id),
        "name": _bot_name(bot),
        "description": bot.description,
        "avatar_shape": bot.avatar_shape or "circle",
        "avatar_color": bot.avatar_color or "violet",
        "status": bot.status,
        "role": member.role,
        "model": bot.model,
        "api_key_id": str(bot.api_key_id) if bot.api_key_id is not None else None,
        "permission_mode": bot.permission_mode or "unattended",
    }


def _group_dict(
    group: ChatGroup, members: list[tuple[ChatGroupMember, Bot]], count: int | None = None
) -> dict[str, Any]:
    data = group.to_dict()
    data["member_count"] = len(members) if members else int(count or 0)
    data["members"] = [_member_dict(member, bot) for member, bot in members]
    return data


def _message_dict(message: ChatGroupMessage) -> dict[str, Any]:
    return message.to_dict()


def _detect_mentions(content: str, members: list[Bot]) -> set[uuid.UUID]:
    """按 bot 名字匹配 @ 提及（支持 `@名字`，名字允许带空格时取最长匹配）。"""
    mentioned: set[uuid.UUID] = set()
    for bot in members:
        name = _bot_name(bot)
        if name and f"@{name}" in content:
            mentioned.add(bot.id)
    return mentioned


@router.get("")
async def list_groups(
    limit: int = 50,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    groups = await container.group_repo.list(limit=max(1, min(limit, 200)))
    members_map = await container.group_repo.list_members_map([group.id for group in groups])
    return ok([_group_dict(group, members_map.get(group.id, [])) for group in groups])


@router.post("")
async def create_group(
    payload: GroupCreateRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    project_id = _parse_uuid(payload.project_id, field="project_id") if payload.project_id else None
    bot_ids = [_parse_uuid(raw, field="bot_ids") for raw in payload.bot_ids]
    admin_id = (
        _parse_uuid(payload.admin_bot_id, field="admin_bot_id") if payload.admin_bot_id else None
    )
    if admin_id is not None and admin_id not in bot_ids:
        raise BadRequestError(
            "admin_bot_id 必须是 bot_ids 中的成员",
            details={"admin_bot_id": str(admin_id)},
        )
    group = await container.group_repo.create(
        name=payload.name,
        description=payload.description,
        project_id=project_id,
        max_rounds=payload.max_rounds,
    )
    for bot_id in bot_ids:
        await container.group_repo.add_member(
            group.id, bot_id, role="admin" if bot_id == admin_id else "member"
        )
    members = await container.group_repo.list_members(group.id)
    return ok(_group_dict(group, members))


@router.get("/{group_id}")
async def get_group(
    group_id: str,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    group = await container.group_repo.get(_group_id(group_id))
    members = await container.group_repo.list_members(group.id)
    return ok(_group_dict(group, members))


@router.patch("/{group_id}")
async def update_group(
    group_id: str,
    payload: GroupCreateRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    group = await container.group_repo.update(
        _group_id(group_id),
        name=payload.name,
        description=payload.description,
        max_rounds=payload.max_rounds,
    )
    members = await container.group_repo.list_members(group.id)
    return ok(_group_dict(group, members))


@router.delete("/{group_id}")
async def delete_group(
    group_id: str,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    await container.group_repo.delete(_group_id(group_id))
    return ok({"deleted": True})


@router.post("/{group_id}/members")
async def add_member(
    group_id: str,
    payload: GroupMemberAddRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    group_uuid = _group_id(group_id)
    await container.group_repo.add_member(
        group_uuid, _parse_uuid(payload.bot_id, field="bot_id"), role=payload.role
    )
    members = await container.group_repo.list_members(group_uuid)
    return ok([_member_dict(member, bot) for member, bot in members])


@router.patch("/{group_id}/members/{bot_id}")
async def update_member(
    group_id: str,
    bot_id: str,
    payload: GroupMemberUpdateRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """调整成员角色 / 模型 / 权限预设；只有人能改，bot 不能自改。"""
    group_uuid = _group_id(group_id)
    bot_uuid = _parse_uuid(bot_id, field="bot_id")
    await container.group_repo.get(group_uuid)

    if payload.role is not None:
        await container.group_repo.set_member_role(group_uuid, bot_uuid, payload.role)

    changes: dict[str, object] = {}
    if payload.model is not None:
        changes["model"] = payload.model
        if payload.key_id is not None:
            changes["key_id"] = payload.key_id
    if payload.permission_mode is not None:
        changes["permission_mode"] = str(payload.permission_mode)
    if changes:
        await container.bots.update(bot_uuid, changes)

    members = await container.group_repo.list_members(group_uuid)
    return ok([_member_dict(member, bot) for member, bot in members])


@router.delete("/{group_id}/members/{bot_id}")
async def remove_member(
    group_id: str,
    bot_id: str,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    group_uuid = _group_id(group_id)
    bot_uuid = _parse_uuid(bot_id, field="bot_id")
    removed = await container.group_repo.remove_member(group_uuid, bot_uuid)
    members = await container.group_repo.list_members(group_uuid)
    return ok(
        {
            "removed": removed,
            "members": [_member_dict(member, bot) for member, bot in members],
        }
    )


@router.get("/{group_id}/memory")
async def get_group_memory(
    group_id: str,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """查看分层记忆：本群共享记忆 + 团队共享记忆（只读，由编排器维护）。"""
    group_uuid = _group_id(group_id)
    await container.group_repo.get(group_uuid)
    group_memory = await container.group_repo.get_memory(group_uuid, scope="group")
    team_memory = await container.group_repo.get_memory(None, scope="team")
    return ok(
        {
            "group": group_memory.to_dict() if group_memory is not None else None,
            "team": team_memory.to_dict() if team_memory is not None else None,
        }
    )


@router.get("/{group_id}/messages")
async def list_messages(
    group_id: str,
    after_seq: int = 0,
    limit: int = 100,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    messages, has_more = await container.group_repo.list_messages(
        _group_id(group_id),
        after_seq=max(0, after_seq),
        limit=max(1, min(limit, 200)),
    )
    return ok({"messages": [_message_dict(message) for message in messages], "has_more": has_more})


@router.post("/{group_id}/messages")
async def create_message(
    group_id: str,
    payload: GroupMessageCreateRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """用户发言：落到群里 → 打断上一个 epoch → 起一个新的后台 epoch。"""
    group_uuid = _group_id(group_id)
    group = await container.group_repo.get(group_uuid)
    members = [bot for _member, bot in await container.group_repo.list_members(group_uuid)]

    await container.group_repo.interrupt_active_epoch(group_uuid)
    created = await container.group_repo.add_messages(
        group_uuid,
        [
            GroupMessageDraft(
                role="user",
                content=payload.content.strip(),
                author_name="我",
                kind="text",
            )
        ],
    )
    user_message = created[0]
    mentioned = _detect_mentions(payload.content, members)

    epoch = await container.group_repo.create_epoch(
        group_uuid, trigger="user", max_rounds=group.max_rounds
    )
    runner = asyncio.create_task(
        container.groups.run_epoch(group_uuid, epoch.id, mentioned=mentioned)
    )
    _RUNNING.add(runner)
    runner.add_done_callback(_RUNNING.discard)

    return ok(
        {
            "message": _message_dict(user_message),
            "epoch_id": str(epoch.id),
            "mentioned": [str(item) for item in mentioned],
        }
    )


@router.post("/{group_id}/interrupt")
async def interrupt_group(
    group_id: str,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    group_uuid = _group_id(group_id)
    # 先确认群存在：对不存在的群返回「没被打断」会让前端误以为群还在
    await container.group_repo.get(group_uuid)
    epoch = await container.group_repo.interrupt_active_epoch(group_uuid)
    return ok({"interrupted": epoch is not None, "epoch_id": str(epoch.id) if epoch else None})
