"""Bot 后台子任务：派活后用户继续聊天，结果回写到父对话。

设计要点：
- 子任务复用既有 `tasks` / `task_messages` / `task_stream` 链路，`origin=subtask`
  让它不进入普通任务/对话列表；`parent_task_id` 指回 Bot 的聊天任务。
- 子任务**不碰** `bots.chat_task_id` 与 `bots.last_task_id`：前者是对话载体，
  后者是定时/手动开工的忙闲锁。子任务独立运行，不能把 Bot 锁成「忙」。
- 每次开始/完成/失败/取消都会往父对话追加一条 `kind=subtask` 的消息，
  同时发 `subtask_update` 流事件与 `Events.SUBTASK_*` 总线事件，供后续 UI 实时更新。
- 后台 job 由本服务持有强引用；进程退出时统一取消并等待，避免测试/退出挂住。
"""

from __future__ import annotations

import asyncio
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
        #: 正在运行的 job 强引用：create_task 的返回值必须有人持有，否则可能被 GC 回收。
        self._jobs: set[asyncio.Task[None]] = set()
        #: subtask id → job，取消时据此精确打断对应协程。
        self._tasks_by_subtask: dict[uuid.UUID, asyncio.Task[None]] = {}
        #: 已被显式取消的子任务：job 醒来后据此避免再写一次完成/失败。
        self._cancelled: set[uuid.UUID] = set()

    # --- 创建 / 查询 ---

    async def create(
        self,
        bot: Bot,
        *,
        instruction: str,
        title: str | None = None,
    ) -> Task:
        """派一条后台子任务并立即返回；真正的模型调用在后台协程里跑。"""
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

        # 先打标记再收尾：即使模型调用恰好在此刻返回，job 醒来也会看到标记而不再写完成。
        self._cancelled.add(subtask_id)
        title = _title_for(None, task.description)
        finished = await self._finish(
            task,
            TaskStatus.CANCELLED,
            title=title,
            result={"cancelled": True},
            content=f"后台任务「{title}」已取消。",
            summary="已取消",
            event=Events.SUBTASK_CANCELLED,
        )
        if finished is None:
            # 另一条收尾路径先到达终态：让调用方看到真实的当前状态。
            latest = await self.get(bot, subtask_id)
            raise ConflictError(
                f"子任务已结束（{latest.status}），不能再取消",
                details={"subtask_id": str(subtask_id), "status": latest.status},
            )

        job = self._tasks_by_subtask.get(subtask_id)
        if job is not None and not job.done():
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        logger.info(
            "Bot 子任务已取消 bot=%s subtask=%s parent=%s",
            bot.id,
            subtask_id,
            finished.parent_task_id,
        )
        return finished

    async def shutdown(self) -> None:
        """取消并等待所有在跑的 job；已终态的子任务保持不变。"""
        jobs = list(self._jobs)
        subtask_ids = list(self._tasks_by_subtask)
        self._jobs.clear()
        self._tasks_by_subtask.clear()
        for job in jobs:
            job.cancel()
        if jobs:
            # return_exceptions：单个 job 收尾失败不能挡住其余协程，也不能让停机本身失败。
            await asyncio.gather(*jobs, return_exceptions=True)
        # job 被取消时来不及写终态：这里补一次，避免重启后永远停在 running/pending。
        for subtask_id in subtask_ids:
            try:
                updated = await self._container.task_repo.finish_if_active(
                    subtask_id, TaskStatus.CANCELLED, {"cancelled": True}
                )
            except Exception:  # noqa: BLE001 - 停机收尾尽力而为
                logger.warning("停机时收尾子任务 %s 失败", subtask_id, exc_info=True)
                continue
            if updated is not None:
                logger.info(
                    "停机时子任务被标记为 cancelled bot=%s subtask=%s parent=%s",
                    updated.bot_id,
                    subtask_id,
                    updated.parent_task_id,
                )
        self._cancelled.clear()

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
            channel = await container.key_vault.channel_for_task(task)
            assistant_turn = await container.assistant.respond(
                description=task.description,
                message=instruction,
                history=[],
                context=None,
                on_delta=None,
                channel=channel,
                model=(task.model or "").strip(),
            )
        except asyncio.CancelledError:
            # cancel()/shutdown() 已负责收尾；这里只让协程正常退出。
            logger.info(
                "子任务被取消 subtask=%s bot=%s parent=%s",
                subtask_id,
                task.bot_id,
                task.parent_task_id,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - 失败必须落成可查询的终态
            error = str(exc).strip()[:_ERROR_CHARS] or "模型调用失败"
            if subtask_id in self._cancelled:
                # 取消已先一步收尾：此时抛出的异常不是「失败」，别记成一条假的失败终态。
                logger.info(
                    "子任务已取消，忽略模型调用异常 subtask=%s bot=%s error=%s",
                    subtask_id,
                    task.bot_id,
                    error,
                )
                return
            logger.warning(
                "子任务失败 subtask=%s bot=%s parent=%s error=%s",
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

        if subtask_id in self._cancelled:
            return
        reply = (assistant_turn.reply or "").strip() or "（模型没有返回内容）"
        await self._finish(
            task,
            TaskStatus.COMPLETED,
            title=display,
            result={
                "reply": reply,
                "provider": assistant_turn.provider,
                "model": assistant_turn.model,
                "usage": assistant_turn.usage,
            },
            content=f"后台任务「{display}」已完成：\n{reply[:_REPLY_CHARS]}",
            summary=_summary(reply),
            event=Events.SUBTASK_COMPLETED,
        )
        logger.info("子任务完成 subtask=%s bot=%s", task.id, task.bot_id)

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
        """收尾：落终态 + 结果，再往父对话追加状态消息并广播。"""
        finished = await self._container.task_repo.finish_if_active(task.id, status, result)
        if finished is None:
            logger.info(
                "子任务已是终态，跳过重复收尾 bot=%s subtask=%s status=%s",
                task.bot_id,
                task.id,
                status.value,
            )
            return None
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
