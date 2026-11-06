"""Connector 持久化模型（主规格 §8.5 执行日志 / §11.2）。"""

from __future__ import annotations

import uuid

from sqlalchemy import JSON, Float, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ConnectorConfig(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """已注册 Connector 的配置（表名 connectors，见主规格 §11.2）。"""

    __tablename__ = "connectors"

    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    configuration: Mapped[dict] = mapped_column(JSON, default=dict)


class ConnectorLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """每次 Connector 执行必须落一条日志（主规格 §8.5）。"""

    __tablename__ = "connector_logs"

    connector_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("connectors.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), index=True, nullable=True
    )
    action: Mapped[str] = mapped_column(String(128), index=True)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    cost: Mapped[float | None] = mapped_column(Float, nullable=True)
