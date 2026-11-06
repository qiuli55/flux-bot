"""无感切换 Agent：执行中排队、本轮收尾后生效、生成交接卡。

用户在执行中选另一个 Agent 时，不能把正在跑的一轮中途交给别人：
- 有活动 Run：只记 `tasks.pending_agent_id`，当前 Agent 与本轮进度不变；
- 本轮真实收尾后：平台应用排队目标、清掉旧模型覆盖、落一张 `kind="handoff"`
  的交接卡（下一轮执行指令会带上它），并发 `agent_switched` 流事件。

切换失败（目标 Agent 不存在 / runtime 未接入）不自动换人、不做转移：
保留原 Agent，把真实原因抛给调用方；排队的切换失效时落一条说明消息。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from flux.core.agent_runtime.runtimes.base import CLI_RUNTIMES, DEFAULT_RUNTIME
from flux.core.context.handoff import build_handoff
from flux.core.task_engine.conversation_trace import build_dialogue_trace
from flux.core.task_engine.repository import MessageDraft
from flux.enums import AgentInstallStatus, TaskMessageRole, TaskStatus
from flux.errors import ConflictError, NotFoundError
from flux.logging import get_logger
from flux.models.task import Task

if TYPE_CHECKING:
    from flux.container import Container

logger = get_logger(__name__)

#: 交接卡正文上限（下一轮会作为对话轨迹的一部分进指令，不能无限长）
HANDOFF_CARD_CHARS = 2400
#: 任务描述在交接卡里的截断长度
HANDOFF_TASK_CHARS = 300
#: 九段式交接文档的截断长度（事实脊柱优先，目录行可省）
HANDOFF_DOC_CHARS = 1400
#: 交接卡里带多少条最近对话
HANDOFF_TRACE_MESSAGES = 6
#: 交接卡里最近对话的字符预算
HANDOFF_TRACE_CHARS = 900

_ACTIVE_STATUSES = frozenset({TaskStatus.RUNNING.value, TaskStatus.WAITING_FOR_USER_DECISION.value})


@dataclass(slots=True)
class AgentTarget:
    """一次切换目标的事实（预检结果）。"""

    agent_id: uuid.UUID
    name: str
    runtime: str


@dataclass(slots=True)
class SwitchResult:
    """切换结果：`queued=True` 表示本轮结束后才生效。"""

    task: Task
    queued: bool
    target: AgentTarget
    message: Any = None


class AgentSwitchService:
    """Agent 切换的唯一入口（API 与 Run 收尾共用同一套预检/应用逻辑）。"""

    def __init__(self, container: Container) -> None:
        self._container = container

    # --- 对外入口 ---

    async def switch(self, task: Task, target_id: str | uuid.UUID) -> SwitchResult:
        """切换 Agent：执行中排队，空闲时立即应用；失败保留原 Agent 并抛原因。"""
        target = await self.preflight(target_id)
        if str(target.agent_id) == str(task.agent_id or ""):
            # 选回当前 Agent：撤销可能存在的排队，视为已完成
            if task.pending_agent_id is not None:
                task = await self._container.task_repo.clear_pending_agent(task.id)
            return SwitchResult(task=task, queued=False, target=target)

        if self._has_active_run(task):
            task = await self._container.task_repo.set_pending_agent(task.id, target.agent_id)
            await self._publish(
                task,
                "agent_switch_queued",
                {
                    "task": task.to_dict(),
                    "to_agent": target.name,
                    "to_agent_id": str(target.agent_id),
                },
            )
            logger.info(
                "Agent 切换已排队 task=%s %s → %s（本轮结束后生效）",
                task.id,
                task.agent_id,
                target.agent_id,
            )
            return SwitchResult(task=task, queued=True, target=target)

        result = await self._apply(task, target, run_id=task.run_id, status=str(task.status))
        return SwitchResult(task=result[0], queued=False, target=target, message=result[1])

    async def apply_pending(
        self, task: Task, run_id: str | None, status: str
    ) -> SwitchResult | None:
        """Run 真实收尾后应用排队切换；没有排队或正等人决策时不动。"""
        fresh = await self._container.task_repo.get(task.id)
        if fresh.pending_agent_id is None:
            return None
        if fresh.status == TaskStatus.WAITING_FOR_USER_DECISION.value:
            # 任务还没真正结束（等用户选方案）：排队保留，等决策后那一轮收尾再应用
            return None
        try:
            target = await self.preflight(fresh.pending_agent_id)
        except Exception as exc:  # noqa: BLE001 - 目标失效不能让任务收尾失败
            fresh = await self._container.task_repo.clear_pending_agent(fresh.id)
            note = (
                f"排队的 Agent 切换未生效：{exc}。"
                "本轮仍由原 Agent 收尾，下一条消息也继续用原 Agent。"
            )
            message = (
                await self._container.task_repo.add_messages(
                    fresh.id,
                    [
                        MessageDraft(
                            role=TaskMessageRole.ASSISTANT.value,
                            kind="text",
                            content=note,
                            payload={"reason": "agent_switch_failed", "error": str(exc)},
                        )
                    ],
                )
            )[0]
            await self._publish(
                fresh,
                "agent_switch_failed",
                {"task": fresh.to_dict(), "reason": str(exc), "message": message.to_dict()},
            )
            logger.warning(
                "排队的 Agent 切换未生效 task=%s target=%s error=%s",
                fresh.id,
                fresh.pending_agent_id,
                exc,
            )
            return None
        task, message = await self._apply(fresh, target, run_id=run_id, status=status)
        return SwitchResult(task=task, queued=False, target=target, message=message)

    async def preflight(self, target_id: str | uuid.UUID) -> AgentTarget:
        """校验目标 Agent 存在且其 runtime 可用；不可用直接抛错（不写库）。"""
        container = self._container
        try:
            agent = container.agents.get(str(target_id))
        except NotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001 - 注册表异常按「不存在」处理
            raise NotFoundError(
                f"Agent {target_id} 不存在", details={"agent_id": str(target_id)}
            ) from exc
        spec = getattr(agent, "spec", None)
        runtime = str(getattr(spec, "runtime", None) or DEFAULT_RUNTIME)
        name = str(getattr(spec, "name", None) or target_id)
        if runtime in CLI_RUNTIMES:
            row = await container.installations.get(runtime)
            state = row.status if row is not None else None
            if state != AgentInstallStatus.READY.value:
                raise ConflictError(
                    f"{name} 使用的 {runtime} 尚未接入 Flux（当前状态：{state or '未发现'}），"
                    f"切换未生效，仍由原 Agent 执行",
                    details={"agent_id": str(target_id), "runtime": runtime, "status": state},
                )
        return AgentTarget(agent_id=uuid.UUID(str(target_id)), name=name, runtime=runtime)

    # --- 内部 ---

    @staticmethod
    def _has_active_run(task: Task) -> bool:
        if task.status in _ACTIVE_STATUSES:
            return True
        return task.pending_decision() is not None

    async def _apply(
        self,
        task: Task,
        target: AgentTarget,
        *,
        run_id: str | None,
        status: str,
    ) -> tuple[Task, Any]:
        """真正落切换：改 Agent、清模型覆盖、生成交接卡、发事件。"""
        container = self._container
        previous_id = task.agent_id
        previous_name = self._agent_name(previous_id)
        task = await container.task_repo.set_agent(task.id, target.agent_id)
        message = None
        # 还没跑过任何一轮（新建对话刚选完 Agent）：没有可交接的事实，不落空卡片
        if run_id:
            card = await self._handoff_card(
                task,
                previous_name=previous_name,
                target=target,
                run_id=run_id,
                status=status,
            )
            message = (
                await container.task_repo.add_messages(
                    task.id,
                    [
                        MessageDraft(
                            role=TaskMessageRole.ASSISTANT.value,
                            kind="handoff",
                            content=card,
                            payload={
                                "from_agent_id": str(previous_id) if previous_id else None,
                                "from_agent": previous_name,
                                "to_agent_id": str(target.agent_id),
                                "to_agent": target.name,
                                "runtime": target.runtime,
                                "run_id": run_id,
                                "status": status,
                            },
                        )
                    ],
                )
            )[0]
        await self._publish(
            task,
            "agent_switched",
            {
                "task": task.to_dict(),
                "from_agent": previous_name,
                "to_agent": target.name,
                "message": message.to_dict() if message is not None else None,
            },
        )
        logger.info(
            "Agent 切换已生效 task=%s %s(%s) → %s(%s) runtime=%s",
            task.id,
            previous_name,
            previous_id,
            target.name,
            target.agent_id,
            target.runtime,
        )
        return task, message

    def _agent_name(self, agent_id: Any) -> str:
        if agent_id is None:
            return "未指定 Agent"
        try:
            agent = self._container.agents.get(str(agent_id))
        except Exception:  # noqa: BLE001 - 名字只用于展示，取不到就退回 id
            return str(agent_id)
        return str(getattr(getattr(agent, "spec", None), "name", None) or agent_id)

    async def _handoff_card(
        self,
        task: Task,
        *,
        previous_name: str,
        target: AgentTarget,
        run_id: str | None,
        status: str,
    ) -> str:
        """拼一张给接手 Agent 看的交接卡；任何一段取不到都降级，绝不因此切换失败。"""
        lines = [
            f"## Agent 交接卡 · {previous_name} → {target.name}",
            f"- 交接时间：{datetime.now(timezone.utc).isoformat()}",
            f"- 任务：{_clip(task.description or '', HANDOFF_TASK_CHARS)}",
            f"- 上一轮结果：{status or '未知'}",
        ]
        if run_id:
            lines.append(f"- 上一轮 Run：{run_id}")

        memory = await self._latest_memory(task.id)
        if memory:
            lines += ["", "## 上一轮工作记忆（接手先读，事实以此为准）", _strip_heading(memory)]

        trace = await self._recent_trace(task.id)
        if trace:
            lines += ["", trace]

        document = await self._handoff_document(run_id)
        if document:
            lines += [
                "",
                "## 上一轮事实脊柱（来自执行日志，核对依据）",
                document[:HANDOFF_DOC_CHARS],
            ]

        lines += [
            "",
            "## 交接约定",
            "- 从下一步接着做，不要重做已完成的步骤；动手前先核对上面的事实与最终答复。",
            "- 交接卡由平台自动生成，只描述上一轮；用户的最新消息才是本轮的唯一指令。",
        ]
        card = "\n".join(lines)
        if len(card) > HANDOFF_CARD_CHARS:
            card = card[: HANDOFF_CARD_CHARS - 1] + "…"
        return card

    async def _latest_memory(self, task_id: uuid.UUID) -> str:
        repo = getattr(self._container, "task_memory", None)
        if repo is None:
            return ""
        try:
            card = await repo.latest(task_id)
        except Exception:  # noqa: BLE001 - 交接卡降级，不阻塞切换
            logger.warning("读取工作记忆卡失败 task=%s", task_id, exc_info=True)
            return ""
        return card.content.strip() if card is not None else ""

    async def _recent_trace(self, task_id: uuid.UUID) -> str:
        try:
            messages, _ = await self._container.task_repo.list_messages(task_id, limit=40)
        except Exception:  # noqa: BLE001 - 交接卡降级，不阻塞切换
            logger.warning("读取对话轨迹失败 task=%s", task_id, exc_info=True)
            return ""
        return build_dialogue_trace(
            messages,
            budget_chars=HANDOFF_TRACE_CHARS,
            max_messages=HANDOFF_TRACE_MESSAGES,
        )

    async def _handoff_document(self, run_id: str | None) -> str:
        """上一轮有 step 日志时附九段式文档；没有就跳过（不编造）。"""
        repo = getattr(self._container, "context_steps", None)
        if repo is None or not run_id:
            return ""
        try:
            steps = await repo.list_by_run(run_id, limit=200)
        except Exception:  # noqa: BLE001 - 交接卡降级，不阻塞切换
            logger.warning("读取交接 step 失败 run=%s", run_id, exc_info=True)
            return ""
        if not steps:
            return ""
        try:
            return build_handoff(run_id, list(steps))
        except Exception:  # noqa: BLE001 - 文档生成失败不影响切换
            logger.warning("生成九段式交接文档失败 run=%s", run_id, exc_info=True)
            return ""

    async def _publish(self, task: Task, event: str, payload: dict[str, Any]) -> None:
        stream = getattr(self._container, "task_stream", None)
        if stream is None:
            return
        try:
            await stream.publish(task.id, event, payload)
        except Exception:  # noqa: BLE001 - 流推送失败不影响已落库的切换
            logger.warning("推送 %s 事件失败 task=%s", event, task.id, exc_info=True)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _strip_heading(card: str) -> str:
    """去掉记忆卡自带的一级标题，避免交接卡里出现两个同名小节。"""
    lines = card.splitlines()
    if lines and lines[0].startswith("## "):
        lines = lines[1:]
    return "\n".join(lines).strip()


__all__ = ["AgentSwitchService", "AgentTarget", "HANDOFF_CARD_CHARS", "SwitchResult"]
