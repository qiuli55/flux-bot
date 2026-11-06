"""任务工作记忆卡的持久化模型。

执行 Agent 每轮结束由平台沉淀一张短卡（本轮目标、结果、已改文件、失败原因），
下一轮开工时平台把它注入指令——不依赖用户手动点「前情摘要」，也不污染聊天记录。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TaskMemoryCard(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一个任务在某轮结束后的工作记忆卡（一轮一张，按 run_id 幂等）。"""

    __tablename__ = "task_memory_cards"

    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[str] = mapped_column(Text, default="")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": str(self.task_id),
            "run_id": self.run_id,
            "content": self.content,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
