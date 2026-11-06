"""apply_batches 表：Apply 事务日志（P0-1 崩溃恢复；P1-2 整批回滚的分组依据）。

一次 apply_many 调用 = 一行记录，且必须在**任何磁盘操作之前**落库：服务被杀后，
重启只能凭这行判断"这批动到哪一步、哪些文件可能要还原"，从而不留无法解释的半应用状态。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ApplyBatch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一次 Apply（apply_many 调用）的事务日志。"""

    __tablename__ = "apply_batches"

    # 取值见 flux.enums.ApplyBatchStatus
    status: Mapped[str] = mapped_column(String(16), index=True, default="in_progress")
    # 本批包含的 virtual_changes.id（有序）；恢复与回滚都按这个列表逐条对账
    change_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 进度标记 prepared → backed_up → writing → verifying → testing（见 apply_engine 常量）
    phase: Mapped[str] = mapped_column(String(16), default="prepared")
    # 本批备份的相对根（.flux/backups）；每条 change 的备份在 <backup_root>/<change_id>/<相对路径>
    backup_root: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 失败原因（测试不过 / 写入失败 / 预检拒绝）
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 崩溃恢复的过程说明（还原了哪些、哪条需要人工处理）
    recovery_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "status": self.status,
            "change_ids": list(self.change_ids or []),
            "phase": self.phase,
            "backup_root": self.backup_root,
            "error": self.error,
            "recovery_note": self.recovery_note,
            "created_at": self.created_at.isoformat() if self.created_at is not None else None,
            "finished_at": (self.finished_at.isoformat() if self.finished_at is not None else None),
        }
