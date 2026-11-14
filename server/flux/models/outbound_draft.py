"""统一出站消息草稿（所有频道共用）。

出站文本的真相源从进程内存搬到 SQL：进程重启、频道切换都不会丢草稿，
后续 WeCom / DingTalk / Email 只需复用同一张表与同一套审批流程。

状态机（单向，除 pending 外都不可再发送）：

- pending：刚生成，等用户审批；唯一可以进入 sending / denied / cancelled 的状态；
- sending：已被某次「允许」原子认领，发送进行中；不能再被认领；
- sent / denied / cancelled / failed：终态。终态一律不自动重试、不自动发送。

`body_hash` 是正文的 sha256，审批卡与日志只引用哈希，不打印正文。
`attachments` 保存出站图片 / 文件的元数据（upload_id / 文件名 / mime / 大小 / 类型），
文件本体仍在 attachment_store，发送时按 upload_id 还原。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class OutboundDraft(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一条待审批 / 已处理的出站消息草稿。"""

    __tablename__ = "outbound_drafts"

    #: 频道标识，如 feishu；留出 wecom / dingtalk / email 等取值。
    channel: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    #: 取值见 flux.enums.OutboundDraftStatus。
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending", index=True
    )
    #: 要发送的正文；失败后仍保留，避免草稿丢失。
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: 正文的 sha256（hex），日志与审批 meta 只放它。
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 出站附件清单（JSON 数组），每项含 upload_id / name / mime / size / kind。
    #: 只存 attachment_store 的 upload_id 与展示元数据，不存绝对路径与文件内容；
    #: 进程重启后按 upload_id 仍能还原文件。纯文本草稿为空数组。
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    #: 频道相关的投递目标，如飞书的 {"chat_id": ..., "message_id": ...}。
    dest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: 来源归属。不设外键：草稿是审计记录，任务/Bot 被清理后仍应可查。
    bot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    #: 审批记录 id（tasks.decisions 里的那条），创建后回填。
    decision_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: 这条回复代表哪个 Flux 云用户（绑定的 IM 发送者 / 该用户的例程）。
    #: NULL = Phase 3 之前的草稿，只属于配置的主用户。
    cloud_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: 失败原因（截断）。只放异常类型与短消息，不写正文、不回显任何令牌。
    error: Mapped[str | None] = mapped_column(String(256), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "channel": self.channel,
            "status": self.status,
            "body": self.body,
            "body_hash": self.body_hash,
            "attachments": list(self.attachments or []),
            "dest": dict(self.dest or {}),
            "bot_id": str(self.bot_id) if self.bot_id is not None else None,
            "task_id": str(self.task_id) if self.task_id is not None else None,
            "turn_id": str(self.turn_id) if self.turn_id is not None else None,
            "decision_id": self.decision_id,
            "cloud_user_id": self.cloud_user_id,
            "error": self.error,
            "sent_at": _iso(self.sent_at),
            "resolved_at": _iso(self.resolved_at),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


__all__ = ["OutboundDraft"]
