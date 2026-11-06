"""Agent Run 执行轨迹的持久化模型。

`agent_runs` 只记生命周期状态，任务对话页此前只能看到「正在发生」的思考与命令，
Run 结束、刷新或换设备后轨迹就消失了。这张表把轨迹按 Run 落库：

- `kind`：thinking（思考段）/ tool（工具调用）/ error（错误行）；
- `seq`：Run 内顺序，查询按它升序还原时间线；
- `category`：工具分类（command / read / write / other），供「概览」统计命令数与文件数；
- `paths`：该次工具调用触及的文件路径，用于按文件去重；
- `text` / `detail` / `output`：思考正文、命令行或参数、命令输出。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import JSON, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RunTraceItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一条执行轨迹项（一个思考段 / 一次工具调用 / 一条错误）。"""

    __tablename__ = "run_trace_items"

    run_id: Mapped[str] = mapped_column(String(64), index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), index=True, nullable=True)
    #: Run 内的展示顺序（从 1 开始）
    seq: Mapped[int] = mapped_column(Integer, default=0)
    #: thinking / tool / error
    kind: Mapped[str] = mapped_column(String(16), index=True)
    #: 工具分类：command / read / write / other；思考与错误行留空
    category: Mapped[str] = mapped_column(String(16), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    name: Mapped[str] = mapped_column(String(120), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    output: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="")
    call_id: Mapped[str] = mapped_column(String(160), default="")
    paths: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "run_id": self.run_id,
            "task_id": str(self.task_id) if self.task_id else None,
            "kind": self.kind,
            "category": self.category,
            "text": self.text,
            "name": self.name,
            "detail": self.detail,
            "output": self.output,
            "status": self.status,
            "call_id": self.call_id,
            "paths": list(self.paths or []),
        }
