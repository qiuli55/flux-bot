"""Apply 成功后自动刷新项目 Brain，并把整批落盘沉淀进 Project 层（上下文闭环）。

Brain 的 Overview / Tech Stack 是扫描快照；提案落盘后工作区已经变了，快照不刷新
就会让下一轮 `context.get` 给出过期事实。这里订阅两个事件：

- `workspace.changed(applied)` → 按项目重新扫描（同项目节流，默认 300 秒）；
- `workspace.applied` → 整批成功的一条事实沉淀（文件清单、行数、真实测试结果），
  按 batch_id 去重。任务收尾时提案往往还没人审，只有 Apply 之后才知道测试过没过，
  所以这条沉淀不能合并进任务终态那一条。

三条边界：

- 只认 `to == applied` 与整批成功事件，其余状态（proposed / accepted / failed …）不触发；
- 同一项目扫描节流（默认 300 秒）：批量 Apply 会连续发多条事件，不能放大成 N 次扫描；
- 任何失败只记日志，绝不影响 Apply 主流程——Brain 是辅助记忆，不是写入路径的门。

没有项目归属的提案直接跳过：Brain 按项目存，猜一个项目比不刷新更糟。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from flux.core.event.bus import EventBus, Events
from flux.enums import BrainSection, VirtualChangeStatus
from flux.logging import get_logger

logger = get_logger(__name__)

#: 同一项目两次自动扫描的最小间隔（秒）：批量 Apply 不应触发 N 次全量扫描
DEFAULT_REFRESH_INTERVAL_SECONDS = 300.0
#: Apply 沉淀里最多逐个列出的文件数，超出只报总数（记忆是短记，不是文件清单）
APPLY_NOTE_MAX_FILES = 8


class BrainRefresher:
    """`workspace.changed(applied)` → 重新扫描；`workspace.applied` → 沉淀一批落盘事实。"""

    def __init__(
        self,
        *,
        brain: Any,
        tasks: Any,
        workspace: Any,
        bus: EventBus,
        interval_seconds: float = DEFAULT_REFRESH_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._brain = brain
        self._tasks = tasks
        self._workspace = workspace
        self._bus = bus
        self._interval = max(interval_seconds, 0.0)
        self._clock = clock
        self._last_scan: dict[str, float] = {}

    def attach(self) -> None:
        self._bus.subscribe(Events.WORKSPACE_CHANGED, self.on_change)
        self._bus.subscribe(Events.WORKSPACE_APPLIED, self.on_applied)

    async def on_change(self, event: str, payload: dict[str, Any]) -> None:
        """事件入口：只处理"已落盘"，其余原样放过；异常一律吞在日志里。"""
        if payload.get("to") != VirtualChangeStatus.APPLIED.value:
            return
        change_id = payload.get("change_id")
        if not isinstance(change_id, str) or not change_id:
            return
        try:
            project_id = await self._project_of(change_id)
            if project_id is None:
                logger.debug("Brain 自动刷新跳过：提案无项目归属 change=%s", change_id)
                return
            await self.refresh(project_id, change_id=change_id)
        except Exception:  # noqa: BLE001 - Brain 刷新失败不能阻塞 Apply 主流程
            logger.warning("Brain 自动刷新失败 change=%s", change_id, exc_info=True)

    async def on_applied(self, event: str, payload: dict[str, Any]) -> None:
        """整批落盘事实 → Project 层沉淀（best-effort，按 batch_id 去重）。"""
        batch_id = payload.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id:
            return
        try:
            project_id = await self._project_of_batch(payload)
            if project_id is None:
                logger.debug("Apply 沉淀跳过：批次无项目归属 batch=%s", batch_id)
                return
            if await self._already_applied(project_id, batch_id):
                return
            await self._brain.write(
                project_id,
                section=BrainSection.AGENT_NOTES,
                content=_apply_note(payload),
                meta={"source": "apply", "batch_id": batch_id},
            )
            logger.info(
                "Apply 已沉淀到项目 Brain project=%s batch=%s files=%s",
                project_id,
                batch_id,
                len(payload.get("files") or []),
            )
        except Exception:  # noqa: BLE001 - 沉淀失败不能阻塞 Apply 主流程
            logger.warning("Apply 沉淀失败 batch=%s", batch_id, exc_info=True)

    async def _project_of_batch(self, payload: dict[str, Any]) -> uuid.UUID | str | None:
        """批次事件自带项目；没有就沿 task_id 找回任务的项目。"""
        project_id = payload.get("project_id")
        if project_id:
            return project_id
        task_id = payload.get("task_id")
        if not task_id:
            return None
        task = await self._tasks.get(task_id)
        return task.project_id if task is not None else None

    async def _already_applied(self, project_id: uuid.UUID | str, batch_id: str) -> bool:
        """同一批次只沉淀一次：事件重投不能把同一批写成两条记忆。"""
        notes = (await self._brain.sections(project_id)).get(BrainSection.AGENT_NOTES.value, [])
        return any((entry.get("metadata") or {}).get("batch_id") == batch_id for entry in notes)

    async def refresh(self, project_id: uuid.UUID | str, *, change_id: str) -> bool:
        """按项目节流后重新扫描；返回本次是否真的扫了。

        节流标记在扫描前占位，避免并发事件同时起跑；扫描失败时撤回占位，
        让下一次 Apply 仍有机会重试——失败不该被当成"刚扫过"。
        """
        key = str(project_id)
        now = self._clock()
        last = self._last_scan.get(key)
        if last is not None and now - last < self._interval:
            logger.debug("Brain 自动刷新节流 project=%s change=%s", project_id, change_id)
            return False
        self._last_scan[key] = now
        try:
            outcome = await self._brain.scan(project_id, record=True)
        except Exception:
            self._last_scan.pop(key, None)
            raise
        logger.info(
            "项目 Brain 已随 Apply 自动刷新 project=%s change=%s files=%s",
            project_id,
            change_id,
            outcome.profile.files_scanned,
        )
        return True

    async def _project_of(self, change_id: str) -> uuid.UUID | str | None:
        """提案自身没项目时，沿 task_id 找任务的项目——两者都没有就不刷新。"""
        change = await self._workspace.get(change_id)
        if change.project_id is not None:
            return change.project_id
        if change.task_id is None:
            return None
        task = await self._tasks.get(change.task_id)
        return task.project_id if task is not None else None


def _apply_note(payload: dict[str, Any]) -> str:
    """把一批落盘事实压成一段短记：文件、行数、真实测试结果。

    行数上限靠文件列表截断控制（记忆是给下一轮开工看的，不是审计日志）。
    """
    files = [str(item) for item in (payload.get("files") or [])]
    shown = "、".join(files[:APPLY_NOTE_MAX_FILES])
    if len(files) > APPLY_NOTE_MAX_FILES:
        shown = f"{shown} 等 {len(files)} 个"
    test = payload.get("test") or {}
    if test:
        verdict = "通过" if test.get("passed") else "未通过"
        test_line = (
            f"- 测试：{verdict}（`{test.get('command') or '未记录'}`，"
            f"exit={test.get('exit_code')}）"
        )
    else:
        test_line = "- 测试：未运行（未配置测试命令或本次跳过）"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return "\n".join(
        [
            f"# Apply 落盘（{stamp}）",
            f"- 文件（{len(files)}）：{shown or '无'}",
            f"- 行数：+{payload.get('added_lines') or 0}/-{payload.get('removed_lines') or 0}",
            test_line,
        ]
    )


__all__ = ["BrainRefresher", "DEFAULT_REFRESH_INTERVAL_SECONDS"]
