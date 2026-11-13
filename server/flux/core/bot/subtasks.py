"""Bot 后台子任务：派活后用户继续聊天，结果回写到父对话。

设计要点：
- 子任务复用既有 `tasks` / `task_messages` / `task_stream` 链路，`origin=subtask`
  让它不进入普通任务/对话列表；`parent_task_id` 指回 Bot 的聊天任务。
- 子任务**不碰** `bots.chat_task_id` 与 `bots.last_task_id`：前者是对话载体，
  后者是定时/手动开工的忙闲锁。子任务独立运行，不能把 Bot 锁成「忙」。
- 子任务是真正的 Agent 活：由 `TaskRunService` 在 Claude Code 内核上起一轮 Run，
  完成信号取既有的 Run 终态（`dsh.run_*` 事件 + `tasks.status`），结果正文取
  内核 Run 收尾写下的 `run_finished` 消息。
- 每次开始/完成/失败/取消都会往父对话追加一条 `kind=subtask` 的消息，
  同时发 `subtask_update` 流事件与 `Events.SUBTASK_*` 总线事件，供后续 UI 实时更新。
- 后台 job 由本服务持有强引用；进程退出时统一取消并等待，避免测试/退出挂住。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from typing import TYPE_CHECKING, Any

from flux.core.event.bus import Events
from flux.core.task_engine.repository import MessageDraft
from flux.enums import (
    DecisionMode,
    PermissionMode,
    TaskMessageRole,
    TaskOrigin,
    TaskStatus,
)
from flux.errors import BadRequestError, ConflictError, NotFoundError, ValidationError
from flux.logging import get_logger
from flux.models.task import Task

if TYPE_CHECKING:
    from flux.container import Container
    from flux.models.bot import Bot

logger = get_logger(__name__)

__all__ = [
    "SUBTASK_TOOLS",
    "BotSubtaskService",
    "run_subtask_tool",
]

#: 子任务终态：到达后不再接受取消，也不会被新结果覆盖
TERMINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
)

#: 父对话消息里展示的标题长度（指令太长时截一段，不把整段指令塞进状态气泡）
_TITLE_CHARS = 60
#: 父对话消息里的结果摘要长度
_SUMMARY_CHARS = 240
#: 父对话消息正文里的结果长度；完整结果仍在子任务 result 里
_REPLY_CHARS = 4000
#: 失败原因落消息/结果的长度上限（不写 traceback）
_ERROR_CHARS = 500

#: 内核 Run 的终态事件：事件到达即唤醒等待，避免只靠轮询空转
_RUN_TERMINAL_EVENTS = (
    Events.DSH_RUN_COMPLETED,
    Events.DSH_RUN_FAILED,
    Events.DSH_RUN_CANCELLED,
    Events.DSH_RUN_TIMEOUT,
    Events.DSH_RUN_INTERRUPTED,
)
#: 兜底轮询间隔（秒）：事件通常先到，轮询只防事件在崩溃时丢失。
#: 间隔偏大是有意的——每次轮询都是一次数据库读，靠总线事件唤醒才是主路径。
_POLL_INTERVAL_SECONDS = 2.0
#: 终态已落库、`run_finished` 消息可能还在写：最多等 1 秒（20 × 0.05s）
_FINISH_MESSAGE_ATTEMPTS = 20
_FINISH_MESSAGE_INTERVAL_SECONDS = 0.05


def _title_for(title: str | None, instruction: str) -> str:
    """展示用标题：优先用户给的短标题，否则截取指令开头。"""
    cleaned = (title or "").strip()
    if cleaned:
        return cleaned[:_TITLE_CHARS]
    text = " ".join((instruction or "").split())
    return (text or "后台任务")[:_TITLE_CHARS]


def _summary(text: str, limit: int = _SUMMARY_CHARS) -> str:
    cleaned = (text or "").strip()
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "…"


class BotSubtaskService:
    """Bot 对话里的后台子任务编排（创建 / 查询 / 取消 / 生命周期）。"""

    def __init__(self, container: Container) -> None:
        self._container = container
        #: 等待内核 Run 收尾的总时限（秒）；<= 0 表示不设上限
        self._timeout_seconds = float(container.settings.bot_subtask_timeout_seconds)
        #: 正在运行的 job 强引用：create_task 的返回值必须有人持有，否则可能被 GC 回收。
        self._jobs: set[asyncio.Task[None]] = set()
        #: subtask id → job，取消时据此精确打断对应协程。
        self._tasks_by_subtask: dict[uuid.UUID, asyncio.Task[None]] = {}
        #: 已被显式取消的子任务：job 醒来后据此避免再写一次完成/失败。
        self._cancelled: set[uuid.UUID] = set()
        #: 已回写过父对话终态的子任务：完成/失败/取消只回写一次。
        self._published: set[uuid.UUID] = set()

    # --- 创建 / 查询 ---

    async def create(
        self,
        bot: Bot,
        *,
        instruction: str,
        title: str | None = None,
    ) -> Task:
        """派一条后台子任务并立即返回；真正的内核 Run 在后台协程里跑。"""
        text = (instruction or "").strip()
        if not text:
            raise ValidationError(
                "子任务指令不能为空", details={"field": "instruction", "bot_id": str(bot.id)}
            )

        container = self._container
        # 复用对话服务已有的懒创建逻辑：父对话必须与聊天轮次是同一条 tasks 行。
        parent = await container.bot_conversation._chat_task(bot)  # noqa: SLF001 - 单一懒创建路径
        task = await container.task_repo.create(
            task_id=uuid.uuid4(),
            description=text,
            priority=100,
            project_id=None,
            agent_id=bot.agent_id,
            decision_mode=DecisionMode(bot.decision_mode or DecisionMode.AUTO),
            permission_mode=PermissionMode(bot.permission_mode or PermissionMode.UNATTENDED),
            model=bot.model,
            api_key_id=bot.api_key_id,
            origin=TaskOrigin.SUBTASK,
            bot_name=bot.name,
            avatar_shape=bot.avatar_shape,
            avatar_color=bot.avatar_color,
            bot_id=bot.id,
            workspace=str(container.bots.workspace_path(bot)),
            parent_task_id=parent.id,
        )
        job = asyncio.create_task(
            self._run(task.id, text, title),
            name=f"flux-bot-subtask-{task.id}",
        )
        self._track(task.id, job)
        logger.info("Bot 子任务已创建 bot=%s subtask=%s parent=%s", bot.id, task.id, parent.id)
        return task

    async def list(
        self,
        bot: Bot,
        *,
        status: TaskStatus | None = None,
        limit: int = 50,
    ) -> list[Task]:
        """某个 Bot 的子任务，按创建时间倒序；可按状态过滤。"""
        return await self._container.task_repo.list(
            bot_id=bot.id,
            origin=TaskOrigin.SUBTASK,
            status=status,
            limit=limit,
        )

    async def get(self, bot: Bot, subtask_id: uuid.UUID) -> Task:
        """取一条属于该 Bot 的子任务；不属于时按不存在处理（不泄露其他 Bot 的 id）。"""
        task = await self._container.task_repo.get(subtask_id)
        if task.origin != TaskOrigin.SUBTASK.value or task.bot_id != bot.id:
            raise NotFoundError(
                f"子任务 {subtask_id} 不存在",
                details={"bot_id": str(bot.id), "subtask_id": str(subtask_id)},
            )
        return task

    # --- 取消 / 停机 ---

    async def cancel(self, bot: Bot, subtask_id: uuid.UUID) -> Task:
        """取消仍在执行的子任务；终态任务返回 409。"""
        task = await self.get(bot, subtask_id)
        if task.status in TERMINAL_STATUSES:
            raise ConflictError(
                f"子任务已结束（{task.status}），不能再取消",
                details={"subtask_id": str(subtask_id), "status": task.status},
            )

        # 先打标记再收尾：即使内核 Run 恰好在此刻结束，job 醒来也会看到标记而不再写完成。
        self._cancelled.add(subtask_id)
        # 先停后台协程，再停内核 Run：Run 还在改文件与「已取消」是矛盾的。
        job = self._tasks_by_subtask.get(subtask_id)
        if job is not None and not job.done():
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        # 协程停下的这段时间里 Run 可能刚好自己跑完：重读一次拿到真实状态与 run_id。
        task = await self.get(bot, subtask_id)
        if task.status in TERMINAL_STATUSES:
            raise ConflictError(
                f"子任务已结束（{task.status}），不能再取消",
                details={"subtask_id": str(subtask_id), "status": task.status},
            )
        await self._cancel_kernel_run(task, reason="user_cancel")

        title = _title_for(None, task.description)
        finished = await self._finish(
            task,
            TaskStatus.CANCELLED,
            title=title,
            result={"cancelled": True, "run_id": task.run_id},
            content=f"后台任务「{title}」已取消。",
            summary="已取消",
            event=Events.SUBTASK_CANCELLED,
        )
        if finished is None:
            # 另一条收尾路径先到达终态：让调用方看到真实的当前状态。
            latest = await self.get(bot, subtask_id)
            if latest.status == TaskStatus.CANCELLED.value:
                # 内核 Run 的桥接已经把状态收成 cancelled，结果也已回写父对话。
                logger.info(
                    "Bot 子任务已取消 bot=%s subtask=%s parent=%s",
                    bot.id,
                    subtask_id,
                    latest.parent_task_id,
                )
                return latest
            raise ConflictError(
                f"子任务已结束（{latest.status}），不能再取消",
                details={"subtask_id": str(subtask_id), "status": latest.status},
            )

        logger.info(
            "Bot 子任务已取消 bot=%s subtask=%s parent=%s",
            bot.id,
            subtask_id,
            finished.parent_task_id,
        )
        return finished

    async def shutdown(self) -> None:
        """取消并等待所有在跑的 job 与内核 Run；已终态的子任务保持不变。"""
        jobs = list(self._jobs)
        subtask_ids = list(self._tasks_by_subtask)
        self._jobs.clear()
        self._tasks_by_subtask.clear()
        for job in jobs:
            job.cancel()
        if jobs:
            # return_exceptions：单个 job 收尾失败不能挡住其余协程，也不能让停机本身失败。
            await asyncio.gather(*jobs, return_exceptions=True)
        for subtask_id in subtask_ids:
            # 协程停下不等于内核进程停下：Run 必须显式取消，否则退出后还会残留子进程。
            try:
                task = await self._container.task_repo.get(subtask_id)
            except Exception:  # noqa: BLE001 - 读不到就只做状态收尾
                logger.warning("停机时读取子任务 %s 失败", subtask_id, exc_info=True)
                task = None
            if task is not None and task.status not in TERMINAL_STATUSES:
                await self._cancel_kernel_run(task, reason="shutdown")
            # job 被取消时来不及写终态：这里补一次，避免重启后永远停在 running/pending。
            try:
                updated = await self._container.task_repo.finish_if_active(
                    subtask_id, TaskStatus.CANCELLED, {"cancelled": True}
                )
            except Exception:  # noqa: BLE001 - 停机收尾尽力而为
                logger.warning("停机时收尾子任务 %s 失败", subtask_id, exc_info=True)
                continue
            if updated is None:
                # 内核 Run 的桥接可能先一步把状态收成 cancelled：重读一次以库里为准。
                try:
                    latest = await self._container.task_repo.get(subtask_id)
                except Exception:  # noqa: BLE001 - 读不到就不补日志
                    latest = None
                if latest is not None and latest.status == TaskStatus.CANCELLED.value:
                    if latest.result is None:
                        # 桥接收状态时不带结果：补上，别让终态行留下空结果。
                        with contextlib.suppress(Exception):
                            latest = await self._container.task_repo.set_result(
                                subtask_id, {"cancelled": True}
                            )
                    updated = latest
            if updated is not None:
                logger.info(
                    "停机时子任务被标记为 cancelled bot=%s subtask=%s parent=%s",
                    updated.bot_id,
                    subtask_id,
                    updated.parent_task_id,
                )
        self._cancelled.clear()
        self._published.clear()

    # --- 后台 job ---

    def _track(self, subtask_id: uuid.UUID, job: asyncio.Task[None]) -> None:
        self._jobs.add(job)
        self._tasks_by_subtask[subtask_id] = job

        def _cleanup(done: asyncio.Task[None]) -> None:
            self._jobs.discard(done)
            if self._tasks_by_subtask.get(subtask_id) is done:
                self._tasks_by_subtask.pop(subtask_id, None)

        job.add_done_callback(_cleanup)

    async def _run(self, subtask_id: uuid.UUID, instruction: str, title: str | None) -> None:
        container = self._container
        try:
            task = await container.task_repo.get(subtask_id)
        except NotFoundError:
            logger.warning("子任务 %s 在开始前已被删除", subtask_id)
            return

        display = _title_for(title, instruction)
        try:
            # 只有当它还是 pending 时才进入 running：用户在启动前点了取消时，
            # 这里不能把已写好的 cancelled 顶回 running。
            running = await container.task_repo.start_if_pending(subtask_id)
        except NotFoundError:
            logger.warning("子任务 %s 在开始前已被删除", subtask_id)
            return
        if running is None:
            logger.info(
                "子任务在启动前已结束，跳过 running 收尾 subtask=%s bot=%s parent=%s",
                subtask_id,
                task.bot_id,
                task.parent_task_id,
            )
            return
        task = running
        await self._publish_parent(
            task,
            TaskStatus.RUNNING,
            title=display,
            content=f"后台任务「{display}」已开始，完成后会把结果发到这里。",
            summary="已开始",
            event=Events.SUBTASK_STARTED,
        )
        logger.info("子任务开始 subtask=%s bot=%s", task.id, task.bot_id)

        try:
            started = await container.task_run_service.start(task)
        except asyncio.CancelledError:
            # cancel()/shutdown() 已负责收尾；这里只让协程正常退出。
            logger.info(
                "子任务被取消 subtask=%s bot=%s parent=%s",
                subtask_id,
                task.bot_id,
                task.parent_task_id,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - 起不来必须落成可查询的终态
            error = str(exc).strip()[:_ERROR_CHARS] or "内核 Run 启动失败"
            if subtask_id in self._cancelled:
                # 取消已先一步收尾：此时抛出的异常不是「失败」，别记成一条假的失败终态。
                logger.info(
                    "子任务已取消，忽略内核 Run 启动异常 subtask=%s bot=%s error=%s",
                    subtask_id,
                    task.bot_id,
                    error,
                )
                return
            logger.warning(
                "子任务启动内核 Run 失败 subtask=%s bot=%s parent=%s error=%s",
                subtask_id,
                task.bot_id,
                task.parent_task_id,
                error,
            )
            await self._finish(
                task,
                TaskStatus.FAILED,
                title=display,
                result={"error": error},
                content=f"后台任务「{display}」失败：{error}",
                summary=_summary(error),
                event=Events.SUBTASK_FAILED,
            )
            return

        run_id = started.task.run_id
        logger.info(
            "子任务内核 Run 已启动 subtask=%s bot=%s parent=%s run=%s",
            subtask_id,
            task.bot_id,
            task.parent_task_id,
            run_id,
        )

        try:
            settled = await self._wait_for_terminal(subtask_id, run_id)
        except asyncio.CancelledError:
            logger.info(
                "子任务被取消 subtask=%s bot=%s parent=%s",
                subtask_id,
                task.bot_id,
                task.parent_task_id,
            )
            raise
        except NotFoundError:
            logger.warning("子任务 %s 在运行中被删除", subtask_id)
            return

        if subtask_id in self._cancelled:
            # cancel() 已负责收尾：这里不再写第二条终态。
            return

        if settled.status not in TERMINAL_STATUSES:
            await self._fail_on_timeout(settled, display)
            return

        await self._report_outcome(settled, display)

    async def _wait_for_terminal(self, subtask_id: uuid.UUID, run_id: str | None) -> Task:
        """等内核 Run 收尾：既有的 `dsh.run_*` 终态事件负责唤醒，轮询只做兜底。

        返回最后一次读到的任务行；超时时它仍是非终态，由调用方负责终止与收尾。
        """
        container = self._container
        wake = asyncio.Event()

        def _on_run_terminal(_event: str, body: dict[str, Any]) -> None:
            if run_id and body.get("run_id") == run_id:
                wake.set()
            elif body.get("task_id") == str(subtask_id):
                # 自动重试会换一个新 run_id：按任务归属同样能收到信号
                wake.set()

        for event in _RUN_TERMINAL_EVENTS:
            container.bus.subscribe(event, _on_run_terminal)
        try:
            deadline = (
                time.monotonic() + self._timeout_seconds if self._timeout_seconds > 0 else None
            )
            while True:
                # 先清后读：读取期间到达的事件会把 wake 置位，随后的等待立即返回并重读，
                # 因此状态检查与等待之间的事件不会被丢掉。
                wake.clear()
                task = await container.task_repo.get(subtask_id)
                if task.status in TERMINAL_STATUSES:
                    return task
                remaining = _POLL_INTERVAL_SECONDS
                if deadline is not None:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return task
                    remaining = min(remaining, left)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(wake.wait(), timeout=remaining)
        finally:
            for event in _RUN_TERMINAL_EVENTS:
                container.bus.unsubscribe(event, _on_run_terminal)

    async def _report_outcome(self, task: Task, display: str) -> None:
        """把内核 Run 的终态翻译成父对话里的 completed / failed / cancelled。"""
        status = TaskStatus(task.status)
        if status is TaskStatus.CANCELLED:
            await self._finish(
                task,
                TaskStatus.CANCELLED,
                title=display,
                result={"cancelled": True, "run_id": task.run_id},
                content=f"后台任务「{display}」已取消。",
                summary="已取消",
                event=Events.SUBTASK_CANCELLED,
            )
            logger.info(
                "子任务已取消 subtask=%s bot=%s parent=%s",
                task.id,
                task.bot_id,
                task.parent_task_id,
            )
            return

        # 失败也读一眼结果消息：Run 的桥接把失败原因写进了那里，作为兜底比状态码可读
        reply, error = await self._run_outcome(task, with_message=True)
        if status is TaskStatus.COMPLETED:
            text = reply or "（内核没有返回内容）"
            await self._finish(
                task,
                TaskStatus.COMPLETED,
                title=display,
                result={"reply": text, "run_id": task.run_id},
                content=f"后台任务「{display}」已完成：\n{text[:_REPLY_CHARS]}",
                summary=_summary(text),
                event=Events.SUBTASK_COMPLETED,
            )
            logger.info("子任务完成 subtask=%s bot=%s", task.id, task.bot_id)
            return

        reason = error or reply[:_ERROR_CHARS] or "内核 Run 执行失败"
        await self._finish(
            task,
            TaskStatus.FAILED,
            title=display,
            result={"error": reason, "run_id": task.run_id},
            content=f"后台任务「{display}」失败：{reason}",
            summary=_summary(reason),
            event=Events.SUBTASK_FAILED,
        )
        logger.warning(
            "子任务失败 subtask=%s bot=%s parent=%s error=%s",
            task.id,
            task.bot_id,
            task.parent_task_id,
            reason,
        )

    async def _run_outcome(self, task: Task, *, with_message: bool) -> tuple[str, str]:
        """从内核 Run 的既有落库物里取最终正文与失败原因。

        正文在 `TaskRunBridge` 收尾写下的 `run_finished` 消息里（completed 就是 Agent
        的最终回复原文），失败原因优先取 `agent_runs.error`。
        """
        container = self._container
        text = ""
        if with_message:
            message = await self._final_message(task.id)
            text = (message.content or "").strip() if message is not None else ""
        error = ""
        run_id = task.run_id
        if run_id:
            try:
                run = await container.run_repo.get(run_id)
            except Exception:  # noqa: BLE001 - 读不到 Run 时退回消息正文
                logger.warning(
                    "读取子任务内核 Run 失败 subtask=%s run=%s", task.id, run_id, exc_info=True
                )
                run = None
            if run is not None and run.error:
                error = str(run.error).strip()[:_ERROR_CHARS]
        return text, error

    async def _final_message(self, task_id: uuid.UUID) -> Any:
        """取内核 Run 收尾写的 `run_finished` 消息；状态先落库、消息后写入时短暂等待。"""
        container = self._container
        for attempt in range(_FINISH_MESSAGE_ATTEMPTS):
            try:
                message = await container.task_repo.latest_message(task_id, kind="run_finished")
            except NotFoundError:
                return None
            if message is not None:
                return message
            if attempt + 1 < _FINISH_MESSAGE_ATTEMPTS:
                await asyncio.sleep(_FINISH_MESSAGE_INTERVAL_SECONDS)
        return None

    async def _fail_on_timeout(self, task: Task, display: str) -> None:
        """超过总时限仍未收尾：按失败回写父对话，再终止内核 Run。

        先落 failed 再取消 Run：Run 被取消时桥接会把任务收成 cancelled，
        超时是「失败」而不是「用户取消」，顺序反过来回写就会失真。
        """
        error = f"执行超过 {self._timeout_seconds:.0f} 秒仍未结束，已终止"
        logger.warning(
            "子任务超时 subtask=%s bot=%s parent=%s run=%s timeout=%s",
            task.id,
            task.bot_id,
            task.parent_task_id,
            task.run_id,
            self._timeout_seconds,
        )
        await self._finish(
            task,
            TaskStatus.FAILED,
            title=display,
            result={"error": error, "run_id": task.run_id},
            content=f"后台任务「{display}」失败：{error}",
            summary=_summary(error),
            event=Events.SUBTASK_FAILED,
        )
        await self._cancel_kernel_run(task, reason="timeout")

    async def _cancel_kernel_run(self, task: Task, *, reason: str) -> None:
        """取消任务当前的内核 Run（进程树清理归看护器）；没有 Run 时是空操作。"""
        run_id = task.run_id
        if not run_id:
            return
        try:
            status = await self._container.run_supervisor.cancel(run_id)
        except NotFoundError:
            logger.info(
                "子任务内核 Run 已不存在 subtask=%s bot=%s run=%s reason=%s",
                task.id,
                task.bot_id,
                run_id,
                reason,
            )
            return
        except Exception:  # noqa: BLE001 - 取消失败不能挡住子任务自身的收尾
            logger.warning(
                "取消子任务内核 Run 失败 subtask=%s bot=%s run=%s reason=%s",
                task.id,
                task.bot_id,
                run_id,
                reason,
                exc_info=True,
            )
            return
        logger.info(
            "子任务内核 Run 已取消 subtask=%s bot=%s parent=%s run=%s status=%s reason=%s",
            task.id,
            task.bot_id,
            task.parent_task_id,
            run_id,
            status,
            reason,
        )

    async def _finish(
        self,
        task: Task,
        status: TaskStatus,
        *,
        title: str,
        result: dict[str, Any],
        content: str,
        summary: str,
        event: str,
    ) -> Task | None:
        """收尾：落终态 + 结果，再往父对话追加状态消息并广播。

        内核 Run 的 `TaskRunBridge` 可能先一步把状态收成同一个终态（例如取消时）：
        这时不重复改状态，只补写结果并回写父对话；已经是别的终态则让位给先到的路径。
        """
        container = self._container
        if task.id in self._published:
            logger.info(
                "子任务结果已回写，跳过重复收尾 bot=%s subtask=%s status=%s",
                task.bot_id,
                task.id,
                status.value,
            )
            return None
        finished = await container.task_repo.finish_if_active(task.id, status, result)
        if finished is None:
            try:
                latest = await container.task_repo.get(task.id)
            except NotFoundError:
                logger.warning("子任务 %s 已不存在，跳过收尾", task.id)
                return None
            if latest.status != status.value:
                logger.info(
                    "子任务已被内核 Run 收成 %s，跳过 %s 收尾 bot=%s subtask=%s",
                    latest.status,
                    status.value,
                    task.bot_id,
                    task.id,
                )
                return None
            if latest.result is None:
                latest = await container.task_repo.set_result(task.id, result)
            finished = latest
        if task.id in self._published:
            return None
        self._published.add(task.id)
        await self._publish_parent(
            finished,
            status,
            title=title,
            content=content,
            summary=summary,
            event=event,
        )
        return finished

    async def _publish_parent(
        self,
        task: Task,
        status: TaskStatus,
        *,
        title: str,
        content: str,
        summary: str,
        event: str,
    ) -> None:
        """把状态写成父对话消息，并同时发流事件与总线事件。"""
        container = self._container
        parent_id = task.parent_task_id
        if parent_id is None:
            logger.warning("子任务 %s 没有父对话任务，跳过状态回写", task.id)
            return

        payload: dict[str, Any] = {
            "subtask_id": str(task.id),
            "status": status.value,
            "title": title,
            "summary": summary,
        }
        try:
            message = (
                await container.task_repo.add_messages(
                    parent_id,
                    [
                        MessageDraft(
                            role=TaskMessageRole.ASSISTANT.value,
                            kind="subtask",
                            content=content,
                            payload=payload,
                        )
                    ],
                )
            )[0]
        except NotFoundError:
            # 父对话被删了：子任务自己的终态已经落库，这里只留日志。
            logger.warning("父对话任务 %s 不存在，子任务 %s 状态未回写", parent_id, task.id)
            return

        await container.task_stream.publish(
            parent_id,
            "subtask_update",
            {
                "message": message.to_dict(),
                "status": status.value,
                "subtask_id": str(task.id),
                "parent_task_id": str(parent_id),
                "bot_id": str(task.bot_id) if task.bot_id is not None else None,
            },
        )
        await container.bus.publish(
            event,
            {
                "bot_id": str(task.bot_id) if task.bot_id is not None else None,
                "subtask_id": str(task.id),
                "parent_task_id": str(parent_id),
                "status": status.value,
                "title": title,
                "message_id": str(message.id),
            },
        )


# --- Bot 对话里的工具（只在 Bot 聊天轮次注入）---

_VALID_TOOL_STATUSES = {status.value for status in TaskStatus}

SUBTASK_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "subtask_start",
            "description": (
                "把耗时的长活派成后台子任务，立即返回子任务 id；用户仍可继续聊天，"
                "完成后结果会自动出现在当前对话里。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "description": (
                            "交给后台执行的完整指令（要能独立理解，不依赖这轮聊天的上下文）"
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": "可选的短标题，用于对话里的状态提示",
                    },
                },
                "required": ["instruction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "subtask_get",
            "description": "查询一条后台子任务的当前状态与结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "subtask_id": {
                        "type": "string",
                        "description": "subtask_start 返回的子任务 id",
                    },
                },
                "required": ["subtask_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "subtask_list",
            "description": "列出当前 Bot 最近的后台子任务（可按状态过滤）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": sorted(_VALID_TOOL_STATUSES),
                        "description": "可选：只看某个状态",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "返回条数，默认 20",
                    },
                },
            },
        },
    },
]


def _tool_uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise BadRequestError(f"{field} 不是合法的 UUID", details={field: str(value)}) from exc


async def run_subtask_tool(
    service: BotSubtaskService,
    bot: Bot,
    name: str,
    arguments: dict[str, Any],
) -> Any:
    """执行一个子任务工具；只接受 SUBTASK_TOOLS 里声明的名字。"""
    args = arguments or {}
    if name == "subtask_start":
        instruction = str(args.get("instruction") or "").strip()
        title = args.get("title")
        task = await service.create(
            bot,
            instruction=instruction,
            title=str(title).strip() if title else None,
        )
        return {
            "subtask_id": str(task.id),
            "status": task.status,
            "title": _title_for(str(title).strip() if title else None, instruction),
        }
    if name == "subtask_get":
        subtask_id = _tool_uuid(args.get("subtask_id"), "subtask_id")
        task = await service.get(bot, subtask_id)
        return task.to_dict()
    if name == "subtask_list":
        raw_status = args.get("status")
        wanted: TaskStatus | None = None
        if raw_status:
            try:
                wanted = TaskStatus(str(raw_status))
            except ValueError as exc:
                raise BadRequestError(
                    f"status 不是合法的任务状态：{raw_status}",
                    details={"status": raw_status, "allowed": sorted(_VALID_TOOL_STATUSES)},
                ) from exc
        try:
            limit = int(args.get("limit") or 20)
        except (TypeError, ValueError) as exc:
            raise BadRequestError("limit 必须是整数", details={"limit": args.get("limit")}) from exc
        limit = max(1, min(limit, 50))
        tasks = await service.list(bot, status=wanted, limit=limit)
        return {"subtasks": [task.to_dict() for task in tasks], "count": len(tasks)}
    raise BadRequestError(
        f"未知的子任务工具：{name}",
        details={"tool": name, "allowed": [tool["function"]["name"] for tool in SUBTASK_TOOLS]},
    )
