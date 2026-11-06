"""Virtual Workspace 持久化模型（主规格 §7.3 Change Object / §11.2）。

提案的权威存储在 virtual_changes 表：进程重启后人工审核队列不能丢，
且 Apply 必须拿库里的 original_hash 与磁盘现状比对（实施计划 §5 关键规则）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class VirtualChange(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """AI 改动的虚拟提案。落盘与否由审查状态决定（主规格 §7）。"""

    __tablename__ = "virtual_changes"

    # 提案可以脱离项目存在（本地临时目录也能跑闭环），故 project_id 可空
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    # 产生该提案的任务；tasks 表已存在但本轮不设外键（与 tasks.agent_id 同一处理，
    # 见附录 A 裁决 A21）
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), index=True, nullable=True)
    # 同一次 CodeChangeSet 落成的多条提案共享一个 group_id：审核/落盘可按整组进行（P0-02）
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), index=True, nullable=True
    )
    file_path: Mapped[str] = mapped_column(String(512), index=True)
    # 动作类型（取值见 flux.enums.ChangeKind）：create / modify / delete（P1-1）
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="modify", default="modify"
    )
    # 生成提案时该文件的 sha256（十六进制）；Apply 前必须与磁盘现状复验（实施计划 §5）
    original_hash: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    original_content: Mapped[str] = mapped_column(Text)
    # delete 类提案没有"改动后内容"，为 NULL；create / modify 必为完整文件内容
    proposed_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Diff 概览，供 UI 列表直接展示，避免每次列表都重算 diff
    added_lines: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    removed_lines: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    hunks: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # 为什么这样改（来自 agent 经 MCP 提交的提案 reason / summary，主规格 §6 的"修改原因"）
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 产出该提案的 Agent 标识
    agent_source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 取值见 flux.enums.VirtualChangeStatus
    status: Mapped[str] = mapped_column(String(32), index=True, default="pending")
    # Apply 时原文件的备份路径（新建文件无备份，留空）
    backup_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Apply 失败的完整错误；成功时为空
    apply_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 审核截止时间（UTC）：为空表示这条提案不会因超时失效（P0-02 的 TTL）
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )
    # 失效原因（超时 / 被新提案取代），供 UI 与审计解释"为什么这条不能再审"
    expired_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 崩溃恢复挂起项的人工决策结果（取值见 flux.enums.RecoveryResolution）；为空 = 待决策。
    # 恢复遇到"内容既非原文也非提案内容 / 文件被外部删除"时不再自动处理，改为挂起等人选择
    recovery_resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id) if self.project_id is not None else None,
            "task_id": str(self.task_id) if self.task_id is not None else None,
            "group_id": str(self.group_id) if self.group_id is not None else None,
            "file_path": self.file_path,
            "kind": self.kind,
            "original_hash": self.original_hash,
            "original_content": self.original_content,
            "proposed_content": self.proposed_content,
            "diff": self.diff or "",
            "added_lines": self.added_lines,
            "removed_lines": self.removed_lines,
            "hunks": self.hunks,
            "reason": self.reason,
            "summary": self.summary,
            "agent_source": self.agent_source,
            "status": self.status,
            "backup_path": self.backup_path,
            "apply_error": self.apply_error,
            "expires_at": self.expires_at.isoformat() if self.expires_at is not None else None,
            "expired_reason": self.expired_reason,
            "recovery_resolution": self.recovery_resolution,
        }
