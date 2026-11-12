"""Bot 生命周期编排：创建 / 改配置 / 手动开工 / 定时开工。

设计要点（与对话模式的唯一区别是生命周期）：
- 每次开工都是一条普通 `tasks` 记录（`tasks.bot_id` 指回 Bot），执行、计费、消息
  全部复用既有链路；
- 工作目录独立（`tasks.workspace` 快照），CLI runtime 据此把子进程 cwd 放进 Bot 目录；
- 定时靠落库的 `next_run_at`：后台循环按到期取 Bot，重启后从库里恢复，不丢计划；
- 同一时刻只允许一轮（上一轮未终态时跳过，防止定时撞车）。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flux.core.agent_runtime.runtimes.base import DEFAULT_RUNTIME
from flux.core.bot.cron import CronSchedule, parse_cron
from flux.core.bot.notification_repository import BotNotificationRepository
from flux.core.bot.repository import BotRepository
from flux.core.bot.routine_repository import BotRoutineRepository
from flux.core.event.bus import WILDCARD, Events
from flux.core.group.avatar import normalize_color, normalize_shape
from flux.core.mcp.tools import ALL_TOOLS, FORBIDDEN_TOOL_NAMES, TOOLS
from flux.core.virtual_workspace.path_guard import within_root
from flux.enums import BotStatus, DecisionMode, PermissionMode, TaskStatus
from flux.errors import BadRequestError, ConflictError, NotFoundError
from flux.logging import get_logger
from flux.models.bot import Bot
from flux.models.bot_notification import BotNotification
from flux.models.bot_routine import BotRoutine
from flux.models.task import Task

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from flux.container import Container

logger = get_logger(__name__)

__all__ = ["BotService"]

#: 定时开工任务的优先级（与普通对话同档）
_DEFAULT_PRIORITY = 100
#: 已经结束、可以结算健康统计的任务状态
_FINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
)
#: 失败原因落库的最大长度
_MAX_ERROR_CHARS = 2_000
#: 每日 token 预算的上限（留足 PostgreSQL Integer 余量）
_MAX_TOKEN_BUDGET = 1_000_000_000
#: 事件触发的最小间隔（秒）：同一 Bot 不被同一串事件连续打爆
_EVENT_COOLDOWN_SECONDS = 60.0
#: 白名单/触发表单的单条最大长度与条目数
_MAX_LIST_ITEM_CHARS = 512
_MAX_LIST_ITEMS = 64
#: 漏跑补偿策略；skip 与旧行为一致（只开一轮，然后从当前时刻重新计时）。
_MISSED_RUN_POLICIES = frozenset({"skip", "skip_silent", "catch_up"})
_DEFAULT_MISSED_RUN_POLICY = "skip"
#: catch_up 最多连续补跑的错过的触发点；超过则直接跳到当前时刻之后，避免停机数周后雪崩。
_MAX_CATCH_UP_SLOTS = 24
#: 可配置的事件名（Events 里的字符串常量）
_KNOWN_EVENTS = frozenset(
    value for value in vars(Events).values() if isinstance(value, str) and value != WILDCARD
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _day_start(value: datetime, tz: ZoneInfo | None = None) -> datetime:
    """配置时区的自然日起点（本地午夜转换成 UTC），预算窗口据此重置。"""
    zone = tz or ZoneInfo("UTC")
    moment = _as_utc(value) or _utcnow()
    start_local = moment.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    """数据库可能返回 naive datetime（SQLite），统一按 UTC 解读。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


#: 任务结果里可能承载失败原因的几个键（不同 runtime 写法不一）
_REASON_KEYS = ("error", "message", "detail", "reason", "summary")


def _failure_reason(task: Task) -> str:
    """从任务结果里提取可读的失败原因；`tasks` 表没有独立的 error 字段。"""
    result = task.result
    if isinstance(result, dict):
        for key in _REASON_KEYS:
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:_MAX_ERROR_CHARS]
        if result:
            return str(result)[:_MAX_ERROR_CHARS]
    elif isinstance(result, str) and result.strip():
        return result.strip()[:_MAX_ERROR_CHARS]
    return "任务以失败状态结束，未返回原因"


