"""全局审批规则（按 Flux 云用户隔离）。

产品约定（2026-11-14）：**默认什么都不需要审批**。用户在 App 设置里添加规则
（例如「支付超过 500 元要审批」「执行 rm 开头的命令要审批」），规则对该用户的
**所有 Bot** 生效。Bot 也可以给自己加更严格的规则，但只能新增，不能删除、
停用或放宽——规则只会让审批更多，永远不会直接授予权限。

`created_by_bot_id` 只是「这条规则是谁提的」的归属信息，**不是作用域**：
规则永远按 `cloud_user_id` 对全部 Bot 生效，避免出现只约束单个 Bot 的隐式行为。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class ApprovalRule(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一条用户级审批规则；空表 = 零审批。"""

    __tablename__ = "approval_rules"

    #: 规则所属的 Flux 云用户（云 JWT 的 sub）；规则对该用户的所有 Bot 生效。
    cloud_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: 取值见 flux.core.approval_rules.rules.RULE_TYPES。
    type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    #: 已校验 / 归一化的参数（JSON）。不同 type 的结构见 rules.py。
    params: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: 展示给用户的说明；创建时未提供则由 type + params 生成中文默认值。
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 停用的规则不参与匹配，但仍会出现在设置页（App 需要展示全部规则）。
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1", index=True
    )
    #: user / bot：是谁添加了这条规则。Bot 只能新增，不能改/删/停用。
    created_by_kind: Mapped[str] = mapped_column(
        String(8), nullable=False, default="user", server_default="user"
    )
    #: created_by_kind=bot 时记录是哪个 Bot 添加的；仅用于展示与审计，不是作用域。
    created_by_bot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "cloud_user_id": self.cloud_user_id,
            "type": self.type,
            "params": dict(self.params or {}),
            "description": self.description,
            "enabled": bool(self.enabled),
            "created_by_kind": self.created_by_kind,
            "created_by_bot_id": (
                str(self.created_by_bot_id) if self.created_by_bot_id is not None else None
            ),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


__all__ = ["ApprovalRule"]
