"""任务模型（主规格 §11.2 / §5.2）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from flux.core.components.mapping import component_payload, public_payload
from flux.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Task(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tasks"

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True
    )
    description: Mapped[str] = mapped_column(Text)
    # 取值见 flux.enums.TaskStatus
    status: Mapped[str] = mapped_column(String(32), index=True, default="pending")
    # 累计成本；未配置供应商计价时为 None（不臆造价格，主规格 §15.4）
    cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 调度优先级：数字越小越优先（主规格 §5.1 调度依据）。本列不在 §11.2 原始定义内，
    # 属附录 A 裁决 A20 的增列：API 已暴露 priority，不落库会导致重启后取值不一致。
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default="100"
    )
    # 执行该任务的 Agent。附录 A 裁决 A21：暂不设外键——M1 的 agents 表尚无写入路径，
    # 加 FK 会让带 agent_id 的建任务在 PostgreSQL 上必然失败；待 Agent 持久化落地后补 FK。
    agent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # 任务级决策策略（文档 §5）：auto = Agent 按默认方案自行拍板，manual = 遇决策点暂停等用户。
    # 取值见 flux.enums.DecisionMode；列级默认与 DecisionMode.AUTO 一致。
    decision_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="auto", server_default="auto"
    )
    # 对话级权限模式（2026-10-25）：plan = 只读+计划文档 / normal = 高风险命令审批 /
    # full = 跳过逐步审批（受保护对象仍逐次问）。取值见 flux.enums.PermissionMode。
    # 切换只影响后续消息：Run 启动时把当前值快照进 agent_runs.permission_mode。
    permission_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="normal", server_default="normal"
    )
    # 需求确认六维度（文档 §4 P0-06）。结构 {"items": [{label, value}], "actor": ...}，
    # actor 取 assistant / user；用户改过之后以最后一次写入为准，执行时用的就是最终版本。
    confirmation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 决策点历史（文档 §5）：一个列表，每个元素是一个决策点记录；status=pending 的那条是"正等人选"。
    decisions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 该任务触发的 Agent Run（P0-05）。Run 结束时靠它把终态回写到任务上。
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 任务级模型覆盖（移动端对话页切换 Agent 的模型）；None = 用该 Agent runtime 的默认模型。
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 任务级模型来自哪条 key 库条目；None = 原生模型（不需要 key 库凭据）。
    # 与 model 一起构成完整选择；切换 Agent 时两者同时清空。
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    # 排队切换的目标 Agent（2026-10-27）：任务执行中切换 Agent 时只记在这里，
    # 等本轮 Run 真正收尾后再应用，避免中途换人把当前进度交给另一个 Agent。
    pending_agent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # 对话来源（2026-11-03）：chat = 首页普通对话；assistant = 助理页的助理对话。
    # 助理对话是真实任务（能跑 Agent、选模型），但不进首页对话列表。
    # 取值见 flux.enums.TaskOrigin；旧数据由 server_default 补成 chat。
    origin: Mapped[str] = mapped_column(
        String(16), nullable=False, default="chat", server_default="chat", index=True
    )
    # Bot 档案（2026-11-04）：每条对话都视为一个 bot，群聊里用它展示身份。
    # 头像不允许上传图片，只由「形状 × 颜色」组合而成（取值见 flux.core.group.avatar）。
    bot_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    avatar_shape: Mapped[str] = mapped_column(
        String(32), nullable=False, default="circle", server_default="circle"
    )
    avatar_color: Mapped[str] = mapped_column(
        String(32), nullable=False, default="violet", server_default="violet"
    )
    # Bot 模式（2026-11-05）：这条任务由哪个常驻 Bot 开工。为空 = 普通对话/助理任务，
    # 也因此不进首页对话列表（见 TaskRepository.list 的 exclude_bot_tasks）。
    bot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bots.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 后台子任务的父对话任务（通常是 bots.chat_task_id）。普通对话/开工任务为 NULL。
    # 子任务自己的状态与结果通过父任务的 kind=subtask 消息对外可见，父任务被清理时
    # 只把关联置空，子任务历史仍可查询（不级联删除执行记录）。
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tasks.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    # 本轮执行的工作目录快照。Bot 每次开工时把它的独立目录写进来，
    # CLI runtime 据此把子进程 cwd 放到 bots/<id>/workspace，而不是全局工作区。
    workspace: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 这一轮代表哪个 Flux 云用户（Phase 3）。例程开工时从 bot_routines 带下来，
    # Agent 执行期的记忆 / 连接器按它取人；NULL = 旧任务，按主用户兜底。
    cloud_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    def pending_decision(self) -> dict | None:
        """当前正等用户选择的那条决策点记录；没有则返回 None。"""
        for record in reversed(self.decisions or []):
            if isinstance(record, dict) and record.get("status") == "pending":
                return record
        return None

    def to_dict(self) -> dict[str, Any]:
        """任务的标准响应体：M0 冻结的 7 个键 + created_at + Solo 生命周期字段。

        created_at / decision_mode / confirmation / decisions / pending_decision / run_id
        都是后续增列，属向后兼容的加字段——调用方原有字段不变。cost 仍不外露（未配置计价时为 None）。
        """
        return {
            "id": str(self.id),
            "description": self.description,
            "status": self.status,
            "priority": self.priority,
            "agent_id": str(self.agent_id) if self.agent_id is not None else None,
            "project_id": str(self.project_id) if self.project_id is not None else None,
            "result": self.result,
            "created_at": self.created_at.isoformat() if self.created_at is not None else None,
            "decision_mode": self.decision_mode,
            "permission_mode": self.permission_mode or "normal",
            "confirmation": self.confirmation,
            "decisions": list(self.decisions or []),
            "pending_decision": self.pending_decision(),
            "run_id": self.run_id,
            "model": self.model,
            "api_key_id": str(self.api_key_id) if self.api_key_id is not None else None,
            "pending_agent_id": (
                str(self.pending_agent_id) if self.pending_agent_id is not None else None
            ),
            "origin": self.origin or "chat",
            "bot_name": self.bot_name,
            "avatar_shape": self.avatar_shape or "circle",
            "avatar_color": self.avatar_color or "violet",
            "bot_id": str(self.bot_id) if self.bot_id is not None else None,
            "parent_task_id": (
                str(self.parent_task_id) if self.parent_task_id is not None else None
            ),
            "cloud_user_id": self.cloud_user_id,
        }


class TaskMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """任务对话消息（Solo 任务执行中心的聊天记录）。

    落库而不是留在前端内存：刷新页面、后端重启后聊天记录都不丢。
    `seq` 是同一任务内的单调序号（从 1 起），向上懒加载更早消息时用它做游标——
    不依赖 created_at（SQLite 的时间戳只到秒，同一秒内的多条消息无法排序）。
    """

    __tablename__ = "task_messages"

    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # 说话方：user / assistant（取值见 flux.enums.TaskMessageRole）
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # 消息类型：本增量只有 text；agent 事件类消息（执行步骤、状态变更）待真实事件源接入后扩展
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="text", server_default="text"
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 结构化附加数据：澄清结论、执行计划步骤、模型调用元信息；无附加数据时为 NULL
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 上下文云同步游标（2026-11-02）：NULL = 尚未处理；有值 = 已上传，或命中密钥红线
    # 被主动跳过（都表示这条不用再传）。只对可上传类型排队，见 flux.cloud.context_sync。
    cloud_uploaded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    def to_dict(self) -> dict[str, Any]:
        # 既有类型在读取时附带统一组件树（payload["component"]），原 kind / content /
        # payload 字段保持不变；kind="component" 的消息原样返回。
        enriched = component_payload(self.kind, self.content, self.payload, str(self.id))
        payload = public_payload(enriched if enriched is not None else self.payload)
        return {
            "id": str(self.id),
            "task_id": str(self.task_id),
            "seq": self.seq,
            "role": self.role,
            "kind": self.kind,
            "content": self.content,
            "payload": payload,
            "created_at": self.created_at.isoformat() if self.created_at is not None else None,
        }
