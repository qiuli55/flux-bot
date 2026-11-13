"""技能库服务：技能包 CRUD、Bot 分配与对话上下文注入。

三个约束值得记住：

- 删除策略：技能包仍被任何 Bot 分配时返回 409，必须先解除分配再删除；
  数据库外键同样是 RESTRICT，应用层与存储层的口径一致。
- 注入只读数据库，不经过 agent_runtime / DSH / MCP 工具注册；v1 只是把
  技能正文拼进 Bot 对话的 context。
- 日志只记 id / name / 字符数，绝不打印正文。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from flux.core.skills.package import build_package, parse_package_document
from flux.core.skills.repository import SkillLibraryRepository
from flux.errors import BadRequestError, ConflictError, NotFoundError, ValidationError
from flux.logging import get_logger
from flux.models.skill_package import BotSkill, SkillPackage

logger = get_logger(__name__)

#: 注入上下文的技能总字符预算：技能多时按顺序截断，不能让一个 Bot 撑爆上下文。
SKILLS_BLOCK_MAX_CHARS = 12_000
_TRUNCATE_NOTE = "\n……（技能内容超出上下文预算，已截断）"
_BLOCK_HEADER = "【技能说明】以下技能包由平台为此 Bot 启用，按顺序参考执行：\n"
_BLOCK_FOOTER = "\n【技能说明结束】"

#: 允许通过 PATCH 单独修改的包字段（content 走整包重解析）。
_PACKAGE_KEYS = frozenset(
    {
        "name",
        "description",
        "version",
        "body",
        "entry",
        "requires_bins",
        "requires_skills",
        "permissions",
        "network",
        "files",
    }
)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _as_skill_uuid(value: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise NotFoundError(f"技能 {value} 不存在", details={"skill_id": str(value)}) from exc


def _row_package_data(row: SkillPackage) -> dict[str, Any]:
    """把库里的行还原成 build_package 可接受的字段集合。"""
    spec = row.spec if isinstance(row.spec, Mapping) else {}
    return {
        "name": row.name,
        "description": row.description or "",
        "version": row.version,
        "entry": spec.get("entry", "SKILL.md"),
        "requires_bins": spec.get("requires_bins", []),
        "requires_skills": spec.get("requires_skills", []),
        "permissions": spec.get("permissions", []),
        "network": spec.get("network", False),
        "files": spec.get("files", []),
        "body": row.body,
    }


def _assignment_dict(assignment: BotSkill, package: SkillPackage) -> dict[str, Any]:
    data = assignment.to_dict()
    data.update(
        {
            "name": package.name,
            "description": package.description or "",
            "version": package.version,
            "package_enabled": bool(package.enabled),
            "body_chars": len(package.body or ""),
            "created_at": _iso(assignment.created_at),
        }
    )
    return data


class SkillLibraryService:
    """技能库的领域服务。"""

    def __init__(self, repository: SkillLibraryRepository) -> None:
        self._repo = repository

    # --- 技能包 CRUD ---

    async def create(
        self,
        *,
        document: str | None = None,
        data: Mapping[str, Any] | None = None,
        enabled: bool = True,
    ) -> SkillPackage:
        """从 SKILL.md 文本（document）或 JSON 字段（data）创建一个技能包。"""
        if document is not None:
            package = parse_package_document(document)
        elif data is not None:
            package = build_package(data)
        else:
            raise ValidationError("缺少技能包内容", details={"field": "content"})

        if await self._repo.get_by_name(package.name) is not None:
            raise ConflictError(f"同名技能已存在：{package.name}", details={"name": package.name})
        try:
            row = await self._repo.create(
                name=package.name,
                description=package.description,
                version=package.version,
                spec=package.to_spec(),
                body=package.body,
                enabled=bool(enabled),
            )
        except IntegrityError as exc:
            raise ConflictError(
                f"同名技能已存在：{package.name}", details={"name": package.name}
            ) from exc
        # 只记元数据与字符数；正文（提示词）绝不进日志。
        logger.info("技能包创建 id=%s name=%s chars=%d", row.id, row.name, len(row.body))
        return row

    async def list(self, *, enabled: bool | None = None, limit: int = 50) -> list[SkillPackage]:
        return await self._repo.list(enabled=enabled, limit=limit)

    async def get(self, skill_id: uuid.UUID) -> SkillPackage:
        row = await self._repo.get(skill_id)
        if row is None:
            raise NotFoundError(f"技能 {skill_id} 不存在", details={"skill_id": str(skill_id)})
        return row

    async def update(self, skill_id: uuid.UUID, changes: Mapping[str, Any]) -> SkillPackage:
        """部分更新；传 content 时整包重新解析，不能与单字段修改混用。"""
        row = await self.get(skill_id)
        enabled = bool(changes.get("enabled", row.enabled))
        content = changes.get("content")
        if content is not None:
            extras = sorted(set(changes) - {"content", "enabled"})
            if extras:
                raise ValidationError(
                    "提供 content 时不能再同时修改单个字段",
                    details={"fields": extras},
                )
            package = parse_package_document(content)
        else:
            data = _row_package_data(row)
            for key in _PACKAGE_KEYS:
                if key in changes:
                    data[key] = changes[key]
            package = build_package(data)

        if package.name != row.name and await self._repo.get_by_name(package.name) is not None:
            raise ConflictError(f"同名技能已存在：{package.name}", details={"name": package.name})
        try:
            updated = await self._repo.update(
                row.id,
                {
                    "name": package.name,
                    "description": package.description,
                    "version": package.version,
                    "spec": package.to_spec(),
                    "body": package.body,
                    "enabled": enabled,
                },
            )
        except IntegrityError as exc:
            raise ConflictError(
                f"同名技能已存在：{package.name}", details={"name": package.name}
            ) from exc
        if updated is None:  # 并发删除
            raise NotFoundError(f"技能 {skill_id} 不存在", details={"skill_id": str(skill_id)})
        logger.info(
            "技能包更新 id=%s name=%s chars=%d",
            updated.id,
            updated.name,
            len(updated.body),
        )
        return updated

    async def delete(self, skill_id: uuid.UUID) -> None:
        """删除技能包；仍被 Bot 分配时 409，要求先解除分配。"""
        row = await self.get(skill_id)
        assigned = await self._repo.assigned_bot_ids(row.id)
        if assigned:
            raise ConflictError(
                f"技能 {row.name} 仍分配给 {len(assigned)} 个 Bot，请先解除分配再删除",
                details={
                    "skill_id": str(row.id),
                    "assigned_bots": [str(bot_id) for bot_id in assigned[:20]],
                },
            )
        try:
            deleted = await self._repo.delete(row.id)
        except IntegrityError as exc:
            raise ConflictError(
                f"技能 {row.name} 仍被 Bot 使用，请先解除分配再删除",
                details={"skill_id": str(row.id)},
            ) from exc
        if not deleted:
            raise NotFoundError(f"技能 {skill_id} 不存在", details={"skill_id": str(skill_id)})
        logger.info("技能包删除 id=%s name=%s", row.id, row.name)

    # --- Bot 分配 ---

    async def list_for_bot(self, bot_id: uuid.UUID) -> list[dict[str, Any]]:
        rows = await self._repo.list_for_bot(bot_id)
        return [_assignment_dict(assignment, package) for assignment, package in rows]

    async def replace_for_bot(
        self, bot_id: uuid.UUID, entries: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """整表替换某 Bot 的技能分配；position 缺省按请求顺序。"""
        normalized: list[tuple[uuid.UUID, int, bool]] = []
        seen: dict[uuid.UUID, int] = {}
        for index, entry in enumerate(entries):
            skill_id = _as_skill_uuid(entry.get("skill_id"))
            if skill_id in seen:
                raise BadRequestError(
                    "同一技能不能重复分配",
                    details={"skill_id": str(skill_id)},
                )
            seen[skill_id] = index
            position = entry.get("position")
            if position is None:
                position = index
            if not isinstance(position, int) or isinstance(position, bool) or position < 0:
                raise ValidationError("position 必须是非负整数", details={"field": "position"})
            normalized.append((skill_id, position, bool(entry.get("enabled", True))))

        existing = await self._repo.get_many(list(seen))
        missing = sorted(str(skill_id) for skill_id in seen if skill_id not in existing)
        if missing:
            raise NotFoundError(
                "技能不存在",
                details={"skill_ids": missing},
            )
        await self._repo.replace_for_bot(bot_id, normalized)
        logger.info("Bot 技能分配 bot_id=%s count=%d", bot_id, len(normalized))
        return await self.list_for_bot(bot_id)

    async def remove_from_bot(self, bot_id: uuid.UUID, skill_id: uuid.UUID) -> None:
        removed = await self._repo.delete_assignment(bot_id, skill_id)
        if not removed:
            raise NotFoundError(
                f"Bot 未分配技能 {skill_id}",
                details={"bot_id": str(bot_id), "skill_id": str(skill_id)},
            )
        logger.info("Bot 技能解除分配 bot_id=%s skill_id=%s", bot_id, skill_id)

    # --- 对话上下文注入 ---

    async def render_block(self, bot_id: uuid.UUID) -> str:
        """渲染启用中的技能包；没有可注入内容时返回空串。

        只取「分配启用 + 技能包启用」的行，按 position 顺序；总字符预算用尽后
        截断并显式标注，保证注入内容有上限。
        """
        rows = await self._repo.list_for_bot(bot_id, enabled_only=True)
        if not rows:
            return ""

        pieces: list[str] = [_BLOCK_HEADER]
        used = len(_BLOCK_HEADER)
        included = 0
        truncated = False
        for _assignment, package in rows:
            body = (package.body or "").strip()
            if not body:
                continue
            heading = f"\n### {package.name}\n"
            if package.description:
                heading += f"{package.description}\n"
            # 预留 footer，保证追加结尾后仍不超预算
            remaining = SKILLS_BLOCK_MAX_CHARS - used - len(_BLOCK_FOOTER)
            if remaining <= len(heading):
                truncated = True
                break
            room = remaining - len(heading)
            if len(body) > room:
                note = _TRUNCATE_NOTE
                keep = max(0, room - len(note))
                piece = body[:keep] + note if keep else note
                piece = piece[:room]
                pieces.append(heading + piece)
                used += len(heading) + len(piece)
                included += 1
                truncated = True
                break
            pieces.append(heading + body)
            used += len(heading) + len(body)
            included += 1

        if included == 0:
            return ""
        pieces.append(_BLOCK_FOOTER)
        text = "".join(pieces)
        logger.info(
            "Bot 技能渲染 bot_id=%s skills=%d chars=%d truncated=%s",
            bot_id,
            included,
            len(text),
            truncated,
        )
        return text


__all__ = [
    "SKILLS_BLOCK_MAX_CHARS",
    "SkillLibraryService",
]
