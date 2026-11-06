"""把 DSH Run 的终态回写到它所属的任务（P0-05「开始执行」链路的收尾）。

平台自己不起 Run——Run 由 FluxDshClient 交给后台任务跑。Run 结束时会发
`dsh.run_completed / failed / cancelled` 事件，本桥接订阅这三个事件，
按 `tasks.run_id` 找到任务并落终态 + 追加一条真实的结果消息。

两条不做的事，都很明确：

1. 不认识的 run_id 一律忽略——DSH Run 也可以由 `/dsh/runs` 独立发起，没有对应任务很正常；
2. 任务已经停在 `waiting_for_user_decision` 时不动状态——那是用户要求的暂停，
   平台不能因为 run 线程结束就把"等人决策"改写成完成/失败（文档 §5 必须测试项）。

事件是"顺路"送达的：Flux 崩溃时事件会丢，任务就可能永久停在 running（P2-04 / TC-502）。
因此本模块还提供 `reconcile_orphan_tasks()`，把"没有活的 Run 支撑的 running 任务"收尾。
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

from flux.core.agent_runtime.run_repository import AgentRunRepository, as_utc, is_terminal, utcnow
from flux.core.context.redact import redact
from flux.core.event.bus import EventBus, Events
from flux.core.task_engine.repository import MessageDraft, TaskRepository
from flux.core.task_engine.work_memory import build_memory_card
from flux.enums import BrainSection, DshRunStatus, TaskMessageRole, TaskStatus
from flux.logging import get_logger

logger = get_logger(__name__)

#: 已是终态的任务不再被 Run 事件改写
_FINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
)

_STATUS_BY_EVENT = {
    Events.DSH_RUN_COMPLETED: TaskStatus.COMPLETED,
    Events.DSH_RUN_FAILED: TaskStatus.FAILED,
    Events.DSH_RUN_CANCELLED: TaskStatus.CANCELLED,
    # P2-15：超时与「重启后接管」都不是成功，任务按失败落终态，不留 running 僵尸
    Events.DSH_RUN_TIMEOUT: TaskStatus.FAILED,
    Events.DSH_RUN_INTERRUPTED: TaskStatus.FAILED,
}


#: 对账宽限期（秒）：任务刚被推进 running 时，Run 的行可能还没建好（start_task 先登记
#: run_id 再真正起 Run）。只看"安静了足够久"的 running 任务，避免把正在起跑的 Run 判成孤儿。
_ORPHAN_GRACE_SECONDS = 120.0

#: Run 终态 → 任务终态（对账用；超时/中断都不是成功，按失败落）
_TASK_STATUS_BY_RUN = {
    DshRunStatus.COMPLETED: TaskStatus.COMPLETED,
    DshRunStatus.CANCELLED: TaskStatus.CANCELLED,
    DshRunStatus.FAILED: TaskStatus.FAILED,
    DshRunStatus.TIMEOUT: TaskStatus.FAILED,
    DshRunStatus.INTERRUPTED: TaskStatus.FAILED,
}

#: 自动沉淀只写终态里的"有结果"两种：取消不是产出，不该在项目记忆里留一条噪音
_SEDIMENT_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED})
#: 沉淀正文上限：Project 层是给下一轮开工看的短记，不是执行日志（明细在提案与 Run 里）
SEDIMENT_MAX_CHARS = 800
#: 任务描述在沉淀里的截断长度
SEDIMENT_TASK_CHARS = 200
#: 错误在沉淀里的截断长度（只留尾部，栈的最后几行最能定位问题）
SEDIMENT_ERROR_CHARS = 300

#: 上游瞬时故障的判定词：命中才自动重试，真正的业务失败（鉴权、上下文超限、用户取消）不重试。
#: 观测到的真实形态：opencode-go 返回 429 后流挂住，Flux 侧最终报 ReadTimeout。
_TRANSIENT_ERROR_MARKERS = (
    "readtimeout",
    "read timeout",
    "timed out",
    "timeout",
    "too many requests",
    "connection reset",
    "remoteprotocol",
    "connecterror",
    "connect error",
    "temporarily unavailable",
    "service unavailable",
)

#: HTTP 状态码按词边界匹配：错误正文里的 "4291" 或路径里的数字不算限流，避免误重试。
_TRANSIENT_STATUS_RE = re.compile(r"\b(429|500|502|503|504)\b")

#: 每个任务每轮最多自动重试一次：重试也失败就按真实失败收尾，避免无限重试烧额度。
_AUTO_RETRY_BUDGET = 1
#: 限流后等一会儿再重试，给上游恢复窗口（429 常见按分钟限流）。
_AUTO_RETRY_DELAY_SECONDS = 30.0
#: 自动重试时给 Agent 的续跑指令：让模型接着做，避免把已完成的部分重来一遍。
#: CLI runtime 每次重试都是新会话，所以必须提示先读上下文与已产出物，而不是凭记忆续跑。
AUTO_RETRY_FOLLOWUP = (
    "继续（上一次执行被上游限流/超时中断，这是自动续跑的新会话）。"
    "先用 context.boot 与 design.list 查看已有进度，再从未完成的地方接着做；"
    "不要重复已完成的步骤，也不要把已交付的内容重做一遍。"
)


class TaskRunBridge:
    """DSH Run 终态 → 任务状态 / 结果消息。"""

    def __init__(
        self,
        tasks: TaskRepository,
        runs: AgentRunRepository | None = None,
        *,
        brain: Any = None,
        workspace: Any = None,
        stream: Any = None,
        trace: Any = None,
        trace_repo: Any = None,
        memory: Any = None,
    ) -> None:
        self._tasks = tasks
        # 对账要拿 Run 的权威状态；纯事件转发（单测）可以不装配
        self._runs = runs
        # 任务收尾自动沉淀（上下文闭环）：装配了 Brain 才写 Project 层，
        # 纯事件转发单测可以只传前两个参数，行为不变。
        self._brain = brain
        # 提案统计是"改了什么"的事实来源；没装配时沉淀退化成任务级摘要
        self._workspace = workspace
        # 任务对话的实时流（可选）：Run 收尾时把结果消息推给正在看对话的客户端
        self._stream = stream
        # 执行轨迹记录器（可选）：收尾时先把思考/命令轨迹落库，
        # 客户端刷新任务详情时「任务耗时 / 概览」立刻可读（不必等 SSE 订阅者处理完）
        self._trace = trace
        # 轨迹仓储（可选）：工作记忆卡要读本轮 write 类路径与命令统计
        self._trace_repo = trace_repo
        # 工作记忆卡仓储（可选）：Run 收尾时平台自动写一张，下一轮开工必带
        self._memory = memory
        # 自动重试（可选）：上游瞬时故障（429 / ReadTimeout）时由容器注入「再起一轮」的回调；
        # 没装配时退化为原有的失败收尾，行为不变。
        self._retry_handler: Callable[[Any], Awaitable[None]] | None = None
        # 排队切换 Agent（可选）：Run 真实收尾后应用「本轮结束后切换」的目标；
        # 没装配时切换只支持空闲时立即生效，行为不变。
        self._switch_handler: Callable[[Any, str | None, str], Awaitable[Any]] | None = None
        #: task_id → 本轮失败链剩余的自动重试次数（成功后清零）
        self._retry_budget: dict[str, int] = {}
        #: 正在冷却、等待自动重试的任务：此时 run_id 还是刚失败的那个 Run，
        #: 孤儿对账必须跳过它们，否则冷却没结束就被判成失败。
        self._retry_pending: set[str] = set()
        #: 重试协程的强引用，避免 create_task 后被 GC 提前回收
        self._retry_jobs: set[asyncio.Task[Any]] = set()
        #: 事件总线（可选）：任务终态再 PUBLISH 一条 task.completed / task.failed，
        #: 供 Bot 事件触发等下游订阅；没 attach 时行为不变。
        self._bus: EventBus | None = None
        #: 计费服务（可选）：Run 收尾时补一次官方额度快照，
        #: 给「外部客户端消耗」的差分估算切出干净的区间边界。
        self._billing: Any | None = None
        #: 额度快照协程的强引用
        self._snapshot_jobs: set[asyncio.Task[Any]] = set()

    def set_billing(self, billing: Any | None) -> None:
        """注入计费服务：容器在 BillingService 装配完成后调用。"""
        self._billing = billing

    def set_retry_handler(self, handler: Callable[[Any], Awaitable[None]] | None) -> None:
        """注入自动重试回调：参数是任务对象，由容器转交 TaskRunService 重新起 Run。"""
        self._retry_handler = handler

    def set_switch_handler(
        self, handler: Callable[[Any, str | None, str], Awaitable[Any]] | None
    ) -> None:
        """注入排队切换回调：参数是任务、本轮 run_id、任务终态。"""
        self._switch_handler = handler

    def attach(self, bus: EventBus) -> None:
        self._bus = bus
        for event in _STATUS_BY_EVENT:
            bus.subscribe(event, self._on_run_settled)

    async def reconcile_orphan_tasks(self) -> list[str]:
        """收尾"没有活 Run 支撑"的 running 任务（P2-04 / TC-502）。

        任务的 running 只有在一个仍然活着的 Run 支撑时才成立，两种情况都要收尾：

        - `run_id` 为空：从没有 Run 被真正起过（例如对话把 pending 推进了 running），
          任务没有在执行 → 退回 `pending`，用户仍可再点「开始执行」；
        - `run_id` 指向的 Run 已终态或已不存在：Run 结束事件在崩溃时丢了 →
          按 Run 的真实终态落任务终态，不再留在 running。
        """
        fixed: list[str] = []
        cutoff = utcnow().timestamp() - _ORPHAN_GRACE_SECONDS
        for task in await self._tasks.list(status=TaskStatus.RUNNING, limit=200):
            if as_utc(task.updated_at).timestamp() > cutoff:
                continue  # 刚动过：可能正在起跑，给它时间
            if str(task.id) in self._retry_pending:
                continue  # 自动重试冷却中：等它自己起新 Run，别当孤儿收尾
            run_id = task.run_id
            if not run_id:
                await self._tasks.set_status(task.id, TaskStatus.PENDING)
                note = "任务此前未真正开始执行（没有关联的 Agent Run），状态已退回待执行。"
                await self._tasks.add_messages(
                    task.id,
                    [
                        MessageDraft(
                            role=TaskMessageRole.ASSISTANT.value,
                            kind="run_finished",
                            content=note,
                            payload={"status": str(TaskStatus.PENDING), "reason": "orphan_no_run"},
                        )
                    ],
                )
                logger.warning("任务对账修正 task=%s running → pending（无关联 Run）", task.id)
                fixed.append(str(task.id))
                continue
            run = await self._runs.get(run_id) if self._runs is not None else None
            if run is not None and not is_terminal(run.status):
                continue  # Run 还活着：任务确实在执行，不动
            if self._trace is not None:
                # 终态事件可能丢了，但进程内的轨迹缓冲还在：顺手落库，别把过程带进坟墓
                await self._trace.flush(run_id)
            status = (
                _TASK_STATUS_BY_RUN.get(DshRunStatus(run.status), TaskStatus.FAILED)
                if run is not None
                else TaskStatus.FAILED
            )
            error = run.error if run is not None else None
            await self._tasks.set_status(task.id, status)
            await self._tasks.add_messages(
                task.id,
                [
                    MessageDraft(
                        role=TaskMessageRole.ASSISTANT.value,
                        kind="run_finished",
                        content=_summary(status, {"run_id": run_id, "error": error}),
                        payload={
                            "run_id": run_id,
                            "status": str(status),
                            "error": error,
                            "reason": "orphan_reconciled",
                        },
                    )
                ],
            )
            await self._write_memory_card(
                task, run_id, status=status, body={"run_id": run_id, "error": error}
            )
            # 本轮真的结束了：排队中的 Agent 切换在这里应用（交接卡 + 事件）
            await self._apply_pending_switch(task, run_id, status)
            logger.warning(
                "任务对账修正 task=%s running → %s（Run %s 已终态/不存在）",
                task.id,
                status,
                run_id,
            )
            fixed.append(str(task.id))
        return fixed

    async def _on_run_settled(self, event: str, body: dict[str, Any]) -> None:
        run_id = body.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return
        task = await self._tasks.get_by_run_id(run_id)
        if task is None:
            return
        self._schedule_billing_snapshot()
        if task.status in _FINAL_STATUSES:
            return
        if task.status == TaskStatus.WAITING_FOR_USER_DECISION.value:
            logger.info("dsh.run 结束但任务正等人决策，保留等待态 task=%s run=%s", task.id, run_id)
            return
        if self._trace is not None:
            # 先落轨迹再写结果消息：手机端收到 run_finished 后立刻拉详情也能读全
            await self._trace.flush(run_id)
        if await self._maybe_auto_retry(event, body, run_id, task):
            return
        status = _STATUS_BY_EVENT[event]
        await self._tasks.set_status(task.id, status)
        # 失败链已收尾：下一轮用户再发起时重新获得一次自动重试机会
        self._retry_budget.pop(str(task.id), None)
        message = (
            await self._tasks.add_messages(
                task.id,
                [
                    MessageDraft(
                        role=TaskMessageRole.ASSISTANT.value,
                        kind="run_finished",
                        content=_summary(status, body),
                        payload={
                            "run_id": run_id,
                            "status": str(status),
                            "finish_reason": body.get("finish_reason"),
                            "error": body.get("error"),
                        },
                    )
                ],
            )
        )[0]
        if self._stream is not None:
            # 对话页据此收掉"执行中"气泡并立即展示结果，不必等下一次轮询
            await self._stream.publish(
                task.id,
                "run_finished",
                {
                    "run_id": run_id,
                    "status": str(status),
                    "message": message.to_dict(),
                },
            )
            self._stream.unbind_run(run_id)
        # 上下文闭环：任务收尾把"这轮做了什么、结果如何"写进项目 Brain 的累积区，
        # 下一轮（可能是另一个 Agent）开工时 context.get 就能看到，不必从人审队列反推。
        await self._sediment(task, status=status, run_id=run_id, body=body)
        # 跨轮记忆：再落一张工作记忆卡，下一轮执行指令必须带上（不依赖用户手动摘要）
        await self._write_memory_card(task, run_id, status=status, body=body)
        # 无感切换：本轮已真实收尾，排队中的 Agent 切换此刻生效并生成交接卡
        await self._apply_pending_switch(task, run_id, status)
        # 任务终态事件：下游（如 Bot 事件触发）据此在别的任务收尾后开工
        await self._publish_task_event(task, status, body)

    async def _publish_task_event(
        self, task: Any, status: TaskStatus, body: dict[str, Any]
    ) -> None:
        """把任务终态再发一条总线事件；只发 completed / failed，cancelled 不算可触发信号。"""
        if self._bus is None:
            return
        if status == TaskStatus.COMPLETED:
            event = Events.TASK_COMPLETED
        elif status == TaskStatus.FAILED:
            event = Events.TASK_FAILED
        else:
            return
        try:
            await self._bus.publish(
                event,
                {
                    "task_id": str(task.id),
                    "bot_id": str(task.bot_id) if task.bot_id else None,
                    "project_id": str(task.project_id) if task.project_id else None,
                    "description": task.description,
                    "status": str(status),
                    "error": body.get("error"),
                },
            )
        except Exception:  # noqa: BLE001 - 事件没人订阅/订阅者失败不影响任务收尾
            logger.warning("任务终态事件发布失败 task=%s status=%s", task.id, status)

    async def _apply_pending_switch(
        self, task: Any, run_id: str | None, status: TaskStatus
    ) -> None:
        """应用排队切换；失败只记日志，不影响任务终态与结果消息。"""
        if self._switch_handler is None:
            return
        try:
            await self._switch_handler(task, run_id, str(status))
        except Exception:  # noqa: BLE001 - 切换失败不能回滚已经完成的一轮
            logger.warning(
                "应用排队 Agent 切换失败 task=%s", getattr(task, "id", "?"), exc_info=True
            )

    def _schedule_billing_snapshot(self) -> None:
        """Run 收尾补一次官方额度快照：让「Flux 在跑」和「Flux 空闲」的区间有边界。

        官方额度只有整数百分比、没有任务归因，外部客户端消耗只能靠区间差分估算；
        没有收尾快照时，一个长区间里只要有过一次 Flux 调用，整段增长都会被判成
        「无法拆分」。这次采集异步执行，失败只记日志，不影响 Run 收尾。
        """
        if self._billing is None:
            return
        job = asyncio.create_task(self._snapshot_billing())
        self._snapshot_jobs.add(job)
        job.add_done_callback(self._snapshot_jobs.discard)

    async def _snapshot_billing(self) -> None:
        try:
            await self._billing.refresh()
        except Exception:
            logger.warning("Run 收尾采集额度快照失败", exc_info=True)

    async def _write_memory_card(
        self,
        task: Any,
        run_id: str,
        *,
        status: TaskStatus,
        body: dict[str, Any],
    ) -> None:
        """Run 收尾时写一张工作记忆卡；失败只记日志，不影响任务终态与结果消息。"""
        if self._memory is None:
            return
        try:
            if await self._memory.exists(task.id, run_id):
                return
            run = await self._runs.get(run_id) if self._runs is not None else None
            trace_rows = (
                await self._trace_repo.list_for_runs([run_id])
                if self._trace_repo is not None
                else []
            )
            changes = (
                await self._workspace.list(task_id=task.id) if self._workspace is not None else []
            )
            card = build_memory_card(
                task=task,
                run=run,
                instruction=(run.instruction if run is not None else str(task.description or "")),
                final_response=str(body.get("final_response") or ""),
                status=str(status),
                trace_rows=trace_rows,
                changes=changes,
                confirmation=getattr(task, "confirmation", None),
            )
            await self._memory.add(task.id, run_id, card)
            logger.info(
                "已生成工作记忆卡 task=%s run=%s status=%s 字数=%d",
                task.id,
                run_id,
                status,
                len(card),
            )
        except Exception:  # noqa: BLE001 - 记忆卡写失败不能改写任务终态
            logger.warning("生成工作记忆卡失败 task=%s run=%s", task.id, run_id, exc_info=True)

    # --- 上游瞬时故障的自动重试 ---

    async def _maybe_auto_retry(
        self, event: str, body: dict[str, Any], run_id: str, task: Any
    ) -> bool:
        """429/超时这类上游瞬时故障自动续跑一轮，避免用户看到一次「假失败」。

        只对 `dsh.run_failed` 生效；每轮失败链最多重试一次（budget 置 0 后放行真实失败），
        取消/人工暂停/业务错误不在重试范围。重试期间任务保持 running，界面不出现失败气泡。
        """
        if event != Events.DSH_RUN_FAILED or self._retry_handler is None:
            return False
        error_text = f"{body.get('error') or ''} {body.get('finish_reason') or ''}".lower()
        transient = any(marker in error_text for marker in _TRANSIENT_ERROR_MARKERS) or bool(
            _TRANSIENT_STATUS_RE.search(error_text)
        )
        if not transient:
            return False
        key = str(task.id)
        # 本轮失败链第一次命中时领取预算；领完置 0，再失败就按真实失败收尾。
        budget = self._retry_budget.setdefault(key, _AUTO_RETRY_BUDGET)
        if budget <= 0:
            return False
        self._retry_budget[key] = 0
        self._retry_pending.add(key)

        try:
            await self._tasks.add_messages(
                task.id,
                [
                    MessageDraft(
                        role=TaskMessageRole.ASSISTANT.value,
                        kind="run_retry",
                        content="上游服务暂时限流（429/超时），正在自动重试…",
                        payload={
                            "run_id": run_id,
                            "status": "retrying",
                            "error": body.get("error"),
                            "finish_reason": body.get("finish_reason"),
                        },
                    )
                ],
            )
            if self._stream is not None:
                await self._stream.publish(
                    task.id,
                    "run_retry",
                    {"run_id": run_id, "message": "上游服务暂时限流，正在自动重试…"},
                )
        except Exception:  # noqa: BLE001 - 提示写不进去也必须继续重试
            logger.warning("写自动重试提示失败 task=%s", task.id, exc_info=True)
        finally:
            # 解除旧 Run 绑定：终态中转看到 run_id 已解绑就不会再向对话推 failed 状态
            if self._stream is not None:
                self._stream.unbind_run(run_id)
        logger.warning(
            "上游瞬时故障，自动重试 task=%s run=%s error=%s finish=%s",
            task.id,
            run_id,
            body.get("error"),
            body.get("finish_reason"),
        )
        job = asyncio.create_task(self._run_retry_after_delay(task.id))
        self._retry_jobs.add(job)
        job.add_done_callback(self._retry_jobs.discard)
        return True

    async def _run_retry_after_delay(self, task_id: Any) -> None:
        """等冷却时间后重新起一轮；任务已被用户取消/改状态时放弃重试。"""
        key = str(task_id)
        try:
            await asyncio.sleep(_AUTO_RETRY_DELAY_SECONDS)
            handler = self._retry_handler
            if handler is None:
                self._retry_budget.pop(key, None)
                return
            try:
                task = await self._tasks.get(task_id)
            except Exception:  # noqa: BLE001 - 任务读不到就不再重试
                logger.warning("自动重试读取任务失败 task=%s", task_id, exc_info=True)
                self._retry_budget.pop(key, None)
                return
            if task.status != TaskStatus.RUNNING.value:
                # 冷却期内任务已被取消/收尾：用户意图优先，不再续跑
                self._retry_budget.pop(key, None)
                return
            try:
                await handler(task)
            except Exception as exc:  # noqa: BLE001 - 重试起不来要如实收尾，不能留 running 僵尸
                logger.warning("自动重试启动失败 task=%s", task_id, exc_info=True)
                await self._retry_failed_to_start(task, exc)
                return
            logger.info("自动重试已重新起 Run task=%s", task_id)
        finally:
            # 重试协程结束（起新 Run 或放弃）都会刷新/离开冷却态；
            # 新 Run 的 set_run_id 已刷新任务 updated_at，对账据此看到活的 Run。
            self._retry_pending.discard(key)

    async def _retry_failed_to_start(self, task: Any, exc: Exception) -> None:
        """自动重试没能起 Run：如实落失败并告知用户，别让任务永久停在 running。"""
        await self._tasks.set_status(task.id, TaskStatus.FAILED)
        message = (
            await self._tasks.add_messages(
                task.id,
                [
                    MessageDraft(
                        role=TaskMessageRole.ASSISTANT.value,
                        kind="run_finished",
                        content=f"自动重试未能启动：{exc}",
                        payload={
                            "status": "failed",
                            "error": str(exc),
                            "reason": "auto_retry_failed",
                        },
                    )
                ],
            )
        )[0]
        if self._stream is not None:
            await self._stream.publish(
                task.id,
                "run_finished",
                {
                    "status": "failed",
                    "message": message.to_dict(),
                },
            )
        self._retry_budget.pop(str(task.id), None)

    async def _sediment(
        self, task: Any, *, status: TaskStatus, run_id: str, body: dict[str, Any]
    ) -> None:
        """任务终态后向 Project Brain 写一条简短沉淀（best-effort，失败不影响收尾）。"""
        if self._brain is None or task.project_id is None or status not in _SEDIMENT_STATUSES:
            return
        try:
            if await self._already_sedimented(task.project_id, run_id):
                return
            text = await self._sediment_text(task, status=status, run_id=run_id, body=body)
            await self._brain.write(
                task.project_id,
                section=BrainSection.AGENT_NOTES,
                content=text,
                meta={
                    "source": "task_settle",
                    "run_id": run_id,
                    "task_id": str(task.id),
                    "status": str(status),
                },
            )
            logger.info(
                "任务收尾已沉淀到项目 Brain task=%s project=%s run=%s",
                task.id,
                task.project_id,
                run_id,
            )
        except Exception:  # noqa: BLE001 - 沉淀失败不能改写任务终态，只记日志
            logger.warning("任务收尾沉淀失败 task=%s run=%s", task.id, run_id, exc_info=True)

    async def _already_sedimented(self, project_id: Any, run_id: str) -> bool:
        """同一 Run 只沉淀一次：重复投递的事件不能把同一件事写成两条记忆。"""
        notes = (await self._brain.sections(project_id)).get(BrainSection.AGENT_NOTES.value, [])
        return any((entry.get("metadata") or {}).get("run_id") == run_id for entry in notes)

    async def _sediment_text(
        self, task: Any, *, status: TaskStatus, run_id: str, body: dict[str, Any]
    ) -> str:
        """沉淀正文：任务、结果、失败原因、提案统计（改了什么），控制在短记长度内。"""
        description = " ".join((task.description or "").split())
        lines = [
            "任务收尾（平台自动沉淀）",
            f"- 任务：{description[:SEDIMENT_TASK_CHARS]}",
            f"- 结果：{status}（run={run_id}）",
        ]
        finish_reason = body.get("finish_reason")
        if finish_reason:
            lines.append(f"- 结束原因：{finish_reason}")
        error = body.get("error")
        if error:
            # 沉淀是长期记忆：错误里可能夹着密钥/令牌，落库前必须脱敏
            lines.append(f"- 错误：{redact(str(error)[-SEDIMENT_ERROR_CHARS:]).text}")
        if self._workspace is not None:
            changes = await self._workspace.list(task_id=task.id)
            if changes:
                counts = Counter(str(change.status) for change in changes)
                detail = "、".join(f"{name} {count}" for name, count in counts.items())
                lines.append(f"- 提案：共 {len(changes)} 条（{detail}）")
        lines.append("- 细节以人审队列与 Run 记录为准；本条是下一轮开工的短期锚点。")
        return "\n".join(lines)[:SEDIMENT_MAX_CHARS]


def _summary(status: TaskStatus, body: dict[str, Any]) -> str:
    """结果消息正文：优先用 Agent 的最终回复原文，没有才用状态兜底文案。"""
    if status is TaskStatus.COMPLETED:
        response = body.get("final_response")
        if isinstance(response, str) and response.strip():
            return response.strip()
        # 空正文说明模型这轮只做了工具动作/推理，没产出可读文字；别再报「执行完成」糊弄用户
        return "Agent 本轮没有生成文字回复（工具调用与耗时可在「执行过程」查看）。"
    if status is TaskStatus.FAILED:
        error = body.get("error")
        if error:
            return f"Agent 执行失败：{error}"
        timeout_kind = body.get("timeout_kind")
        if timeout_kind:
            return f"Agent 执行超时（{timeout_kind}），已终止。"
        return "Agent 执行失败。"
    return "Agent 执行已取消。"


__all__ = ["TaskRunBridge"]
