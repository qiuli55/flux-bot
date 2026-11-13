"""Bot 产出的可分享制品页面（artifact page）。

一个页面 = 一份可长期访问的「标题 + 正文」，由 Bot（或代表 Bot 的用户）发布，
通过公开链接分享给没有本机登录态的人。分两张表：

- `artifact_pages`：页面主体与当前状态。`share_token` 是不可猜测的公开令牌，
  公共 GET 只按它寻址，不暴露自增 id；`current_revision` 指向最新版本号。
- `artifact_page_revisions`：只追加的版本历史。旧版本永不改写——每次保存
  正文 / 标题 / 内容类型都新增一行并把 `current_revision` 加一。

这与设计工作室（`/api/v1/designs`，Luma Studio 代理）不是一回事：那里的数据
仍在本机 Luma Studio，本表是 Flux 自己持久化、可直接对外分享的静态内容。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class ArtifactPage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一份可分享页面的当前状态。"""

    __tablename__ = "artifact_pages"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_artifact_pages_slug"),
        UniqueConstraint("share_token", name="uq_artifact_pages_share_token"),
        Index("ix_artifact_pages_status", "status"),
    )

    #: 产出这个页面的 Bot；Bot 被删除后页面保留，只把归属置空
    bot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bots.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    #: 管理面用的可读标识（小写字母 / 数字 / 连字符），与公开令牌分开
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 公开分享令牌（不可猜测，不经自增 id 暴露页面）
    share_token: Mapped[str] = mapped_column(String(64), nullable=False)
    #: draft / published / unpublished
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default="draft"
    )
    #: 当前版本号；没有正文的空页面为 0，正常创建即为 1
    current_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: text/markdown 或 text/html（写入前统一规范化）
    content_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="text/markdown", server_default="text/markdown"
    )

    def to_dict(self) -> dict[str, Any]:
        """列表 / 详情共用的页面元数据（正文由调用方按需附加）。"""
        return {
            "id": str(self.id),
            "bot_id": str(self.bot_id) if self.bot_id is not None else None,
            "title": self.title,
            "slug": self.slug,
            "share_token": self.share_token,
            "public_path": f"/api/v1/public/pages/{self.share_token}",
            "status": self.status,
            "current_revision": self.current_revision,
            "content_type": self.content_type,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class ArtifactPageRevision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一次保存产生的不可变版本。"""

    __tablename__ = "artifact_page_revisions"
    __table_args__ = (
        UniqueConstraint("page_id", "revision", name="uq_artifact_page_revisions_page_revision"),
    )

    page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("artifact_pages.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 这次保存由哪个 Bot 发起；Bot 删除后保留版本本身
    author_bot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bots.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    def body_bytes(self) -> int:
        return len(self.body.encode("utf-8"))

    def to_dict(self) -> dict[str, Any]:
        """版本的完整视图（含正文）。"""
        data = self.to_summary()
        data["body"] = self.body
        return data

    def to_summary(self) -> dict[str, Any]:
        """列表视图：不带正文，避免版本历史接口一次吐回几十份全文。"""
        return {
            "id": str(self.id),
            "page_id": str(self.page_id),
            "revision": self.revision,
            "title": self.title,
            "content_type": self.content_type,
            "author_bot_id": (str(self.author_bot_id) if self.author_bot_id is not None else None),
            "body_bytes": self.body_bytes(),
            "created_at": _iso(self.created_at),
        }