class BotService:
    def __init__(
        self,
        container: Container,
        *,
        interval_seconds: float = 30.0,
        initial_delay_seconds: float = 5.0,
    ) -> None:
        self._container = container
        # cron 与每日预算统一按配置时区解释；Settings 加载时已校验过 IANA 名称。
        self._tz = ZoneInfo(container.settings.timezone)
        self._repo = BotRepository(container.session_factory)  # type: ignore[attr-defined]
        self._routines = BotRoutineRepository(container.session_factory)  # type: ignore[attr-defined]
        self._notifications = BotNotificationRepository(
            container.session_factory  # type: ignore[attr-defined]
        )
        self._interval_seconds = max(5.0, float(interval_seconds))
        self._initial_delay_seconds = max(0.0, float(initial_delay_seconds))
        self._task: asyncio.Task[None] | None = None
        #: 当前有 Bot 订阅的事件名；为空时事件回调直接返回，避免每条事件都查库
        self._trigger_events: set[str] = set()
        #: Bot 事件触发的冷却时间
        self._event_last: dict[uuid.UUID, datetime] = {}
        #: 事件触发协程的强引用，避免 create_task 后被 GC 提前回收
        self._event_jobs: set[asyncio.Task[Any]] = set()
        #: 每个 Bot 的「查忙 + 起一轮」锁：只覆盖开工准备，不覆盖整轮运行
        self._launch_locks: dict[uuid.UUID, asyncio.Lock] = {}

    # --- 查询 ---

    async def list(self, *, status: str | None = None, limit: int = 100) -> list[Bot]:
        return await self._repo.list(status=status, limit=limit)

    async def get(self, bot_id: uuid.UUID) -> Bot:
        return await self._repo.get(bot_id)

    async def tokens_used_today(self, *, now: datetime | None = None) -> dict[uuid.UUID, int]:
        """每个 Bot 今天（配置时区的自然日）已用的 token；账本按 `tasks.bot_id` 归因。"""
        return await self._container.billing.tokens_by_bot_since(
            _day_start(now or _utcnow(), self._tz)
        )

    async def budget_block(
        self, bot: Bot, *, now: datetime | None = None
    ) -> tuple[int, int] | None:
        """预算是否已用完：返回 (上限, 今日已用)，无上限或未超额返回 None。

        对话轮次与定时/手动开工共用同一份预算口径，避免两套账。
        """
        return await self._budget_block(bot, now=_as_utc(now) or _utcnow())

    async def set_chat_task_id(self, bot_id: uuid.UUID, task_id: uuid.UUID) -> Bot:
        """记住 Bot 的唯一对话任务（懒创建后回写，供后续轮次复用）。"""
        return await self._repo.update(bot_id, {"chat_task_id": task_id})

    # --- 配置 ---

    async def create(
        self,
        *,
        name: str,
        description: str = "",
        avatar_shape: str | None = None,
        avatar_color: str | None = None,
        agent_id: str | None = None,
        model: str | None = None,
        key_id: str | None = None,
        permission_mode: PermissionMode = PermissionMode.NORMAL,
        decision_mode: DecisionMode = DecisionMode.AUTO,
        workspace: str | None = None,
        schedule: str | None = None,
        timezone: str | None = None,
        missed_run_policy: str | None = None,
        failure_threshold: int = 3,
        token_budget: int | None = None,
        notify_on_failure: bool = True,
        event_triggers: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        allowed_commands: list[str] | None = None,
        allowed_paths: list[str] | None = None,
    ) -> Bot:
        cleaned = (name or "").strip()
        if not cleaned:
            raise BadRequestError("Bot 名称不能为空")
        agent_uuid = self._agent_uuid(agent_id)
        resolved_model, key_uuid = await self._resolve_model(agent_uuid, model=model, key_id=key_id)
        bot_timezone = self._clean_timezone(timezone)
        policy = self._missed_policy(missed_run_policy)
        cron = self._cron(schedule, tz=self._zone(bot_timezone))
        now = _utcnow()
        bot = await self._repo.create(
            name=cleaned,
            description=description,
            avatar_shape=normalize_shape(avatar_shape),
            avatar_color=normalize_color(avatar_color),
            agent_id=agent_uuid,
            model=resolved_model,
            api_key_id=key_uuid,
            permission_mode=str(permission_mode),
            decision_mode=str(decision_mode),
            workspace=self._clean_workspace(workspace),
            status=BotStatus.ACTIVE.value,
            schedule=cron.expression if cron else None,
            next_run_at=cron.next_after(now) if cron else None,
            timezone=bot_timezone,
            missed_run_policy=policy,
            failure_threshold=self._threshold(failure_threshold),
            token_budget=self._budget(token_budget),
            notify_on_failure=bool(notify_on_failure),
            event_triggers=self._event_list(event_triggers),
            allowed_tools=self._tool_list(allowed_tools),
            allowed_commands=self._string_list(allowed_commands, "allowed_commands"),
            allowed_paths=self._path_list(allowed_paths),
        )
        self._remember_triggers(bot)
        return bot

    async def update(self, bot_id: uuid.UUID, changes: dict[str, Any]) -> Bot:
        """部分更新：`changes` 只含调用方显式给出的字段（API 层用 exclude_unset）。"""
        bot = await self._repo.get(bot_id)
        values: dict[str, Any] = {}

        if "name" in changes:
            name = (changes["name"] or "").strip()
            if not name:
                raise BadRequestError("Bot 名称不能为空")
            values["name"] = name[:64]
        if "description" in changes:
            values["description"] = (changes["description"] or "").strip()

        if "avatar_shape" in changes:
            values["avatar_shape"] = normalize_shape(changes["avatar_shape"])
        if "avatar_color" in changes:
            values["avatar_color"] = normalize_color(changes["avatar_color"])

        agent_changed = "agent_id" in changes
        if agent_changed:
            agent_uuid = self._agent_uuid(changes["agent_id"])
            values["agent_id"] = agent_uuid
            # 与任务切换 Agent 同一口径：旧 Agent 的模型/密钥不一定在新 Agent 上可用
            values["model"] = None
            values["api_key_id"] = None
        elif "model" in changes or "key_id" in changes:
            model, key_uuid = await self._resolve_model(
                bot.agent_id,
                model=changes.get("model", bot.model),
                key_id=changes.get("key_id", str(bot.api_key_id) if bot.api_key_id else None),
            )
            values["model"] = model
            values["api_key_id"] = key_uuid

        if "permission_mode" in changes:
            raw = changes["permission_mode"]
            values["permission_mode"] = str(raw or PermissionMode.NORMAL)
        if "decision_mode" in changes:
            raw = changes["decision_mode"]
            values["decision_mode"] = str(raw or DecisionMode.AUTO)
        if "workspace" in changes:
            values["workspace"] = self._clean_workspace(changes["workspace"])
        if "failure_threshold" in changes:
            values["failure_threshold"] = self._threshold(changes["failure_threshold"])
        if "token_budget" in changes:
            values["token_budget"] = self._budget(changes["token_budget"])
        if "notify_on_failure" in changes:
            values["notify_on_failure"] = bool(changes["notify_on_failure"])
        if "event_triggers" in changes:
            values["event_triggers"] = self._event_list(changes["event_triggers"])
        if "allowed_tools" in changes:
            values["allowed_tools"] = self._tool_list(changes["allowed_tools"])
        if "allowed_commands" in changes:
            values["allowed_commands"] = self._string_list(
                changes["allowed_commands"], "allowed_commands"
            )
        if "allowed_paths" in changes:
            values["allowed_paths"] = self._path_list(changes["allowed_paths"])

        timezone_changed = "timezone" in changes
        bot_timezone = (
            self._clean_timezone(changes["timezone"]) if timezone_changed else bot.timezone
        )
        if timezone_changed:
            values["timezone"] = bot_timezone
        if "missed_run_policy" in changes:
            values["missed_run_policy"] = self._missed_policy(changes["missed_run_policy"])

        schedule_changed = "schedule" in changes
        zone = self._zone(bot_timezone)
        cron = self._cron(bot.schedule, tz=zone)
        if schedule_changed:
            cron = self._cron(changes["schedule"], tz=zone)
            values["schedule"] = cron.expression if cron else None

        status_changed = changes.get("status") is not None
        status = BotStatus(changes["status"]) if status_changed else BotStatus(bot.status)
        if status_changed:
            values["status"] = status.value
        if status == BotStatus.PAUSED:
            values["next_run_at"] = None
        elif schedule_changed or timezone_changed or status_changed:
            values["next_run_at"] = cron.next_after(_utcnow()) if cron else None
            if status_changed:
                # 手动恢复 = 重新计时：熔断计数与自动暂停原因一起清掉
                values["consecutive_failures"] = 0
                values["paused_reason"] = None

        if not values:
            return bot
        updated = await self._repo.update(bot_id, values)
        if timezone_changed:
            await self._reschedule_routines(updated)
        if "event_triggers" in values:
            self._remember_triggers(updated)
        return updated

    async def delete(self, bot_id: uuid.UUID) -> None:
        await self._repo.delete(bot_id)
        await self._refresh_triggers()

    # --- 例程 ---

    async def list_routines(self, bot_id: uuid.UUID) -> list[BotRoutine]:
        await self._repo.get(bot_id)
        return await self._routines.list_for_bot(bot_id)

    async def create_routine(
        self,
        bot_id: uuid.UUID,
        *,
        name: str,
        instruction: str = "",
        schedule: str | None = None,
        enabled: bool = True,
    ) -> BotRoutine:
        bot = await self._repo.get(bot_id)
        cleaned = (name or "").strip()
        if not cleaned:
            raise BadRequestError("例程名称不能为空")
        cron = self._cron(schedule, tz=self._zone_for(bot))
        return await self._routines.create(
            bot_id=bot_id,
            name=cleaned[:64],
            instruction=(instruction or "").strip()[:8000],
            schedule=cron.expression if cron else None,
            next_run_at=cron.next_after(_utcnow()) if cron and enabled else None,
            enabled=enabled,
        )

    async def update_routine(
        self, bot_id: uuid.UUID, routine_id: uuid.UUID, changes: dict[str, Any]
    ) -> BotRoutine:
        routine = await self._routines.get(routine_id)
        if routine.bot_id != bot_id:
            raise NotFoundError(
                f"例程 {routine_id} 不属于 Bot {bot_id}", details={"routine_id": str(routine_id)}
            )
        bot = await self._repo.get(bot_id)
        zone = self._zone_for(bot)
        values: dict[str, Any] = {}
        if "name" in changes:
            name = (changes["name"] or "").strip()
            if not name:
                raise BadRequestError("例程名称不能为空")
            values["name"] = name[:64]
        if "instruction" in changes:
            values["instruction"] = (changes["instruction"] or "").strip()[:8000]
        if "schedule" in changes:
            values["schedule"] = (
                self._cron(changes["schedule"], tz=zone).expression if changes["schedule"] else None
            )
        if "enabled" in changes:
            values["enabled"] = bool(changes["enabled"])

        if "schedule" in values or "enabled" in values:
            enabled = bool(values.get("enabled", routine.enabled))
            schedule = values.get("schedule", routine.schedule)
            cron = self._cron(schedule, tz=zone)
            values["next_run_at"] = cron.next_after(_utcnow()) if cron and enabled else None
        if not values:
            return routine
        return await self._routines.update(routine_id, values)

    async def _reschedule_routines(self, bot: Bot) -> None:
        """Bot 时区变更后，启用中的例程从当前时刻按新时区重新计时。"""
        zone = self._zone_for(bot)
        now = _utcnow()
        for routine in await self._routines.list_for_bot(bot.id):
            if not routine.enabled:
                continue
            cron = self._cron(routine.schedule, tz=zone)
            if cron is None:
                continue
            await self._routines.update(routine.id, {"next_run_at": cron.next_after(now)})

    async def delete_routine(self, bot_id: uuid.UUID, routine_id: uuid.UUID) -> None:
        routine = await self._routines.get(routine_id)
        if routine.bot_id != bot_id:
            raise NotFoundError(
                f"例程 {routine_id} 不属于 Bot {bot_id}", details={"routine_id": str(routine_id)}
            )
        await self._routines.delete(routine_id)

    # --- 通知 ---

    async def list_notifications(
        self, *, unread_only: bool = False, limit: int = 100
    ) -> list[BotNotification]:
        return await self._notifications.list(unread_only=unread_only, limit=limit)

    async def unread_notification_count(self) -> int:
        return await self._notifications.unread_count()

    async def mark_notification_read(self, notification_id: uuid.UUID) -> BotNotification | None:
        return await self._notifications.mark_read(notification_id)

    async def mark_all_notifications_read(self) -> int:
        return await self._notifications.mark_all_read()

    # --- 技能目录 ---

    def tools_catalog(self) -> list[dict[str, str]]:
        """可选工具清单：直接来自 MCP 工具面，硬禁令不出现在可选项中。"""
        return [
            {"name": tool.name, "title": tool.title, "description": tool.description}
            for tool in ALL_TOOLS
            if tool.name not in FORBIDDEN_TOOL_NAMES
        ]

    # --- 事件触发 ---

    async def handle_event(self, event: str, payload: dict[str, Any]) -> None:
        """事件总线订阅入口：只做轻量筛选，真正的开工放到后台协程。"""
        if event not in self._trigger_events:
            return
        job = asyncio.create_task(self._dispatch_event(event, payload), name="flux-bot-event")
        self._event_jobs.add(job)
        job.add_done_callback(self._event_jobs.discard)

    async def _dispatch_event(self, event: str, payload: dict[str, Any]) -> None:
        moment = _utcnow()
        source_bot = str(payload.get("bot_id") or "")
        summary = self._event_summary(event, payload)
        for bot in await self._repo.list(status=BotStatus.ACTIVE.value, limit=200):
            if event not in (bot.event_triggers or []):
                continue
            # 自己触发的任务事件不再触发自己：否则一轮完成会立刻引发下一轮
            if source_bot and source_bot == str(bot.id):
                continue
            last = self._event_last.get(bot.id)
            if last is not None and (moment - last).total_seconds() < _EVENT_COOLDOWN_SECONDS:
                continue
            if await self._budget_block(bot, now=moment) is not None:
                logger.info("Bot %s 今日 token 已用完，跳过事件 %s", bot.id, event)
                continue
            description = (bot.description or "").strip()
            instruction = f"[事件触发] {event}：{summary}"
            if description:
                instruction = f"{instruction}\n\n{description}"
            try:
                task = await self._launch_if_idle(bot, instruction=instruction, now=moment)
                if task is None:
                    continue
                self._event_last[bot.id] = moment
                logger.info("Bot %s 被事件 %s 触发开工", bot.id, event)
            except Exception as exc:  # noqa: BLE001 - 单个 Bot 失败不影响其他 Bot
                logger.warning("Bot %s 事件触发开工失败：%s", bot.id, exc)

    async def _refresh_triggers(self) -> None:
        events: set[str] = set()
        for bot in await self._repo.list(limit=500):
            events.update(bot.event_triggers or [])
        self._trigger_events = events & _KNOWN_EVENTS

    def _remember_triggers(self, bot: Bot) -> None:
        self._trigger_events.update(
            event for event in (bot.event_triggers or []) if event in _KNOWN_EVENTS
        )

    def _event_summary(self, event: str, payload: dict[str, Any]) -> str:
        for key in ("summary", "description", "title", "path", "batch_id", "task_id"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:_MAX_LIST_ITEM_CHARS]
        return event

    # --- 开工 ---

    async def run_now(self, bot_id: uuid.UUID, *, instruction: str | None = None) -> Task:
        """手动触发一次开工（与定时开工共用同一条链路）。

        手动开工不受 paused 限制：暂停只关闭「自动触发」，用户显式点开工仍然执行。
        """
        bot = await self._repo.get(bot_id)
        bot = await self._settle_previous(bot)
        task = await self._launch_if_idle(
            bot,
            instruction=instruction,
            now=_utcnow(),
            conflict_on_busy=True,
            check_budget=True,
        )
        assert task is not None  # conflict_on_busy 保证要么开工、要么抛 ConflictError
        return task

    async def tick(self, now: datetime | None = None) -> list[str]:
        """扫一轮到期的 Bot，返回本次开工的任务 id。异常不外抛（后台循环不该死）。"""
        moment = _as_utc(now) or _utcnow()
        # 先结算所有终态未结算的 Bot：暂停中的也要结算，跑通一轮才能自愈
        for pending in await self._repo.list_unsettled():
            try:
                await self._settle_previous(pending, now=moment)
            except Exception as exc:  # noqa: BLE001 - 结算失败不影响调度
                logger.warning("Bot %s 结算上一轮失败：%s", pending.id, exc)
        due = await self._repo.list_due(moment)
        started: list[str] = []
        for bot in due:
            try:
                if bot.status != BotStatus.ACTIVE.value or bot.next_run_at is None:
                    continue
                cron = self._cron_for(bot, bot.schedule)
                policy = bot.missed_run_policy or _DEFAULT_MISSED_RUN_POLICY
                if policy == "skip_silent" and cron is not None and bot.next_run_at is not None:
                    # 只在“至少错过一个完整槽位”时静默跳过。仅仅到期几秒
                    # （调度循环的正常延迟）仍然要跑，否则 skip_silent 的 Bot 永远不会自动开工。
                    overdue = _as_utc(bot.next_run_at)
                    if overdue is not None and cron.next_after(overdue) <= moment:
                        await self._repo.update(
                            bot.id,
                            {"next_run_at": cron.next_after(moment)},
                        )
                        logger.info("Bot %s 使用 skip_silent，静默跳过错过的定时", bot.id)
                        continue
                blocked = await self._budget_block(bot, now=moment)
                if blocked is not None:
                    # 预算超了也照样推进下一次触发：否则每轮 tick 都会重新扫到它
                    await self._repo.update(
                        bot.id,
                        {"next_run_at": cron.next_after(moment) if cron else None},
                    )
                    logger.info(
                        "Bot %s 今日 token 已用 %s/%s，跳过本次定时", bot.id, blocked[1], blocked[0]
                    )
                    continue
                task = await self._launch_if_idle(
                    bot,
                    instruction=None,
                    now=moment,
                    catch_up=policy == "catch_up",
                )
                if task is None:
                    # 上一轮还在跑（或刚被其他路径抢到锁开工）：跳过本轮，顺延到下一个触发点
                    await self._repo.update(
                        bot.id,
                        {"next_run_at": cron.next_after(moment) if cron else None},
                    )
                    logger.info("Bot %s 上一轮未结束，跳过本次定时", bot.id)
                    continue
                started.append(str(task.id))
            except Exception as exc:  # noqa: BLE001 - 单个 Bot 失败不影响其他 Bot
                logger.warning("Bot %s 定时开工失败：%s", bot.id, exc)
        # 例程：与 Bot 主定时同一条链路，各自按自己的 cron 推进
        for routine in await self._routines.list_due(moment):
            try:
                task = await self._tick_routine(routine, moment)
                if task is not None:
                    started.append(str(task.id))
            except Exception as exc:  # noqa: BLE001 - 单个例程失败不影响其他例程
                logger.warning("例程 %s 定时开工失败：%s", routine.id, exc)
        return started

    async def _tick_routine(self, routine: BotRoutine, moment: datetime) -> Task | None:
        """一个到期例程：先推进自身 next_run_at，再按 Bot 的可用状态决定是否开工。"""
        try:
            bot = await self._repo.get(routine.bot_id)
        except NotFoundError:
            # Bot 已删除：例程应随外键级联清理，这里只兜底跳过
            cron = self._cron(routine.schedule)
            await self._routines.update(
                routine.id, {"next_run_at": cron.next_after(moment) if cron else None}
            )
            return None
        # 例程没有自己的时区，跟随父 Bot（Bot 未设置则继承 FLUX_TIMEZONE）。
        cron = self._cron_for(bot, routine.schedule)
        next_run_at = cron.next_after(moment) if cron else None
        if bot.status != BotStatus.ACTIVE.value:
            await self._routines.update(routine.id, {"next_run_at": next_run_at})
            return None
        if await self._budget_block(bot, now=moment) is not None:
            await self._routines.update(routine.id, {"next_run_at": next_run_at})
            logger.info("Bot %s 今日 token 已用完，跳过例程 %s", bot.id, routine.name)
            return None

        async def _advance(*, last_run: bool = True) -> None:
            values: dict[str, Any] = {"next_run_at": next_run_at}
            if last_run:
                values["last_run_at"] = moment
            await self._routines.update(routine.id, values)

        async def _skip_busy() -> None:
            await _advance(last_run=False)
            logger.info("Bot %s 上一轮未结束，跳过例程 %s", bot.id, routine.name)

        # 先推进例程时间再起 Run：起 Run 期间再扫到也不会重复开工。
        return await self._launch_if_idle(
            bot,
            instruction=routine.instruction,
            now=moment,
            routine=routine,
            on_busy=_skip_busy,
            before_launch=_advance,
        )

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        # 事件触发：订阅通配事件，是否有 Bot 关心由 _trigger_events 提前筛掉
        await self._refresh_triggers()
        self._container.bus.subscribe(WILDCARD, self.handle_event)
        self._task = asyncio.create_task(self._loop(), name="flux-bot-worker")

    async def stop(self) -> None:
        self._container.bus.unsubscribe(WILDCARD, self.handle_event)
        for job in list(self._event_jobs):
            job.cancel()
        self._event_jobs.clear()
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # --- 内部 ---

    async def _settle_previous(self, bot: Bot, *, now: datetime | None = None) -> Bot:
        """把上一轮开工的终态计入健康统计（同一条任务只结算一次）。

        Bot 没有可靠的完成回调，调度器每轮 tick 顺带结算即可：
        连续失败累积到阈值就把 Bot 自动暂停，避免无意义地反复重试。
        """
        task_id = bot.last_task_id
        if task_id is None or task_id == bot.last_settled_task_id:
            return bot
        try:
            task = await self._container.task_repo.get(task_id)
        except NotFoundError:
            # 历史任务已被清理：只记下已结算，不影响后续调度
            return await self._repo.update(bot.id, {"last_settled_task_id": task_id})
        if task.status not in _FINAL_STATUSES:
            return bot

        values: dict[str, Any] = {
            "last_settled_task_id": task_id,
            "runs_total": int(bot.runs_total or 0) + 1,
        }
        if task.status == TaskStatus.COMPLETED.value:
            values.update(consecutive_failures=0, last_error=None, paused_reason=None)
            if bot.status == BotStatus.PAUSED.value and bot.paused_reason:
                # 自动暂停的 Bot 跑通一轮就自愈；用户手动暂停（无 paused_reason）保持原样
                cron = self._cron_for(bot, bot.schedule)
                values["status"] = BotStatus.ACTIVE.value
                values["next_run_at"] = cron.next_after(now or _utcnow()) if cron else None
        elif task.status == TaskStatus.FAILED.value:
            failures = int(bot.consecutive_failures or 0) + 1
            values.update(
                consecutive_failures=failures,
                runs_failed=int(bot.runs_failed or 0) + 1,
                last_error=_failure_reason(task),
            )
            threshold = int(bot.failure_threshold or 0)
            if threshold > 0 and failures >= threshold and bot.status == BotStatus.ACTIVE.value:
                values.update(
                    status=BotStatus.PAUSED.value,
                    next_run_at=None,
                    paused_reason=f"连续 {failures} 次开工失败，已自动暂停",
                )
                logger.warning("Bot %s 连续失败 %s 次，已自动暂停", bot.id, failures)
        # cancelled 只计入 runs_total：用户主动取消不该触发熔断
        updated = await self._repo.update(bot.id, values)
        if task.status == TaskStatus.FAILED.value and bot.notify_on_failure:
            await self._notify_failure(updated, task)
        return updated

    async def _notify_failure(self, bot: Bot, task: Task) -> None:
        """失败主动通知：通知写不进去也不能影响结算与调度。"""
        try:
            reason = _failure_reason(task)
            title = f"Bot {bot.name} 本轮开工失败"
            if bot.status == BotStatus.PAUSED.value and bot.paused_reason:
                title = f"Bot {bot.name} 已自动暂停"
            await self._notifications.create(
                bot_id=bot.id,
                bot_name=bot.name,
                task_id=task.id,
                level="error",
                title=title,
                body=bot.paused_reason or reason,
            )
        except Exception:  # noqa: BLE001 - 通知失败不打乱健康统计
            logger.warning("Bot %s 失败通知写入失败", bot.id, exc_info=True)

    def workspace_path(self, bot: Bot) -> Path:
        """Bot 的执行目录：配置的绝对路径，或 `<dsh_home>/bots/<id>/workspace`。"""
        if bot.workspace:
            return Path(bot.workspace).expanduser()
        dsh_home = Path(self._container.settings.dsh_home).expanduser()
        return dsh_home / "bots" / str(bot.id) / "workspace"

    async def _launch_if_idle(
        self,
        bot: Bot,
        *,
        instruction: str | None,
        now: datetime,
        routine: BotRoutine | None = None,
        conflict_on_busy: bool = False,
        check_budget: bool = False,
        on_busy: Callable[[], Awaitable[None]] | None = None,
        before_launch: Callable[[], Awaitable[None]] | None = None,
        catch_up: bool = False,
    ) -> Task | None:
        """在 Bot 级锁内重读忙闲，再决定是否开工（check-then-act 必须原子）。

        锁覆盖到任务行已落库、Bot 的 last_task_id 已更新且 `is_busy` 可见为止，
        随后立即释放：整轮运行期间不再占锁，其他入口只需看到「忙」就会跳过。
        """
        lock = self._launch_locks.get(bot.id)
        if lock is None:
            lock = asyncio.Lock()
            self._launch_locks[bot.id] = lock
        async with lock:
            if await self._repo.is_busy(bot.id):
                if conflict_on_busy:
                    raise ConflictError(
                        f"Bot {bot.name} 上一轮还没跑完，等它结束再触发",
                        details={"bot_id": str(bot.id)},
                    )
                if on_busy is not None:
                    await on_busy()
                return None
            if check_budget:
                blocked = await self._budget_block(bot, now=now)
                if blocked is not None:
                    budget, used = blocked
                    if conflict_on_busy:
                        raise ConflictError(
                            f"Bot {bot.name} 今日 token 已用 {used}，达到上限 {budget}；"
                            "调高预算或等明天再跑",
                            details={
                                "bot_id": str(bot.id),
                                "token_budget": budget,
                                "tokens_used_today": used,
                            },
                        )
                    return None
            if before_launch is not None:
                await before_launch()
            return await self._launch(
                bot, instruction=instruction, now=now, routine=routine, catch_up=catch_up
            )

    async def _launch(
        self,
        bot: Bot,
        *,
        instruction: str | None,
        now: datetime,
        routine: BotRoutine | None = None,
        catch_up: bool = False,
    ) -> Task:
        prompt = (instruction or "").strip() or (bot.description or "").strip()
        if not prompt:
            raise BadRequestError(
                f"Bot {bot.name} 还没有作业内容：先写 description，或手动触发时带上 instruction",
                details={"bot_id": str(bot.id)},
            )
        cron = self._cron_for(bot, bot.schedule) if routine is None else None
        if cron is None:
            next_run_at = None
        elif catch_up:
            next_run_at = self._catch_up_next(cron, _as_utc(bot.next_run_at), now)
        else:
            next_run_at = cron.next_after(now)
        workspace = str(self.workspace_path(bot))
        container = self._container
        task = await container.task_repo.create(
            task_id=uuid.uuid4(),
            description=prompt,
            priority=_DEFAULT_PRIORITY,
            project_id=None,
            agent_id=bot.agent_id,
            decision_mode=DecisionMode(bot.decision_mode or DecisionMode.AUTO),
            permission_mode=PermissionMode(bot.permission_mode or PermissionMode.NORMAL),
            model=bot.model,
            api_key_id=bot.api_key_id,
            bot_name=bot.name,
            avatar_shape=bot.avatar_shape,
            avatar_color=bot.avatar_color,
            bot_id=bot.id,
            workspace=workspace,
        )
        # 先推进 next_run_at 再起 Run：起 Run 期间调度器再扫到也不会重复开工。
        # 例程触发时 Bot 主定时的 next_run_at 不动，只更新最近开工信息。
        if routine is None:
            await self._repo.mark_run(
                bot.id, task_id=task.id, started_at=now, next_run_at=next_run_at
            )
        else:
            await self._repo.update(bot.id, {"last_task_id": task.id, "last_run_at": now})
        try:
            await container.task_run_service.start(task)
        except Exception:
            # 起不来就把任务收成 failed：任务停在 pending 会让 Bot 永远「忙」
            await container.task_repo.set_status(task.id, TaskStatus.FAILED)
            logger.warning("Bot %s 第 %s 轮起 Run 失败", bot.id, task.id, exc_info=True)
            raise
        return task

    async def _loop(self) -> None:
        if self._initial_delay_seconds:
            await asyncio.sleep(self._initial_delay_seconds)
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - 循环本身不因单轮失败退出
                logger.warning("Bot 调度循环本轮失败", exc_info=True)
            await asyncio.sleep(self._interval_seconds)

    def _cron(
        self, expression: str | None, *, tz: ZoneInfo | str | None = None
    ) -> CronSchedule | None:
        """解析 cron；空表示只手动触发。非法表达式在写入口就按 400 拒绝。"""
        text = (expression or "").strip()
        if not text:
            return None
        try:
            return parse_cron(text, tz=tz or self._tz)
        except ValueError as exc:
            raise BadRequestError(
                f"schedule 不是合法的 cron：{exc}", details={"schedule": text}
            ) from exc

    def _cron_for(self, bot: Bot, expression: str | None) -> CronSchedule | None:
        """按 Bot 自己的时区解析 cron；未设置时继承进程级 FLUX_TIMEZONE。"""
        return self._cron(expression, tz=self._zone_for(bot))

    def _zone(self, timezone: str | None) -> ZoneInfo:
        if not timezone:
            return self._tz
        try:
            return ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return self._tz

    def _zone_for(self, bot: Bot) -> ZoneInfo:
        """Bot 时区；字段为空时用进程配置时区（在 __init__ 已校验过）。"""
        if not bot.timezone:
            return self._tz
        try:
            return ZoneInfo(bot.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            # 历史数据里出现非法时区时不能让调度循环崩掉，退回进程配置。
            logger.warning("Bot %s 的时区 %r 非法，回退到 FLUX_TIMEZONE", bot.id, bot.timezone)
            return self._tz

    def _clean_timezone(self, value: str | None) -> str | None:
        """空值 = 继承（NULL）；非空必须是合法的 IANA 时区名。"""
        text = (value or "").strip()
        if not text:
            return None
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise BadRequestError(
                f"timezone 不是合法的 IANA 时区：{text}", details={"timezone": text}
            ) from exc
        return text

    def _missed_policy(self, value: str | None) -> str:
        text = (value or "").strip() or _DEFAULT_MISSED_RUN_POLICY
        if text not in _MISSED_RUN_POLICIES:
            raise BadRequestError(
                f"missed_run_policy 必须是 {sorted(_MISSED_RUN_POLICIES)} 之一",
                details={"missed_run_policy": text},
            )
        return text

    def _catch_up_next(
        self, cron: CronSchedule, overdue: datetime | None, now: datetime
    ) -> datetime:
        """catch_up 的下一跳：从错过的触发点继续，但最多补 24 个槽位。"""
        candidate = cron.next_after(overdue or now)
        if candidate > now:
            return candidate
        cursor = candidate
        missed = 0
        while cursor <= now and missed <= _MAX_CATCH_UP_SLOTS:
            missed += 1
            cursor = cron.next_after(cursor)
        if missed > _MAX_CATCH_UP_SLOTS:
            logger.warning(
                "Bot 错过超过 %s 个触发点，catch_up 跳过错过的槽位，从当前时刻重新计时",
                _MAX_CATCH_UP_SLOTS,
            )
            return cron.next_after(now)
        return candidate

    def _clean_workspace(self, workspace: str | None) -> str | None:
        text = (workspace or "").strip()
        if not text:
            return None
        path = Path(text).expanduser()
        if not path.is_absolute():
            raise BadRequestError("workspace 必须是绝对路径", details={"workspace": text})
        root_text = (self._container.settings.workspace_root or "").strip()
        if not root_text:
            raise BadRequestError(
                "未配置 FLUX_WORKSPACE_ROOT，不能使用自定义工作目录；"
                "请先为服务设置 FLUX_WORKSPACE_ROOT",
                details={"workspace": text},
            )
        resolved = path.resolve()
        root = Path(root_text).expanduser().resolve()
        if not within_root(root, resolved):
            raise BadRequestError(
                "workspace 必须位于 FLUX_WORKSPACE_ROOT 之内",
                details={"workspace": str(resolved), "workspace_root": str(root)},
            )
        return str(resolved)

    def _threshold(self, value: int | None) -> int:
        """连续失败阈值：0 表示永不自动暂停，范围 0~20。"""
        if value is None:
            return 3
        if isinstance(value, bool) or not isinstance(value, int):
            raise BadRequestError("failure_threshold 必须是整数", details={"value": str(value)})
        if value < 0 or value > 20:
            raise BadRequestError(
                "failure_threshold 超出范围（0~20）", details={"failure_threshold": value}
            )
        return value

    def _budget(self, value: int | None) -> int | None:
        """每日 token 预算：None / 0 = 无上限，正数即当日上限。"""
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise BadRequestError("token_budget 必须是整数", details={"value": str(value)})
        if value < 0:
            raise BadRequestError("token_budget 不能为负数", details={"token_budget": value})
        if value > _MAX_TOKEN_BUDGET:
            raise BadRequestError(
                "token_budget 过大",
                details={"token_budget": value, "max": _MAX_TOKEN_BUDGET},
            )
        return value or None

    async def _budget_block(self, bot: Bot, *, now: datetime) -> tuple[int, int] | None:
        """预算是否已用完：返回 (上限, 今日已用)，无上限或未超额返回 None。"""
        budget = int(bot.token_budget or 0)
        if budget <= 0:
            return None
        used = (await self.tokens_used_today(now=now)).get(bot.id, 0)
        return (budget, used) if used >= budget else None

    def _string_list(
        self, value: Any, field: str, *, allowed: set[str] | None = None
    ) -> list[str] | None:
        """字符串白名单字段：None / 空数组都表示「不额外限制」。"""
        if value is None:
            return None
        if not isinstance(value, (list, tuple)):
            raise BadRequestError(f"{field} 必须是字符串数组", details={field: str(value)})
        result: list[str] = []
        for raw in value:
            if not isinstance(raw, str):
                raise BadRequestError(f"{field} 必须是字符串数组", details={field: str(raw)})
            item = raw.strip()
            if not item:
                continue
            if len(item) > _MAX_LIST_ITEM_CHARS:
                raise BadRequestError(
                    f"{field} 单项过长（上限 {_MAX_LIST_ITEM_CHARS}）", details={field: item[:64]}
                )
            if allowed is not None and item not in allowed:
                raise BadRequestError(f"{field} 含不支持的值：{item}", details={field: item[:64]})
            if item not in result:
                result.append(item)
        if len(result) > _MAX_LIST_ITEMS:
            raise BadRequestError(
                f"{field} 条目过多（上限 {_MAX_LIST_ITEMS}）", details={field: len(result)}
            )
        return result or None

    def _event_list(self, value: Any) -> list[str] | None:
        return self._string_list(value, "event_triggers", allowed=set(_KNOWN_EVENTS))

    def _tool_list(self, value: Any) -> list[str] | None:
        allowed = frozenset(TOOLS) - FORBIDDEN_TOOL_NAMES
        return self._string_list(value, "allowed_tools", allowed=set(allowed))

    def _path_list(self, value: Any) -> list[str] | None:
        """路径白名单一律相对工作目录：绝对路径直接拒绝，避免绕过目录约束。"""
        items = self._string_list(value, "allowed_paths")
        if not items:
            return None
        cleaned: list[str] = []
        for item in items:
            text = item.replace("\\", "/").strip()
            if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
                raise BadRequestError(
                    "allowed_paths 必须是相对工作目录的路径", details={"allowed_paths": item}
                )
            text = text.removeprefix("./").rstrip("/") or "."
            if text not in cleaned:
                cleaned.append(text)
        return cleaned or None

    def _agent_uuid(self, agent_id: str | uuid.UUID | None) -> uuid.UUID | None:
        """Agent 注册表是存在性的唯一权威：非法标识与不存在的 Agent 都是 404。"""
        if not agent_id:
            return None
        return self._container.agents.get(agent_id).id

    async def _resolve_model(
        self,
        agent_id: uuid.UUID | None,
        *,
        model: str | None,
        key_id: str | None,
    ) -> tuple[str | None, uuid.UUID | None]:
        """与建任务同一套模型/密钥校验；Agent 决定 runtime 原生模型目录。"""
        runtime = DEFAULT_RUNTIME
        if agent_id is not None:
            with contextlib.suppress(Exception):
                agent = self._container.agents.get(agent_id)
                runtime = str(getattr(getattr(agent, "spec", None), "runtime", DEFAULT_RUNTIME))
        return await self._container.key_vault.resolve_selection(
            runtime=runtime, model=model or "", key_id=key_id or None
        )
