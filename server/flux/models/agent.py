"""Agent 持久化模型（主规格 §11.2）。

注意：这里的 ORM 实体与运行时的领域对象（flux.core.agent_runtime.manager.AgentSpec /
AgentHandle）是两回事——ORM 负责持久化，运行时对象只承载档案与权限边界。
"""

from __future__ import annotations

import uuid

from sqlalchemy import JSON, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Agent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agents"

    name: Mapped[str] = mapped_column(String(128), index=True)
    # 取值见 flux.enums.AgentRole
    role: Mapped[str] = mapped_column(String(32), index=True)
    model_provider: Mapped[str] = mapped_column(String(32))
    model_name: Mapped[str] = mapped_column(String(128))
    # 运行时状态，取值见 flux.enums.AgentState
    status: Mapped[str] = mapped_column(String(32), index=True, default="CREATED")
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class AgentSkill(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Agent ↔ Skill 关联。

    skill_id 暂为字符串键（Skill 表随 M5 Skill System 引入，主规格 §9）。
    """

    __tablename__ = "agent_skills"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    skill_id: Mapped[str] = mapped_column(String(128), index=True)


class AgentExecutionLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Agent 执行日志（主规格 §11.2；原 agent_logs 与 agent_execution_logs 已合并为一张表）。"""

    __tablename__ = "agent_execution_logs"

    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), index=True, nullable=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), index=True, nullable=True
    )
    event: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
