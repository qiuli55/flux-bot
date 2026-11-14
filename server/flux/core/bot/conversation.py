"""Bot 对话轮次：异步、持久化、按 Bot 串行。

对话与 legacy `/tasks/{id}/messages` 共用这里的 assistant-turn 逻辑：
落用户消息 → 装配历史/上下文 → 调模型 → 落回复 → 推 SSE。
Bot 额外拿到长期记忆、自己的 description/name、自己的模型通道，并把用量记到
聊天任务上（`usage_records.task_id` → `tasks.bot_id`，因此计入每日预算）。

HTTP 只把 turn 落成 queued 并入队就返回；每 Bot 一个后台队列，严格按创建顺序
一次处理一轮。队列是进程内易失结构，turn 行才是权威——重启后遗留的 queued/running
由 `recover_interrupted()` 统一标记为 failed。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from flux.core.attachment_store import UploadedFile, size_label
from flux.core.bot.subtasks import SUBTASK_TOOLS, run_subtask_tool
from flux.core.bot.turn_repository import TERMINAL_TURN_STATUSES, BotTurnRepository
from flux.core.channels.outbound_media import (
    ATTACH_FILE_TOOL_NAME,
    ATTACH_FILE_TOOLS,
    attachment_reply_payload,
    run_attach_file_tool,
)
from flux.core.context.budget import history_message_limit
from flux.core.github.service import normalize_account_key
from flux.core.github.tools import GITHUB_TOOLS, run_github_tool
from flux.core.mcp.tools.approval_rule_tools import (
    APPROVAL_RULE_TOOLS,
    run_approval_rule_tool,
)
from flux.core.mcp.tools.component_tools import (
    COMPONENT_CHAT_TOOLS,
    COMPONENT_EMIT_CHAT_TOOL_NAME,
    COMPONENT_EMIT_TOOL_NAME,
    run_component_tool,
)
from flux.core.mcp.tools.routine_tools import ROUTINE_TOOLS, run_routine_tool
from flux.core.model_gateway.base import ChatMessage
from flux.core.task_engine.assistant import AssistantTurn
from flux.core.task_engine.context_usage import CONTEXT_MESSAGE_KINDS
from flux.core.task_engine.repository import MessageDraft
from flux.core.user_scope import normalize_cloud_user_id
from flux.enums import (
    BotStatus,
    DecisionMode,
    PermissionMode,
    TaskMessageRole,
    TaskOrigin,
    TaskStatus,
)
from flux.errors import BadRequestError, ConflictError, NotFoundError, ValidationError
from flux.logging import get_logger
from flux.models.bot import Bot
from flux.models.bot_turn import BotTurn
from flux.models.task import Task, TaskMessage

if TYPE_CHECKING:
    from flux.container import Container

logger = get_logger(__name__)

_FINAL_TASK_STATUSES = {
    TaskStatus.COMPLETED.value,
    TaskStatus.CANCELLED.value,
    TaskStatus.FAILED.value,
}
#: 一次对话请求带上的历史条数上限：由 §6.1 的字符预算折算而来。
_HISTORY_LIMIT = history_message_limit()
#: Bot 记忆注入的条数与字符预算由 BotMemoryService 统一约束（render_block）。
_MEMORY_LIMIT = 8


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _meta(data: dict[str, Any], meta: dict[str, Any] | None) -> dict[str, Any]:
    if not meta:
        return data
    return {**data, **meta}


async def record_attachments(
    container: Container,
    task_uuid: uuid.UUID,
    files: list[UploadedFile],
    *,
    stream_meta: dict[str, Any] | None = None,
) -> None:
    """把本轮附件记成一条 kind=attachments 的消息：刷新/换设备后仍能看到发过哪些文件。"""
    if not files:
        return
    message = (
        await container.task_repo.add_messages(
            task_uuid,
            [
                MessageDraft(
                    role=TaskMessageRole.USER.value,
                    kind="attachments",
                    content="、".join(item.name for item in files),
                    payload={"files": [item.to_dict() for item in files]},
                )
            ],
        )
    )[0]
    await container.task_stream.publish(
        task_uuid, "user_message", _meta({"message": message.to_dict()}, stream_meta)
    )


def attachments_brief(files: list[UploadedFile]) -> str:
    """只发附件没写正文时，给助手/Agent 的一句人话说明。"""
    return "请看我随消息附上的文件：" + "、".join(
        f"{item.name}（{size_label(item.size)}）" for item in files
    )


@dataclass(frozen=True)
class TaskTurnResult:
    task: Task
    user_message: TaskMessage
    reply_message: TaskMessage
    assistant_turn: AssistantTurn


@dataclass(frozen=True)
class _PendingTurn:
    """后台队列里的一轮：内容与附件只在进程内传递。

    用户消息在 worker 真正开始处理时才落库，保证同一 Bot 的对话顺序与 turn
    创建顺序一致；进程若在处理前被杀，这一轮会按 interrupted 结束（内容不丢在半途）。
    """

    turn_id: uuid.UUID
    bot_id: uuid.UUID
    content: str
    files: tuple[UploadedFile, ...] = ()
    #: 本轮代表哪个 Flux 云用户；绑定 IM / API 登录人由调用方带进来。
    cloud_user_id: str | None = None


class BotConversationService:
    def __init__(self, container: Container) -> None:
        self._container = container
        self._turns = BotTurnRepository(container.session_factory)  # type: ignore[attr-defined]
        self._queues: dict[uuid.UUID, asyncio.Queue[_PendingTurn]] = {}
        self._workers: dict[uuid.UUID, asyncio.Task[None]] = {}
        #: 后台任务的强引用：create_task 的返回值必须有人持有，否则可能被 GC 提前回收。
        self._background: set[asyncio.Task[Any]] = set()
        self._conditions: dict[uuid.UUID, asyncio.Condition] = {}
        self._revisions: dict[uuid.UUID, int] = {}
        #: 每个 turn 上仍在使用 condition/revision 的长轮询数；归零且终态即可释放。
        self._waiters: dict[uuid.UUID, int] = {}
        #: 已经进入终态、但还有 waiter 未离开的 turn（用于 waiter 离开时补一次清理）。
        self._terminal_turns: set[uuid.UUID] = set()
        #: bot_id -> 当前 worker 正在处理的 turn，shutdown 据此立即收尾运行中的轮次。
        self._active_turns: dict[uuid.UUID, uuid.UUID] = {}

    @property
    def repository(self) -> BotTurnRepository:
        return self._turns

    # --- 按人隔离 ---

    def _owner(self) -> str | None:
        return normalize_cloud_user_id(
            getattr(self._container.settings, "owner_cloud_user_id", None)
        )

    def _current(self, cloud_user_id: str | None) -> str | None:
        """当前轮次的人格：显式传入优先，内部调用回退配置的主用户。"""
        return normalize_cloud_user_id(cloud_user_id) or self._owner()

    # --- 创建 / 查询 ---

    async def create_turn(
        self,
        bot: Bot,
        *,
        content: str,
        files: list[UploadedFile] | None = None,
        client_turn_id: str | None = None,
        cloud_user_id: str | None = None,
    ) -> tuple[BotTurn, bool]:
        """落一条 queued turn 并交给该 Bot 的后台队列。

        返回 (turn, created)：created=False 表示 client_turn_id 命中已有 turn，
        调用方应回 200 而不是 202。预算按「开始前」检查，超限直接 409
        code=budget_exhausted（与 Bot 手动开工的既有口径一致）。
        """
        text = (content or "").strip()
        files = list(files or ())
        # 与 /tasks/{id}/messages 同口径：只发附件不写正文也是合法的一轮。
        if not text and not files:
            raise ValidationError("消息内容不能为空", details={"field": "content"})

        current = self._current(cloud_user_id)
        existing = await self._turns.find_by_client_id(
            bot.id,
            client_turn_id,
            cloud_user_id=current,
            owner_cloud_user_id=self._owner(),
            scoped=current is not None,
        )
        if existing is not None:
            return existing, False

        if bot.status != BotStatus.ACTIVE.value:
            raise ConflictError(
                f"Bot {bot.name} 已暂停，恢复后才能继续对话",
                details={"bot_id": str(bot.id), "status": bot.status},
            )

        blocked = await self._container.bots.budget_block(bot)
        if blocked is not None:
            budget, used = blocked
            raise ConflictError(
                f"Bot {bot.name} 今日 token 已用 {used}，达到上限 {budget}；调高预算或等明天再聊",
                code="budget_exhausted",
                details={
                    "bot_id": str(bot.id),
                    "token_budget": budget,
                    "tokens_used_today": used,
                },
            )

        task = await self._chat_task(bot, current)
        turn, created = await self._turns.create(
            bot_id=bot.id,
            task_id=task.id,
            client_turn_id=client_turn_id,
            cloud_user_id=current,
        )
        if created:
            self._enqueue(
                _PendingTurn(
                    turn_id=turn.id,
                    bot_id=bot.id,
                    content=text,
                    files=tuple(files),
                    cloud_user_id=current,
                )
            )
        return turn, created

    async def find_turn(self, bot: Bot, client_turn_id: str | None) -> BotTurn | None:
        """按幂等键查已有轮次；频道在下载媒体前用它避免重推时重复下载。"""
        return await self._turns.find_by_client_id(bot.id, client_turn_id)

    async def get_turn(
        self,
        bot_id: uuid.UUID,
        turn_id: uuid.UUID,
        *,
        cloud_user_id: str | None = None,
        scoped: bool = False,
    ) -> BotTurn:
        return await self._turns.get(
            bot_id,
            turn_id,
            cloud_user_id=cloud_user_id,
            owner_cloud_user_id=self._owner(),
            scoped=scoped,
        )

    async def list_turns(
        self,
        bot_id: uuid.UUID,
        *,
        limit: int = 50,
        before: datetime | None = None,
        cloud_user_id: str | None = None,
        scoped: bool = False,
    ) -> list[BotTurn]:
        return await self._turns.list(
            bot_id,
            limit=limit,
            before=before,
            cloud_user_id=cloud_user_id,
            owner_cloud_user_id=self._owner(),
            scoped=scoped,
        )

    async def chat_task_id_for(
        self, bot: Bot, *, cloud_user_id: str | None = None
    ) -> uuid.UUID | None:
        """当前用户在该 Bot 下的活动对话任务 id；没有则该用户还没有历史。"""
        task = await self._container.task_repo.find_active_chat_task(
            bot.id,
            cloud_user_id=self._current(cloud_user_id),
            owner_cloud_user_id=self._owner(),
        )
        return task.id if task is not None else None

    async def chat_task_ids_for(
        self, bots: list[Bot], *, cloud_user_id: str | None = None
    ) -> dict[uuid.UUID, uuid.UUID]:
        return await self._container.task_repo.active_chat_task_ids(
            [bot.id for bot in bots],
            cloud_user_id=self._current(cloud_user_id),
            owner_cloud_user_id=self._owner(),
        )

    async def conversation(
        self,
        bot: Bot,
        *,
        limit: int = 50,
        before_id: int | None = None,
        cloud_user_id: str | None = None,
    ) -> tuple[uuid.UUID | None, list[TaskMessage]]:
        """当前用户在该 Bot 下的最近一页消息（seq 升序）；没有对话任务时返回空页。

        会话载体是「该用户自己的 chat 任务」，绝不再读 `bots.chat_task_id`——
        那个指针可能属于另一个人。
        """
        task = await self._container.task_repo.find_active_chat_task(
            bot.id,
            cloud_user_id=self._current(cloud_user_id),
            owner_cloud_user_id=self._owner(),
        )
        if task is None:
            return None, []
        messages, _ = await self._container.task_repo.list_messages(
            task.id, limit=limit, before=before_id
        )
        return task.id, messages

    async def turn_payload(self, turn: BotTurn) -> dict[str, Any]:
        """API 的 turn 形状：状态 + 两条消息（用户消息在 worker 开始前可能为空）。"""
        user_message = None
        reply_message = None
        if turn.user_message_id is not None:
            user_message = await self._container.task_repo.get_message(
                turn.task_id, turn.user_message_id
            )
        if turn.reply_message_id is not None:
            reply_message = await self._container.task_repo.get_message(
                turn.task_id, turn.reply_message_id
            )
        return {
            "id": str(turn.id),
            "status": turn.status,
            "error": turn.error,
            "error_code": turn.error_code,
            "created_at": turn.created_at.isoformat() if turn.created_at else None,
            "completed_at": turn.completed_at.isoformat() if turn.completed_at else None,
            "user_message": user_message.to_dict() if user_message is not None else None,
            "reply_message": reply_message.to_dict() if reply_message is not None else None,
        }

    async def wait_for_turn(
        self,
        bot_id: uuid.UUID,
        turn_id: uuid.UUID,
        *,
        wait_seconds: float,
        cloud_user_id: str | None = None,
        scoped: bool = False,
    ) -> BotTurn:
        """长轮询：终态立即返回，否则等一次状态变化或超时（不轮询数据库）。"""
        turn = await self._turns.get(
            bot_id,
            turn_id,
            cloud_user_id=cloud_user_id,
            owner_cloud_user_id=self._owner(),
            scoped=scoped,
        )
        if turn.status in TERMINAL_TURN_STATUSES or wait_seconds <= 0:
            return turn

        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds
        self._waiters[turn_id] = self._waiters.get(turn_id, 0) + 1
        try:
            while True:
                # 先记 revision 再读库：通知若落在两者之间或进入 condition 之前，
                # revision 会变化并触发下一轮读库，不会丢失唤醒。
                revision = self._revisions.get(turn_id, 0)
                turn = await self._turns.get(
                    bot_id,
                    turn_id,
                    cloud_user_id=cloud_user_id,
                    owner_cloud_user_id=self._owner(),
                    scoped=scoped,
                )
                if turn.status in TERMINAL_TURN_STATUSES:
                    break
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                condition = self._conditions.setdefault(turn_id, asyncio.Condition())
                async with condition:
                    if self._revisions.get(turn_id, 0) == revision:
                        try:
                            await asyncio.wait_for(condition.wait(), remaining)
                        except asyncio.TimeoutError:
                            break
            return turn
        finally:
            remaining_waiters = self._waiters.get(turn_id, 1) - 1
            if remaining_waiters > 0:
                self._waiters[turn_id] = remaining_waiters
            else:
                self._waiters.pop(turn_id, None)
                # `turn` 可能是超时瞬间的旧值；终态通知留下的标记兜住这个竞态。
                if turn.status in TERMINAL_TURN_STATUSES or turn_id in self._terminal_turns:
                    self._drop_signal_state(turn_id)

    async def recover_interrupted(self) -> int:
        count = await self._turns.mark_interrupted()
        if count:
            logger.warning("启动恢复：%s 条 Bot 对话轮次被标记为 interrupted", count)
        return count

    async def shutdown(self) -> None:
        """停止全部 Bot worker；运行中被取消的 turn 立即收成 interrupted。"""
        running_turns = list(self._active_turns.values())
        workers = list(self._workers.values())
        self._workers.clear()
        self._queues.clear()
        for worker in workers:
            worker.cancel()
        for worker in workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - 收尾失败不阻塞进程退出
                logger.warning("停止 Bot 对话 worker 失败", exc_info=True)
        if running_turns:
            try:
                count = await self._turns.mark_interrupted(running_turns)
                for turn_id in running_turns:
                    await self._notify(turn_id, terminal=True)
            except Exception:  # noqa: BLE001 - 停止时尽力而为，不阻塞进程退出
                logger.warning("停止时标记运行中的 Bot 对话轮次失败", exc_info=True)
            else:
                if count:
                    logger.warning("停止时 %s 条运行中的 Bot 对话轮次被标记为 interrupted", count)
        self._active_turns.clear()
        self._background.clear()

    # --- 共享 assistant-turn 逻辑（legacy /tasks/{id}/messages 也走这里）---

    async def begin_user_turn(
        self,
        task: Task,
        *,
        content: str,
        files: list[UploadedFile] | None = None,
        stream_meta: dict[str, Any] | None = None,
    ) -> TaskMessage:
        user_message = (
            await self._container.task_repo.add_messages(
                task.id,
                [MessageDraft(role=TaskMessageRole.USER.value, content=content)],
            )
        )[0]
        await self._container.task_stream.publish(
            task.id, "user_message", _meta({"message": user_message.to_dict()}, stream_meta)
        )
        await record_attachments(
            self._container, task.id, list(files or ()), stream_meta=stream_meta
        )
        return user_message

    async def complete_turn(
        self,
        task: Task,
        *,
        user_message: TaskMessage,
        content: str,
        files: list[UploadedFile] | None = None,
        account_key: str = "",
        description: str | None = None,
        context: str | None = None,
        history_limit: int = _HISTORY_LIMIT,
        include_github: bool = True,
        subtask_bot: Bot | None = None,
        advance_status: bool = True,
        stream_meta: dict[str, Any] | None = None,
        record_billing: bool = False,
        billing_run_id: str | None = None,
    ) -> TaskTurnResult:
        container = self._container
        files = list(files or ())
        history, _ = await container.task_repo.list_messages(task.id, limit=history_limit)
        if context is None:
            context = (
                await container.brain.context(task.project_id)
                if task.project_id is not None
                else None
            )
        await container.task_stream.publish(task.id, "assistant_start", _meta({}, stream_meta))

        async def _on_delta(text: str) -> None:
            await container.task_stream.publish(
                task.id, "assistant_delta", _meta({"text": text}, stream_meta)
            )

        prompt_text = content or attachments_brief(files)
        if files and content:
            prompt_text = content + "\n\n" + attachments_brief(files)

        github_kwargs: dict[str, Any] = {}
        if include_github:
            # 外部连接器按人隔离：REST 调用点传当前登录人；Bot 后台线程没有逐请求
            # principal，回退到部署主用户（与记忆 / 密钥的内部口径一致）。
            effective_account = account_key or (self._container.settings.owner_cloud_user_id or "")
            normalized = normalize_account_key(effective_account)
            github_connected = await container.github.is_connected(normalized)

            async def _run_github(name: str, arguments: dict[str, Any]) -> Any:
                return await run_github_tool(container.github, normalized, name, arguments)

            github_kwargs = (
                {
                    "tools": list(GITHUB_TOOLS),
                    "tool_runner": _run_github,
                    "tools_note": (
                        "当前账号已连接 GitHub。用户问到仓库、代码、Issue、PR、提交或 Actions 时，"
                        "先调用 github_* 工具查真实数据再回答；不要凭猜测编造仓库名、数量或状态。"
                    ),
                }
                if github_connected
                else {
                    "tools_note": (
                        "当前账号还没有连接 GitHub。用户如果问仓库、代码、Issue、PR 等 "
                        "GitHub 数据，明确告诉对方先到「账户 → 连接器 → GitHub」完成连接，"
                        "然后才能查询；不得编造任何仓库或提交数据。"
                    )
                }
            )
        tool_kwargs = dict(github_kwargs)
        #: 本轮 Bot 通过 attach_file 登记的出站附件；只随回复消息落库，不直接发送。
        outbound_files: list[UploadedFile] = []
        if subtask_bot is not None:
            # 子任务 / 例程工具只给 Bot 对话轮次：长活派到后台后，用户还能继续聊；
            # 结果会以 kind=subtask 消息回到当前对话，不需要本轮阻塞等待。
            # 例程工具让 Bot 直接按对话里的自然语言创建/管理自己的定时与事件触发。
            service = container.bot_subtasks

            async def _run_subtask(name: str, arguments: dict[str, Any]) -> Any:
                return await run_subtask_tool(
                    service,
                    subtask_bot,
                    name,
                    arguments,
                    cloud_user_id=account_key or None,
                    owner_cloud_user_id=self._owner(),
                )

            async def _run_routine(name: str, arguments: dict[str, Any]) -> Any:
                return await run_routine_tool(
                    container.bots,
                    subtask_bot,
                    name,
                    arguments,
                    cloud_user_id=account_key or None,
                )

            async def _run_approval_rule(name: str, arguments: dict[str, Any]) -> Any:
                return await run_approval_rule_tool(
                    container.approval_rules,
                    subtask_bot,
                    name,
                    arguments,
                    cloud_user_id=account_key or None,
                    owner_cloud_user_id=self._owner(),
                )

            async def _run_component(name: str, arguments: dict[str, Any]) -> Any:
                return await run_component_tool(container, task.id, name, arguments)

            existing_runner = tool_kwargs.get("tool_runner")

            async def _run_bot_tool(name: str, arguments: dict[str, Any]) -> Any:
                if name == ATTACH_FILE_TOOL_NAME:
                    return await run_attach_file_tool(
                        container, subtask_bot, arguments, outbound_files
                    )
                if name in {COMPONENT_EMIT_TOOL_NAME, COMPONENT_EMIT_CHAT_TOOL_NAME}:
                    return await _run_component(name, arguments)
                if name.startswith("subtask_"):
                    return await _run_subtask(name, arguments)
                if name.startswith("routine_"):
                    return await _run_routine(name, arguments)
                if name.startswith("approval_rule_"):
                    return await _run_approval_rule(name, arguments)
                if existing_runner is not None:
                    return await existing_runner(name, arguments)
                raise BadRequestError(f"当前对话不支持工具 {name}", details={"tool": name})

            tool_kwargs["tools"] = [
                *tool_kwargs.get("tools", []),
                *SUBTASK_TOOLS,
                *ROUTINE_TOOLS,
                *APPROVAL_RULE_TOOLS,
                *ATTACH_FILE_TOOLS,
                *COMPONENT_CHAT_TOOLS,
            ]
            tool_kwargs["tool_runner"] = _run_bot_tool
            subtask_note = (
                "遇到耗时的长活（调研、批量整理、跨多个步骤的执行）时，优先调用 subtask_start "
                "把它派成后台子任务，立即告诉用户已开始、可以继续聊；不要在本轮等它跑完。"
                "子任务的结果会自动出现在这段对话里，"
                "用户追问进度时可用 subtask_get / subtask_list 查询。"
            )
            routine_note = (
                "用户用自然语言提出定时/重复/一次性/事件触发的工作（例如「每天早上提醒我」"
                "「明天下午三点做一次」「收到 GitHub push 或邮件时处理」）时，"
                "由你直接调用 routine_create / routine_update / routine_pause / "
                "routine_resume / routine_delete 为当前 Bot 管理例程，"
                "不要让用户去设置页面或调用 API 手动配置。创建前先 routine_list，"
                "已有同类例程就更新而不是重复创建；schedule 是 5 段 cron、run_at 是 "
                "ISO 8601 时刻，二者都按 Bot 自己的时区解释且不能同时使用；"
                "只跑一次的用 run_at，不要写成 cron。"
            )
            approval_rule_note = (
                "默认情况下任何操作都不需要审批。用户说「某类操作前先问我」"
                "（例如「支付超过 500 元先问我」「执行 rm 先问我」）时，调用 "
                "approval_rule_add 为当前 Bot 所属用户添加规则；规则对该用户的所有 Bot "
                "生效。Bot 只能新增更严格的规则，不能删除、停用或放宽；"
                "修改前先用 approval_rule_list 查看已有规则，避免重复添加。"
            )
            attach_note = (
                "需要把图片或文件发给用户时，调用 attach_file 登记出站附件：path 必须是 "
                "Bot 工作区内的文件，或提供已上传附件的 upload_id。attach_file 只登记、"
                "不发送；真正发送要等用户在出站审批卡上选择允许。"
            )
            component_note = (
                "需要比纯文本更清楚的展示（步骤、文件清单、进度、可点击按钮）时，调用 "
                "component_emit 发一张交互卡片：必须同时给 component 树和 fallback_text "
                "纯文本兜底。按钮只能用客户端动作 open_desktop / open_url；决策、密钥输入"
                "等需要服务端处理的动作不能自己伪造。"
            )
            existing_note = str(tool_kwargs.get("tools_note") or "").strip()
            notes = [
                note
                for note in (
                    existing_note,
                    subtask_note,
                    routine_note,
                    approval_rule_note,
                    attach_note,
                    component_note,
                )
                if note
            ]
            tool_kwargs["tools_note"] = "\n".join(notes)
        try:
            # 任务选了自己的模型时，这一轮对话也用它回答（与后续 Agent 执行同一通道）
            channel = await container.key_vault.channel_for_task(task)
            assistant_turn = await container.assistant.respond(
                description=description if description is not None else task.description,
                message=prompt_text,
                history=[
                    ChatMessage(role=m.role, content=m.content)
                    for m in history
                    if m.kind in CONTEXT_MESSAGE_KINDS and m.id != user_message.id
                ],
                context=context,
                on_delta=_on_delta,
                channel=channel,
                model=(task.model or "").strip(),
                **tool_kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - 先通知正在看的客户端，再按原样抛给调用方
            await container.task_stream.publish(
                task.id,
                "assistant_error",
                _meta({"error": str(exc)[:500]}, stream_meta),
            )
            raise

        reply_payload = assistant_turn.payload()
        if outbound_files:
            # 出站附件只记录元数据（upload_id / 文件名 / mime / 大小）；
            # _track_turn 从这条回复消息生成待审批草稿，附件本体仍在 attachment_store。
            reply_payload["attachments"] = attachment_reply_payload(outbound_files)
        reply = (
            await container.task_repo.add_messages(
                task.id,
                [
                    MessageDraft(
                        role=TaskMessageRole.ASSISTANT.value,
                        # 需要用户填密钥时，气泡本身就是那张安全输入卡（正文仍是回复文案）
                        kind="secure_input" if assistant_turn.secure_input else "text",
                        content=assistant_turn.reply,
                        payload=reply_payload,
                    )
                ],
            )
        )[0]
        await container.task_stream.publish(
            task.id,
            "assistant_message",
            _meta({"message": reply.to_dict()}, stream_meta),
        )

        if assistant_turn.confirmation:
            # 助手每给出一次确认，就刷新任务上的确认卡（P0-06）。
            task = await container.task_repo.set_confirmation(
                task.id,
                {
                    "items": assistant_turn.confirmation,
                    "actor": "assistant",
                    "updated_at": _utcnow().isoformat(),
                },
            )

        if advance_status and task.status == TaskStatus.PENDING.value:
            # 平台已真实开始处理这条任务：状态从 pending 进入 running（有实际动作才改状态）
            task = await container.task_repo.set_status(task.id, TaskStatus.RUNNING)

        if record_billing:
            await self._record_usage(
                task,
                assistant_turn,
                run_id=billing_run_id or f"bot-turn:{user_message.id}",
            )

        return TaskTurnResult(
            task=task,
            user_message=user_message,
            reply_message=reply,
            assistant_turn=assistant_turn,
        )

    async def run_turn(
        self,
        task: Task,
        *,
        content: str,
        files: list[UploadedFile] | None = None,
        account_key: str = "",
        **kwargs: Any,
    ) -> TaskTurnResult:
        """一次完整 turn：用户消息 + 助手回复（legacy 端点的入口）。"""
        stream_meta = kwargs.pop("stream_meta", None)
        user_message = await self.begin_user_turn(
            task, content=content, files=files, stream_meta=stream_meta
        )
        return await self.complete_turn(
            task,
            user_message=user_message,
            content=content,
            files=files,
            account_key=account_key,
            stream_meta=stream_meta,
            **kwargs,
        )

    # --- 内部：对话任务 / 队列 / worker ---

    async def _chat_task(self, bot: Bot, cloud_user_id: str | None = None) -> Task:
        """取「当前用户 × 这个 Bot」的对话任务；没有就新建一条普通 tasks 行。

        Phase 4 起对话任务按人隔离：同一个人复用自己最近一条未结束的 chat 任务，
        另一个人会拿到自己的新任务。`bots.chat_task_id` 仅作为主用户/历史兼容指针，
        绝不会被指向另一个用户的任务。
        """
        container = self._container
        owner = self._owner()
        current = self._current(cloud_user_id)
        task: Task | None = None
        if current is not None:
            task = await container.task_repo.find_active_chat_task(
                bot.id,
                cloud_user_id=current,
                owner_cloud_user_id=owner,
            )
        elif bot.chat_task_id is not None:
            # 无法解析人（未配置主用户、也没有登录态）：保持旧行为，只复用已有指针。
            try:
                legacy = await container.task_repo.get(bot.chat_task_id)
            except NotFoundError:
                legacy = None
            if legacy is not None and legacy.status not in _FINAL_TASK_STATUSES:
                task = legacy
        if task is not None:
            if current == owner and bot.chat_task_id != task.id:
                # 指针原来指向别人的/已过期的任务时纠正回主用户自己的任务。
                await container.bots.set_chat_task_id(bot.id, task.id)
                bot.chat_task_id = task.id
            return task

        description = (bot.description or "").strip() or bot.name
        task = await container.task_repo.create(
            task_id=uuid.uuid4(),
            description=description,
            priority=100,
            project_id=None,
            agent_id=bot.agent_id,
            decision_mode=DecisionMode(bot.decision_mode or DecisionMode.AUTO),
            permission_mode=PermissionMode(bot.permission_mode or PermissionMode.UNATTENDED),
            model=bot.model,
            api_key_id=bot.api_key_id,
            origin=TaskOrigin.CHAT,
            bot_name=bot.name,
            avatar_shape=bot.avatar_shape,
            avatar_color=bot.avatar_color,
            bot_id=bot.id,
            workspace=str(container.bots.workspace_path(bot)),
            cloud_user_id=current,
        )
        if current is None or current == owner:
            await container.bots.set_chat_task_id(bot.id, task.id)
            bot.chat_task_id = task.id
        return task

    def _enqueue(self, pending: _PendingTurn) -> None:
        queue = self._queues.get(pending.bot_id)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[pending.bot_id] = queue
        queue.put_nowait(pending)
        worker = self._workers.get(pending.bot_id)
        if worker is None or worker.done():
            worker = asyncio.create_task(self._worker(pending.bot_id), name="flux-bot-turn")
            self._workers[pending.bot_id] = worker

            def _cleanup(done: asyncio.Task[None], bot_id: uuid.UUID = pending.bot_id) -> None:
                self._background.discard(done)
                if self._workers.get(bot_id) is done:
                    self._workers.pop(bot_id, None)

            self._background.add(worker)
            worker.add_done_callback(_cleanup)

    async def _worker(self, bot_id: uuid.UUID) -> None:
        queue = self._queues[bot_id]
        while True:
            pending = await queue.get()
            try:
                await self._process(pending)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单轮失败不拖垮该 Bot 后续轮次
                logger.exception("Bot %s 对话轮次处理失败", bot_id)
                await self._fail(pending.turn_id, str(exc), "internal_error")
            finally:
                queue.task_done()

    async def _process(self, pending: _PendingTurn) -> None:
        container = self._container
        turn = await self._turns.get_by_id(pending.turn_id)
        if turn is None or turn.status != "queued":
            return
        try:
            bot = await container.bots.get(pending.bot_id)
        except NotFoundError:
            await self._fail(pending.turn_id, f"Bot {pending.bot_id} 已不存在", "bot_not_found")
            return

        blocked = await container.bots.budget_block(bot)
        if blocked is not None:
            budget, used = blocked
            await self._fail(
                pending.turn_id,
                f"Bot {bot.name} 今日 token 已用 {used}，达到上限 {budget}",
                "budget_exhausted",
            )
            return

        try:
            task = await container.task_repo.get(turn.task_id)
        except NotFoundError:
            await self._fail(pending.turn_id, f"对话任务 {turn.task_id} 已不存在", "task_not_found")
            return

        meta = {"turn_id": str(turn.id)}
        # 本轮的人格：绑定的 IM 发送者 / API 登录人。没有则保持 NULL，
        # 由 `complete_turn` 按历史口径回退到主用户。
        turn_cloud_user_id = pending.cloud_user_id or turn.cloud_user_id
        files = list(pending.files)
        # 只有附件没有正文时，用户消息与记忆检索都用附件说明，避免落一条空消息。
        prompt_content = pending.content or attachments_brief(files)
        self._active_turns[pending.bot_id] = turn.id
        try:
            await self._turns.set_running(turn.id)
            await self._notify(turn.id)

            memory = await container.bot_memories.render_block(
                bot, group_id=None, query=prompt_content, limit=_MEMORY_LIMIT
            )
            # 三层记忆按本轮的人格注入：Environment（平台规则）+ 该用户的 User 层
            # + 该用户所在共享空间的 Shared 层。绝不注入未按人隔离的全局画像/日志。
            user_memory = await self._memory_block(turn_cloud_user_id)
            # 技能库注入：与记忆并列，失败只降级为「没有技能块」。
            skills = await self._skills_block(bot)
            context_parts = [part for part in (memory, user_memory, skills) if part]
            description = (bot.description or "").strip() or bot.name
            user_message = await self.begin_user_turn(
                task,
                content=prompt_content,
                files=files,
                stream_meta=meta,
            )
            await self._turns.set_user_message(turn.id, user_message.id)
            await self._notify(turn.id)
            result = await self.complete_turn(
                task,
                user_message=user_message,
                content=pending.content,
                files=files,
                description=description,
                context="\n".join(context_parts) or None,
                account_key=turn_cloud_user_id or "",
                subtask_bot=bot,
                advance_status=False,
                stream_meta=meta,
                record_billing=True,
                billing_run_id=f"bot-turn:{turn.id}",
            )
            await self._turns.complete(turn.id, result.reply_message.id)
            await self._notify(turn.id, terminal=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 失败要落成可查询的 turn 状态
            logger.warning("Bot %s turn %s 执行失败：%s", bot.id, turn.id, exc)
            await self._fail(turn.id, str(exc), "assistant_error")
        finally:
            self._active_turns.pop(pending.bot_id, None)

    async def _memory_block(self, cloud_user_id: str | None) -> str:
        """按本轮人格读取记忆（Environment + User + 共享空间 + 本人的画像/日志）。

        读取失败只降级为「没有记忆块」，不影响本轮对话。没有可解析的人时，
        既有口径是回退到配置的主用户（兼容历史行为）；画像/日志同样只注入
        那个人的条目，别人的私有画像不会出现在这里。
        """
        parts: list[str] = []
        memory = getattr(self._container, "memory", None)
        if memory is not None:
            try:
                data = await memory.recall(cloud_user_id=cloud_user_id)
                content = str(data.get("content") or "")
                if content:
                    parts.append(content)
            except Exception:  # noqa: BLE001 - 记忆只是上下文增强，不能打断 turn
                logger.warning("Bot 记忆渲染失败，本轮仅使用 Bot 记忆", exc_info=True)
        shared_user = getattr(self._container, "shared_user_memory", None)
        if shared_user is not None:
            try:
                block = await shared_user.render_block(cloud_user_id=cloud_user_id)
                if block:
                    parts.append(block.rstrip())
            except Exception:  # noqa: BLE001 - 画像/日志同样只是上下文增强
                logger.warning("用户画像/日志渲染失败，本轮不注入", exc_info=True)
        return "\n\n".join(parts)

    async def _skills_block(self, bot: Bot) -> str:
        """读取分配给该 Bot 的启用技能；任何失败都降级为空串，不影响本轮对话。"""
        service = getattr(self._container, "skills", None)
        if service is None:
            return ""
        try:
            return await service.render_block(bot.id)
        except Exception:  # noqa: BLE001 - 技能只是上下文增强，失败不能打断 turn
            logger.warning("Bot %s 技能渲染失败，本轮不注入技能", bot.id, exc_info=True)
            return ""

    async def _fail(self, turn_id: uuid.UUID, error: str, error_code: str) -> None:
        try:
            await self._turns.fail(turn_id, error=error, error_code=error_code)
        except Exception:  # noqa: BLE001 - turn 可能已被恢复流程收尾
            logger.warning("Bot turn %s 标记失败时出错", turn_id, exc_info=True)
        await self._notify(turn_id, terminal=True)

    def _drop_signal_state(self, turn_id: uuid.UUID) -> None:
        self._conditions.pop(turn_id, None)
        self._revisions.pop(turn_id, None)
        self._terminal_turns.discard(turn_id)

    async def _notify(self, turn_id: uuid.UUID, *, terminal: bool = False) -> None:
        condition = self._conditions.setdefault(turn_id, asyncio.Condition())
        async with condition:
            self._revisions[turn_id] = self._revisions.get(turn_id, 0) + 1
            condition.notify_all()
        if not terminal:
            return
        self._terminal_turns.add(turn_id)
        # 没有 waiter 时通知者负责释放；有 waiter 时由最后一个离开的 waiter 释放。
        if self._waiters.get(turn_id, 0) == 0:
            self._drop_signal_state(turn_id)

    async def _record_usage(
        self, task: Task, assistant_turn: AssistantTurn, *, run_id: str
    ) -> None:
        usage = assistant_turn.usage or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        if not (input_tokens or output_tokens):
            return
        # 用聊天任务 id 归因：tokens_by_bot_since 经 tasks.bot_id 聚合，预算才能看见这一轮。
        await self._container.billing.record_step(
            run_id=run_id,
            model=assistant_turn.model or (task.model or ""),
            provider=assistant_turn.provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            task_id=str(task.id),
        )


__all__ = [
    "BotConversationService",
    "TaskTurnResult",
    "attachments_brief",
    "record_attachments",
]
