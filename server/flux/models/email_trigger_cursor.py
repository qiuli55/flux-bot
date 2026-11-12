"""Email 入站触发的轮询游标。

一行 = 某个本地账号在某个邮箱目录上的「已经处理到哪封邮件」。轮询器每轮读取
邮件头后把最大的 UID 记在这里，进程重启（甚至换实例）后仍然不会重复发布
`email.received`。进程内集合在重启后必然丢失，所以游标必须落库。

这里只记去重与观测需要的元数据：UID、最近一封邮件 id、最近轮询时间与结果。
绝不存邮件正文、邮件头全文或连接器密码。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class EmailTriggerCursor(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "email_trigger_cursors"
    __table_args__ = (
        UniqueConstraint(
            "account_key",
            "mailbox",
            name="uq_email_trigger_cursors_account_mailbox",
        ),
    )

    account_key: Mapped[str] = mapped_column(String(64), index=True)
    mailbox: Mapped[str] = mapped_column(String(255))
    #: 已处理的最大 IMAP UID；0 表示还没有成功处理过任何邮件。
    #: IMAP UID 是 32 位无符号整数，用 BigInteger 避免 PostgreSQL int4 溢出。
    last_uid: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    #: 最近一封新邮件的连接器 id（IMAP UID 的字符串形式），仅用于观测。
    last_message_id: Mapped[str] = mapped_column(String(255), default="", server_default="")
    #: 最近一次轮询时间；未轮询过为 NULL。
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: ok / error；给「上次检查状态」这类只读展示用。
    last_status: Mapped[str] = mapped_column(String(16), default="", server_default="")
    #: 最近一次失败的简短原因（连接器已做脱敏，不含密码与服务器原文）。
    last_error: Mapped[str] = mapped_column(String(255), default="", server_default="")


__all__ = ["EmailTriggerCursor"]
