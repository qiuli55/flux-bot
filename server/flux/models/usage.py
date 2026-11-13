"""用量与成本记录（主规格 §11.2 / §15.4）。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class UsageRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """单步模型调用的用量明细（计费账本）。

    `cost` 为 None 表示该供应商未配置计价——**不写入猜测值**（主规格 §15.4）。
    `plan_key` 是套餐归因（如 `opencode-go` / `agent-plan`），窗口聚合以它为准；
    `occurred_at` 是增量落账时刻，5h / 7d / 自然月窗口都按它切片。
    """

    __tablename__ = "usage_records"

    provider: Mapped[str] = mapped_column(String(32), index=True)
    model: Mapped[str] = mapped_column(String(128), index=True)
    #: 套餐标识（见 flux.core.billing.plans）；NULL 表示未识别，只记账不参与闸门
    plan_key: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    #: 账号指纹（同一套餐可能有多个订阅账号，必须分开聚合；空 = 未识别）
    #: 注意：这是**凭据指纹**（sha256[:8]），不是人；按人隔离用 cloud_user_id。
    account_key: Mapped[str] = mapped_column(String(16), index=True, default="", server_default="")
    #: 产生这条用量的 Flux 云用户 id（Phase 2 按人隔离）；历史 NULL/空串归主用户
    cloud_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: 产生该明细的 Run（agent_runs.id），用于按 run 回放
    run_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), index=True, nullable=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), index=True, nullable=True
    )
    #: 用量发生时刻（窗口聚合依据；创建时即落库，不靠 created_at 兜底）
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, server_default=func.now(), nullable=False
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "provider": self.provider,
            "model": self.model,
            "plan_key": self.plan_key,
            "account_key": self.account_key,
            "cloud_user_id": self.cloud_user_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
            "cost": self.cost,
            "run_id": self.run_id,
            "task_id": str(self.task_id) if self.task_id else None,
            "agent_id": str(self.agent_id) if self.agent_id else None,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
        }


class UsageSnapshot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """官方额度快照（对账来源）。

    官方接口只说"总量用了多少"，没有任务归因；这张表负责把总量钉在时间轴上，
    与本地账本的增量做对比，判断 `step-finish.cost` 是否可信。
    """

    __tablename__ = "usage_snapshots"

    plan_key: Mapped[str] = mapped_column(String(32), index=True)
    #: 账号指纹（同一套餐多账号必须分开存，否则窗口互相覆盖）；不是人
    account_key: Mapped[str] = mapped_column(String(16), index=True, default="", server_default="")
    #: 采集/记账时的 Flux 云用户 id；历史 NULL/空串归主用户
    cloud_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: 官方窗口标识：rolling_5h / weekly / monthly / five_hour / daily
    window_key: Mapped[str] = mapped_column(String(24), index=True)
    used: Mapped[float | None] = mapped_column(Float, nullable=True)
    quota: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: 官方给出的已用百分比（0-100）；只有官方没给百分比时才用 used/quota 反算
    percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: 额度单位：usd / afp / percent
    unit: Mapped[str] = mapped_column(String(16), default="percent")
    #: ok / warning / exceeded
    status: Mapped[str] = mapped_column(String(16), default="ok")
    resets_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: 采集来源：opencode_go_usage / arkcli / manual
    source: Mapped[str] = mapped_column(String(32), default="")
    #: 采集失败原因（成功为 NULL）
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_key": self.plan_key,
            "account_key": self.account_key,
            "cloud_user_id": self.cloud_user_id,
            "window_key": self.window_key,
            "used": self.used,
            "quota": self.quota,
            "percent": self.percent,
            "unit": self.unit,
            "status": self.status,
            "resets_at": self.resets_at.isoformat() if self.resets_at else None,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "source": self.source,
            "error": self.error,
        }
