"""Terminal Session 持久化模型（Agent Terminal Console §6 / §8）。

终端事件按会话内单调递增的 seq 落库：前端按 seq 续读即可恢复历史，SSE 断线重连也只需
带上 last event id。会话与事件都是**观察数据**，按 §9 不参与 Agent Context 回灌。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from flux.enums import TerminalSessionKind
from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TerminalSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一个终端会话：主窗口与终端窗口连的是同一个它，不重开第二个 Agent（§6）。"""

    __tablename__ = "terminal_sessions"

    # 关联的 Run：Agent 经 `terminal.execute` 建会话时由 MCP 层带上（来自 X-Flux-Run-Id，
    # 服务端已校验该 Run 归属本 Agent）；Run 终态时据此定向回收会话，避免命令进程泄漏。
    # 人审（用户）直接开的会话无 Run，为 NULL。
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), index=True, nullable=True)
    # 正在执行的命令进程组 id（POSIX pgid；Windows 为组长 pid）。命令结束即清空。
    # 持久化它的意义：Flux 被 SIGKILL 后重启时，DB 里还留着这个 id，能据此识别并回收
    # 遗留的孤儿命令进程（否则会话被标 closed 但 OS 进程永远活着）。
    cmd_pgid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 会话执行命令的工作目录：创建时解析并固定，之后不再变（工作区根）
    workspace_root: Mapped[str] = mapped_column(String(1024))
    # 会话类型：持久化区分 Agent Terminal 与 Human Terminal，避免两个运行时互相接管。
    kind: Mapped[str] = mapped_column(
        String(16),
        default=TerminalSessionKind.AGENT.value,
        server_default=TerminalSessionKind.AGENT.value,
    )
    # 取值见 flux.enums.TerminalSessionStatus
    status: Mapped[str] = mapped_column(String(16), index=True, default="active")
    # 下一条事件的 seq（会话内单调递增，从 1 开始）
    next_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "run_id": str(self.run_id) if self.run_id is not None else None,
            "workspace_root": self.workspace_root,
            "kind": self.kind,
            "status": self.status,
            "next_seq": self.next_seq,
            "created_at": self.created_at.isoformat() if self.created_at is not None else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at is not None else None,
        }


class TerminalEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一条终端事件（§8）：命令来源区分 AI / USER，避免人工接管与 Agent 行为混淆（§5）。"""

    __tablename__ = "terminal_events"

    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("terminal_sessions.id", ondelete="CASCADE"),
        index=True,
    )
    # 会话内单调递增；前端按它续读历史 / 重连
    seq: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # 取值见 flux.enums.TerminalEventKind
    kind: Mapped[str] = mapped_column(String(32), index=True)
    # 取值见 flux.enums.TerminalSource
    source: Mapped[str] = mapped_column(String(8))
    # 命令内容（command.started / stop.requested 这类事件才有）
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 输出片段（terminal.output）
    chunk: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 命令结束时的退出码
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "session_id": str(self.session_id),
            "seq": self.seq,
            "kind": self.kind,
            "source": self.source,
            "command": self.command,
            "chunk": self.chunk,
            "exit_code": self.exit_code,
            "created_at": self.created_at.isoformat() if self.created_at is not None else None,
        }
