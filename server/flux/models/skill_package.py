"""技能库：技能包与 Bot 分配。

技能库与 `imported_capabilities`（扫描本机 SKILL.md 后登记的能力清单）是两条
不同的链路：

- `skill_packages`：技能正文存在库里，可由 API 创建 / 编辑，`spec` 保存与
  FluxSkill 对齐的元数据，`body` 保存真正注入对话上下文的指令正文；
- `bot_skills`：Bot 与技能包的多对多分配，`position` 决定注入顺序。

删除技能包时不能静默带走分配记录：`bot_skills.skill_id` 用 RESTRICT，
服务层会先提示「仍有 Bot 在用」，要求先解除分配再删除。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class SkillPackage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一个可复用的技能包（元数据 + Markdown 指令正文）。"""

    __tablename__ = "skill_packages"
    __table_args__ = (
        UniqueConstraint("name", name="uq_skill_packages_name"),
        Index("ix_skill_packages_enabled", "enabled"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 与 FluxSkill 对齐的元数据（source 固定为 skill-library，不含正文）
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: Markdown 指令正文；注入上下文时只取这一段
    body: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")

    def to_dict(self) -> dict[str, Any]:
        """详情形状：包含正文，供单条读取 / 创建 / 编辑接口使用。"""
        return {
            "id": str(self.id),
            "name": self.name,
            "description": self.description or "",
            "version": self.version,
            "spec": dict(self.spec or {}),
            "body": self.body,
            "body_chars": len(self.body or ""),
            "enabled": bool(self.enabled),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }

    def to_summary(self) -> dict[str, Any]:
        """列表形状：不带正文，避免列表接口一次吐出大量文本。"""
        data = self.to_dict()
        data.pop("body", None)
        return data


class BotSkill(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一个 Bot 对某个技能包的分配（可单独停用、可排序）。"""

    __tablename__ = "bot_skills"
    __table_args__ = (
        UniqueConstraint("bot_id", "skill_id", name="uq_bot_skills_bot_skill"),
        Index("ix_bot_skills_bot", "bot_id", "position"),
    )

    bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bots.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    #: RESTRICT：技能包仍被分配时不允许直接删除，必须先解除分配
    skill_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("skill_packages.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    #: 注入顺序：越小越靠前
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_id": str(self.bot_id),
            "skill_id": str(self.skill_id),
            "enabled": bool(self.enabled),
            "position": int(self.position or 0),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
