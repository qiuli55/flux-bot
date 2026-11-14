"""审批规则持久化服务（按 Flux 云用户隔离）。

- REST CRUD 与 Bot 工具共用本服务：参数校验、默认中文描述、事件发布只有这一处；
- 所有读写都带 `cloud_user_id` 条件：别人的规则一律按不存在处理（404），
  不泄露 id 是否存在；
- `list_specs` 只返回纯匹配视图，供 evaluator 做纯函数匹配。

**不写种子数据**：空表就是零审批。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.core.approval_rules.evaluator import RuleSpec, rule_spec_from_row
from flux.core.approval_rules.rules import (
    RULE_TYPES,
    default_description,
    normalize_rule_params,
)
from flux.core.event.bus import EventBus, Events
from flux.errors import BadRequestError, NotFoundError
from flux.logging import get_logger
from flux.models.approval_rule import ApprovalRule

logger = get_logger(__name__)

__all__ = ["ApprovalRuleService"]

#: 描述长度上限：足够写清一条策略，又不会把审批卡撑爆。
MAX_DESCRIPTION_CHARS = 500
#: 规则创建者取值。
CREATED_BY_KINDS = frozenset({"user", "bot"})


def _normalize_user(cloud_user_id: object) -> str:
    text = str(cloud_user_id).strip() if cloud_user_id is not None else ""
    if not text:
        raise BadRequestError(
            "审批规则必须归属于一个 Flux 云用户",
            details={"field": "cloud_user_id"},
        )
    return text


def _coerce_rule_id(value: object) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise NotFoundError(f"审批规则 {value} 不存在", details={"rule_id": str(value)}) from exc


def _event_payload(row: ApprovalRule) -> dict[str, Any]:
    """事件载荷：只放定位与展示所需字段，不含参数原文。"""
    return {
        "id": str(row.id),
        "cloud_user_id": row.cloud_user_id,
        "type": row.type,
        "enabled": bool(row.enabled),
        "created_by_kind": row.created_by_kind,
        "created_by_bot_id": (
            str(row.created_by_bot_id) if row.created_by_bot_id is not None else None
        ),
    }


class ApprovalRuleService:
    """`approval_rules` 表的唯一读写入口。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Any | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._bus = bus

    async def _publish(self, event: str, payload: dict[str, Any]) -> None:
        if self._bus is None:
            return
        await self._bus.publish(event, payload)

    async def create(
        self,
        *,
        cloud_user_id: str,
        rule_type: str,
        params: Any,
        description: str | None = None,
        enabled: bool = True,
        created_by_kind: str = "user",
        created_by_bot_id: uuid.UUID | str | None = None,
    ) -> ApprovalRule:
        """新增一条规则；参数非法 / 归属非法一律 400。"""
        user = _normalize_user(cloud_user_id)
        if created_by_kind not in CREATED_BY_KINDS:
            raise BadRequestError(
                "created_by_kind 只能是 user 或 bot",
                details={"field": "created_by_kind"},
            )
        bot_id: uuid.UUID | None = None
        if created_by_kind == "bot":
            if created_by_bot_id is None:
                raise BadRequestError(
                    "Bot 添加规则时必须记录 created_by_bot_id",
                    details={"field": "created_by_bot_id"},
                )
            bot_id = _coerce_rule_id(created_by_bot_id)
        elif created_by_bot_id is not None:
            raise BadRequestError(
                "用户创建的规则不应带 created_by_bot_id",
                details={"field": "created_by_bot_id"},
            )

        normalized = normalize_rule_params(rule_type, params)
        text = self._description(description, rule_type, normalized)
        row = ApprovalRule(
            id=uuid.uuid4(),
            cloud_user_id=user,
            type=rule_type,
            params=normalized,
            description=text,
            enabled=bool(enabled),
            created_by_kind=created_by_kind,
            created_by_bot_id=bot_id,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        logger.info(
            "approval_rule.created id=%s user=%s type=%s enabled=%s by=%s bot=%s",
            row.id,
            row.cloud_user_id,
            row.type,
            row.enabled,
            row.created_by_kind,
            row.created_by_bot_id,
        )
        await self._publish(Events.APPROVAL_RULE_CREATED, _event_payload(row))
        return row

    def _description(self, description: str | None, rule_type: str, params: dict[str, Any]) -> str:
        if description is not None and str(description).strip():
            text = str(description).strip()
            if len(text) > MAX_DESCRIPTION_CHARS:
                raise BadRequestError(
                    f"description 最多 {MAX_DESCRIPTION_CHARS} 个字符",
                    details={"field": "description"},
                )
            return text
        return default_description(rule_type, params)

    async def get(self, rule_id: object, *, cloud_user_id: str) -> ApprovalRule:
        """按 id + 当前用户取一条；别人的规则按不存在处理（404）。"""
        user = _normalize_user(cloud_user_id)
        pk = _coerce_rule_id(rule_id)
        async with self._session_factory() as session:
            row = await session.get(ApprovalRule, pk)
            if row is None or row.cloud_user_id != user:
                raise NotFoundError(f"审批规则 {rule_id} 不存在", details={"rule_id": str(rule_id)})
            return row

    async def list(self, cloud_user_id: str, *, enabled_only: bool = False) -> list[ApprovalRule]:
        """列出该用户的规则（默认含停用项，设置页需要看到全部）。"""
        user = _normalize_user(cloud_user_id)
        statement = select(ApprovalRule).where(ApprovalRule.cloud_user_id == user)
        if enabled_only:
            statement = statement.where(ApprovalRule.enabled.is_(True))
        statement = statement.order_by(ApprovalRule.created_at.asc(), ApprovalRule.id.asc())
        async with self._session_factory() as session:
            rows = await session.scalars(statement)
            return list(rows)

    async def list_specs(self, cloud_user_id: str, *, enabled_only: bool = True) -> list[RuleSpec]:
        """给 evaluator 的纯匹配视图；只含该用户的规则。"""
        rows = await self.list(cloud_user_id, enabled_only=enabled_only)
        return [rule_spec_from_row(row) for row in rows]

    async def update(
        self,
        rule_id: object,
        *,
        cloud_user_id: str,
        rule_type: str | None = None,
        params: Any | None = None,
        description: str | None = None,
        enabled: bool | None = None,
    ) -> ApprovalRule:
        """局部更新并重新校验参数；跨用户按不存在处理（404）。"""
        user = _normalize_user(cloud_user_id)
        pk = _coerce_rule_id(rule_id)
        async with self._session_factory() as session:
            row = await session.get(ApprovalRule, pk)
            if row is None or row.cloud_user_id != user:
                raise NotFoundError(f"审批规则 {rule_id} 不存在", details={"rule_id": str(rule_id)})

            effective_type = rule_type if rule_type is not None else row.type
            if effective_type not in RULE_TYPES:
                raise BadRequestError(
                    f"未知的审批规则类型：{effective_type}；可选值：{'、'.join(RULE_TYPES)}",
                    details={"field": "type"},
                )
            effective_params = params if params is not None else row.params
            normalized = normalize_rule_params(effective_type, effective_params)

            row.type = effective_type
            row.params = normalized
            if description is not None:
                row.description = self._description(description, effective_type, normalized)
            if enabled is not None:
                row.enabled = bool(enabled)
            await session.commit()
            await session.refresh(row)

        logger.info(
            "approval_rule.updated id=%s user=%s type=%s enabled=%s",
            row.id,
            row.cloud_user_id,
            row.type,
            row.enabled,
        )
        await self._publish(Events.APPROVAL_RULE_UPDATED, _event_payload(row))
        return row

    async def delete(self, rule_id: object, *, cloud_user_id: str) -> dict[str, Any]:
        """删除自己的规则；跨用户按不存在处理（404）。返回删除前的安全快照。"""
        user = _normalize_user(cloud_user_id)
        pk = _coerce_rule_id(rule_id)
        async with self._session_factory() as session:
            row = await session.get(ApprovalRule, pk)
            if row is None or row.cloud_user_id != user:
                raise NotFoundError(f"审批规则 {rule_id} 不存在", details={"rule_id": str(rule_id)})
            payload = _event_payload(row)
            await session.delete(row)
            await session.commit()
        logger.info(
            "approval_rule.deleted id=%s user=%s type=%s",
            payload["id"],
            payload["cloud_user_id"],
            payload["type"],
        )
        await self._publish(Events.APPROVAL_RULE_DELETED, payload)
        return payload
