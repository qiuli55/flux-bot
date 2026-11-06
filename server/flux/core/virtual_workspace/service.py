"""Virtual Workspace 服务（主规格 §7；实施计划 ④⑥⑦）。

权威存储是 virtual_changes 表，服务层负责：
- 把 agent 经 MCP `proposal.create` 提交的 `CodeChangeSet` 落成一条条 Proposal
  （含 original_hash 与 unified diff）；
- 人工审查动作（accept / reject）与状态跃迁；
- Apply：把落盘交给 Apply Engine（⑦），自己只负责状态与审计字段；
- 批日志（P0-1）：每次 Apply 先落一条 apply_batches 记录（先于任何磁盘操作）、
  逐阶段推进，崩溃由 recover_interrupted_applies 按磁盘事实对账。

**落盘的唯一入口是 ApplyEngine**，API / Agent / UI 都不允许自己写用户文件。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flux.core.event.bus import EventBus, Events
from flux.core.virtual_workspace.apply_engine import (
    OUTCOME_NEEDS_ATTENTION,
    PHASE_PREPARED,
    ApplyEngine,
    ApplyOutcome,
    ChangeRecovery,
    RecoveryEntry,
    RollbackItem,
    recover_batch_on_disk,
    rollback_batch_on_disk,
    safe_relative_path,
)
from flux.core.virtual_workspace.backup import BACKUP_RELATIVE_ROOT, BackupService
from flux.core.virtual_workspace.batch_repository import ApplyBatchRepository
from flux.core.virtual_workspace.diff_engine import (
    compute_delete_diff,
    compute_file_diff,
    content_hash,
)
from flux.core.virtual_workspace.path_guard import resolve_within_root
from flux.core.virtual_workspace.proposal_parser import CodeChangeSet, FileChange
from flux.core.virtual_workspace.repository import ProposalRepository
from flux.enums import (
    ApplyBatchStatus,
    ChangeKind,
    RecoveryResolution,
    VirtualChangeStatus,
)
from flux.errors import (
    ConflictError,
    InvalidTransitionError,
    NotFoundError,
    ProposalExpiredError,
    ValidationError,
)
from flux.logging import get_logger
from flux.models.apply_batch import ApplyBatch
from flux.models.workspace import VirtualChange

logger = get_logger(__name__)

# apply_error 里保留的测试输出上限。测试失败时最该被看到的是「哪个用例挂了、挂在哪行」，
# 这些都在 pytest 输出的尾部；全文可能上千行，截太长反而读不出重点。
APPLY_ERROR_OUTPUT_CHARS = 4000

#: 批日志 recovery_note 的上限：恢复说明是给人看的摘要，不是逐字日志。
RECOVERY_NOTE_CHARS = 4000

#: 恢复挂起项在 apply_error 上的前缀：据此识别"待人工决策"的提案（配合 recovery_resolution 为空）。
RECOVERY_ERROR_PREFIX = "崩溃恢复："

#: 三版本对比（备份原文 / 磁盘现状 / 提案内容）单份内容的展示上限，超出截断。
RECOVERY_CONTENT_CHARS = 200_000

#: 允许的审查状态跃迁（§7.2 文件状态机 + 实施计划 §5 的 FAILED + P0-02 的 EXPIRED）。
#: EXPIRED 是终态：过期/被取代的提案不能再被批准或落盘，只能重新生成提案。
#:
#: APPLYING（P0-1）是在批日志保护下的中间态：推进到 APPLIED/FAILED、或崩溃恢复退回
#: ACCEPTED，都由 apply_many / recover_interrupted_applies 直接写库（附 recovery_note），
#: 人工触发的 transition 一律不许碰它——apply 进行中被人改状态会让批日志与磁盘对不上账。
ALLOWED: dict[VirtualChangeStatus, frozenset[VirtualChangeStatus]] = {
    VirtualChangeStatus.PENDING: frozenset(
        {
            VirtualChangeStatus.ACCEPTED,
            VirtualChangeStatus.REJECTED,
            VirtualChangeStatus.EXPIRED,
        }
    ),
    VirtualChangeStatus.ACCEPTED: frozenset(
        {
            VirtualChangeStatus.APPLYING,
            VirtualChangeStatus.APPLIED,
            VirtualChangeStatus.REJECTED,
            VirtualChangeStatus.FAILED,
            VirtualChangeStatus.EXPIRED,
        }
    ),
    VirtualChangeStatus.APPLYING: frozenset(),
    VirtualChangeStatus.REJECTED: frozenset(),
    # APPLIED 只能被 P1-2 正式回滚推进到 ROLLED_BACK（由 rollback_* 直接写库，不经 _transition）；
    # 其余人工动作（reject/expire 等）一律不可再碰它。
    VirtualChangeStatus.APPLIED: frozenset({VirtualChangeStatus.ROLLED_BACK}),
    VirtualChangeStatus.FAILED: frozenset(),
    VirtualChangeStatus.EXPIRED: frozenset(),
    VirtualChangeStatus.ROLLED_BACK: frozenset(),
}

#: 提案失效原因（写入 expired_reason，供 UI 解释"为什么这条不能再审"）
EXPIRE_REASON_TIMEOUT = "超过审核有效期，提案已失效"
EXPIRE_REASON_SUPERSEDED = "同一文件已有更新的提案，本条被取代"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """把库里读出的时间归一到带时区的 UTC。

    SQLite 不保存时区，写进去的 aware datetime 读出来会丢掉 tzinfo；PostgreSQL 则保留。
    只做一次归一，过期判断在任何数据库上都一致。
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class RecoveryItem:
    """一条待人工决策的崩溃恢复项（三版本并列，供 UI / CLI 展示与决策）。

    `disk_state`：modified（内容被外部改动）/ deleted（文件被外部删除）/ not_file（路径不是
    普通文件）/ unknown（工作区根未配置，无法看盘）。只有 backup_available 为真时才能选"覆盖备份"。
    """

    change_id: str
    batch_id: str
    file_path: str
    original_content: str
    disk_content: str | None
    proposed_content: str
    backup_available: bool
    disk_state: str
    note: str
    detected_at: datetime | None

    def to_dict(self) -> dict[str, object]:
        return {
            "change_id": self.change_id,
            "batch_id": self.batch_id,
            "file_path": self.file_path,
            "original_content": self.original_content,
            "disk_content": self.disk_content,
            "proposed_content": self.proposed_content,
            "backup_available": self.backup_available,
            "disk_state": self.disk_state,
            "note": self.note,
            "detected_at": self.detected_at.isoformat() if self.detected_at else None,
        }


class VirtualWorkspaceService:
    def __init__(
        self,
        repository: ProposalRepository,
        bus: EventBus | None = None,
        apply_engine: ApplyEngine | None = None,
        proposal_ttl_seconds: int = 0,
        batch_repository: ApplyBatchRepository | None = None,
    ) -> None:
        self._repo = repository
        self._bus = bus
        # 未注入引擎时用默认配置（workspace_root 为空即"未配置"，Apply 会明确拒绝）
        self._engine = apply_engine or ApplyEngine()
        # 审核有效期（秒）；<= 0 表示不启用超时失效
        self._ttl_seconds = proposal_ttl_seconds
        # 批日志（P0-1）：未注入时 Apply 照常可用，只是崩溃后无人对账（轻量单测装配）
        self._batches = batch_repository
        #: 本进程正在跑的批 ID——懒检查绝不能把"正在执行的批"当成崩溃现场去恢复
        self._active_batches: set[str] = set()
        self._recovery_lock = asyncio.Lock()

    # --- 提案 ---

    async def propose(
        self,
        *,
        file_path: str,
        original_content: str,
        proposed_content: str | None,
        kind: ChangeKind | str | None = None,
        project_id: str | uuid.UUID | None = None,
        task_id: str | uuid.UUID | None = None,
        group_id: str | uuid.UUID | None = None,
        agent_source: str | None = None,
        reason: str | None = None,
        summary: str | None = None,
        ttl_seconds: int | None = None,
    ) -> VirtualChange:
        """落一条提案。内容与原文一致时拒绝——没有可审阅的改动就不该占用审核队列。

        `kind` 缺省时按 proposed_content 推断：为 None 即 delete，否则 modify（create 由
        propose_changes 依据"原文件是否存在"判定后显式传入）。delete 没有改动后内容，
        其 diff 是整文件移除（P1-1）。

        新提案落地后，同一文件在相同项目/任务范围内的旧 pending 提案会被置为 expired：
        审核队列里不该同时挂着两条改同一文件、谁也不该被落盘的提案（P0-02 状态一致性）。
        """
        resolved_kind = _resolve_kind(kind, proposed_content)
        if resolved_kind is ChangeKind.DELETE:
            if proposed_content is not None:
                raise ValidationError(
                    f"删除提案不得携带 content：{file_path}", details={"file_path": file_path}
                )
            diff = compute_delete_diff(file_path, original_content)
        else:
            if proposed_content is None:
                raise ValidationError(
                    f"{resolved_kind.value} 提案必须给出完整 content：{file_path}",
                    details={"file_path": file_path},
                )
            diff = compute_file_diff(file_path, original_content, proposed_content)
            if not diff.changed:
                raise ValidationError(
                    f"文件 {file_path} 的提案内容与原文一致，没有可审阅的改动",
                    details={"file_path": file_path},
                )
        project_key = _as_uuid(project_id)
        task_key = _as_uuid(task_id)
        change = await self._repo.create(
            file_path=file_path,
            kind=resolved_kind.value,
            original_content=original_content,
            proposed_content=proposed_content,
            original_hash=content_hash(original_content),
            diff=diff.unified,
            added_lines=diff.added_lines,
            removed_lines=diff.removed_lines,
            hunks=diff.hunks,
            project_id=project_key,
            task_id=task_key,
            group_id=_as_uuid(group_id),
            agent_source=agent_source,
            reason=reason,
            summary=summary,
            expires_at=self._deadline(ttl_seconds),
        )
        await self._publish(change, previous=None)
        await self._supersede_pending_for_file(change)
        return change

    async def propose_changes(
        self,
        change_set: CodeChangeSet,
        *,
        project_id: str | uuid.UUID | None = None,
        task_id: str | uuid.UUID | None = None,
        agent_source: str | None = None,
        original_files: Mapping[str, str | None] | None = None,
    ) -> list[VirtualChange]:
        """把一个 CodeChangeSet 落成多条 Proposal（每个文件一条）。

        `original_files` 是各文件当前内容的**二元**事实：键不存在或值为 None 表示文件不存在，
        空串表示"存在但是空文件"（P1-1 起必须能区分二者，否则删空文件会被当成新建）。
        kind 缺省自动判定：原文件存在 → modify，不存在 → create；显式 `op` 优先。
        op=delete 的目标必须存在，且其 proposed_content 记 NULL。

        同一次提交的所有提案共享一个 group_id，可整组批准 / 拒绝 / 落盘（P0-02）。
        """
        originals = dict(original_files or {})
        planned: list[tuple[FileChange, str, ChangeKind]] = []
        for change in change_set.changes:
            value = originals.get(change.path)
            exists = value is not None
            original = value or ""
            kind = _auto_kind(change.op, exists)
            if kind is ChangeKind.DELETE:
                if not exists:
                    raise ValidationError(
                        f"删除目标不存在，无法提交删除提案：{change.path}",
                        details={"file_path": change.path},
                    )
            elif not compute_file_diff(change.path, original, change.content or "").changed:
                # 没有可审阅改动的 create / modify 直接跳过（不占用审核队列）
                continue
            planned.append((change, original, kind))
        if not planned:
            raise ValidationError("提案没有任何有效改动", details={"files": list(change_set.paths)})
        group_id = uuid.uuid4()
        created: list[VirtualChange] = []
        for change, original, kind in planned:
            created.append(
                await self.propose(
                    file_path=change.path,
                    original_content=original,
                    proposed_content=None if kind is ChangeKind.DELETE else change.content,
                    kind=kind,
                    project_id=project_id,
                    task_id=task_id,
                    group_id=group_id,
                    agent_source=agent_source,
                    reason=change.reason or None,
                    summary=change_set.summary or None,
                )
            )
        return created

    # --- 查询 ---

    async def get(self, change_id: str | uuid.UUID) -> VirtualChange:
        return await self._repo.get(change_id)

    async def list(
        self,
        *,
        project_id: str | uuid.UUID | None = None,
        task_id: str | uuid.UUID | None = None,
        group_id: str | uuid.UUID | None = None,
        file_path: str | None = None,
        status: str | VirtualChangeStatus | None = None,
    ) -> list[VirtualChange]:
        return await self._repo.list(
            project_id=project_id,
            task_id=task_id,
            group_id=group_id,
            file_path=file_path,
            status=status,
        )

    async def list_group(self, group_id: str | uuid.UUID) -> list[VirtualChange]:
        """一次提交（一个 group_id）下的全部提案。"""
        return await self._repo.list(group_id=group_id)

    # --- 人工审查 ---

    async def accept(self, change_id: str | uuid.UUID) -> VirtualChange:
        return await self._transition(change_id, VirtualChangeStatus.ACCEPTED)

    async def reject(self, change_id: str | uuid.UUID, reason: str | None = None) -> VirtualChange:
        """拒绝一条提案。reason 由人工填写，进事件供审计，不改提案内容。"""
        return await self._transition(
            change_id, VirtualChangeStatus.REJECTED, extra={"reason": reason} if reason else None
        )

    # --- 批量人工审查（P0-03：先全体校验、再统一执行，不留半状态）---

    async def accept_many(self, change_ids: Sequence[str | uuid.UUID]) -> list[VirtualChange]:
        """整批批准：全部 ID 存在、全部跃迁合法才动手，否则一个都不动。"""
        return await self._transition_many(change_ids, VirtualChangeStatus.ACCEPTED)

    async def reject_many(
        self, change_ids: Sequence[str | uuid.UUID], reason: str | None = None
    ) -> list[VirtualChange]:
        return await self._transition_many(
            change_ids,
            VirtualChangeStatus.REJECTED,
            extra={"reason": reason} if reason else None,
        )

    async def apply(
        self,
        change_id: str | uuid.UUID,
        *,
        workspace_root: str | Path | None = None,
        run_tests: bool = True,
    ) -> VirtualChange:
        """应用单条提案（等价于只有一条的原子批次）。"""
        return (
            await self.apply_many((change_id,), workspace_root=workspace_root, run_tests=run_tests)
        )[0]

    async def apply_many(
        self,
        change_ids: Sequence[str | uuid.UUID],
        *,
        workspace_root: str | Path | None = None,
        run_tests: bool = True,
    ) -> list[VirtualChange]:
        """原子批量应用（P0-03）：要么全部落盘成功、要么全部恢复并标记失败。

        pending 自动先转 accepted（调用即人工批准，§7.5）；状态预检在动任何一条之前
        对整批做完。落盘是同步 IO + 子进程，放进线程执行，避免阻塞事件循环。

        P0-1：整段由批日志护航——批记录先于任何磁盘操作落库、逐阶段推进，提案状态走
        ACCEPTED → APPLYING → APPLIED/FAILED。进程被杀后由 recover_interrupted_applies
        按磁盘事实对账；失败时状态落 failed 并保留原因，异常继续上抛——不静默兜底。
        """
        # 懒检查：上一次进程被杀（或异常退出）留下的中断批先对账。CLI 等一次性进程
        # 没有 lifespan 兜底，只有把检查放在每次 Apply 入口，崩溃现场才一定会被处理。
        await self.recover_interrupted_applies()

        changes = await self._load_many(change_ids, action="apply")
        # 过期预检：已过期的整批先落 expired 终态再报错，不允许落盘旧提案
        await self._guard_expired(changes)
        # 状态预检：整批都在可批准状态才继续（避免"前两条已批准、第三条才发现非法"）。
        # applying 说明另一条路径（并发 Apply / 尚未对账的中断批）正在动它，直接冲突退出
        for change in changes:
            current = VirtualChangeStatus(change.status)
            if current is VirtualChangeStatus.APPLYING:
                raise ConflictError(
                    f"提案正在应用（applying），不允许并发 Apply：{change.id}",
                    details={"change_id": str(change.id), "status": current.value},
                )
            if current is VirtualChangeStatus.ACCEPTED:
                continue
            self._assert_transition(change, VirtualChangeStatus.ACCEPTED)
        accepted: list[VirtualChange] = []
        for change in changes:
            if VirtualChangeStatus(change.status) is not VirtualChangeStatus.ACCEPTED:
                change = await self._transition(change.id, VirtualChangeStatus.ACCEPTED)
            accepted.append(change)

        # 批 id 先登记进活跃集合、再落库：识别"本进程正在跑的批"与批记录之间不留窗口，
        # 否则并发入口的懒检查可能刚建好批就把它当成崩溃现场去恢复
        batch_uuid = uuid.uuid4()
        batch_id = str(batch_uuid)
        self._active_batches.add(batch_id)
        try:
            if self._batches is not None:
                await self._batches.create(
                    batch_id=batch_uuid,
                    change_ids=[str(change.id) for change in accepted],
                    backup_root=BACKUP_RELATIVE_ROOT.as_posix(),
                    phase=PHASE_PREPARED,
                )
            # 抢占：只有当前确实是 accepted 才能置 applying（并发第二方在这里被拒）
            claimed: list[VirtualChange] = []
            try:
                for change in accepted:
                    claimed.append(await self._repo.claim_applying(change.id))
            except ConflictError:
                await self._release_applying(claimed)
                await self._finish_batch(
                    batch_id,
                    ApplyBatchStatus.FAILED,
                    error="状态抢占失败：提案已被其它 Apply 占用或已不在 accepted",
                )
                raise
            for change in claimed:
                await self._publish(change, previous=VirtualChangeStatus.ACCEPTED)

            loop = asyncio.get_running_loop()
            try:
                outcomes = await asyncio.to_thread(
                    self._engine.apply_many,
                    claimed,
                    workspace_root=workspace_root,
                    run_tests=run_tests,
                    on_phase=self._phase_journal(batch_id, loop),
                )
            except (ConflictError, ValidationError) as exc:
                # 预检未通过（文件被用户改过 / 路径非法 / 未配置工作区根目录）：还没碰盘，
                # 回退 applying 让提案留在 accepted 等人工重新决策（apply_error 保持空）
                await self._release_applying(claimed)
                await self._finish_batch(
                    batch_id, ApplyBatchStatus.FAILED, error=f"预检未通过，未落盘：{exc}"
                )
                raise
            except Exception as exc:
                reason = _failure_reason(exc)
                for change in claimed:
                    failed = await self._repo.set_apply_result(
                        change.id,
                        status=VirtualChangeStatus.FAILED,
                        apply_error=reason,
                    )
                    await self._publish(
                        failed,
                        previous=VirtualChangeStatus.APPLYING,
                        extra={"error": str(exc)},
                    )
                await self._finish_batch(batch_id, ApplyBatchStatus.FAILED, error=reason)
                raise

            applied: list[VirtualChange] = []
            for change, outcome in zip(claimed, outcomes, strict=True):
                updated = await self._repo.set_apply_result(
                    change.id,
                    status=VirtualChangeStatus.APPLIED,
                    backup_path=outcome.backup_path,
                )
                await self._publish(updated, previous=VirtualChangeStatus.APPLYING)
                applied.append(updated)
            await self._finish_batch(batch_id, ApplyBatchStatus.APPLIED)
            # 上下文闭环：整批成功后再发一条批次级事件（逐条 applied 没有批次边界，
            # 沉淀挂在逐条事件上会把一批写成 N 条）。
            await self._publish_applied(applied, outcomes, batch_id)
            return applied
        finally:
            self._active_batches.discard(batch_id)

    async def _release_applying(self, changes: Sequence[VirtualChange]) -> None:
        """把已抢占（applying）的提案回退到 accepted，等人工重新决策。

        预检失败不是"落盘失败"：提案本身没有错（文件被改 / 根未配置都可修复），
        所以不写 apply_error（test_workspace_apply_precheck_conflict_keeps_status_accepted
        的契约）。事件必须补发：UI 已经看到 applying，不回退就会一直显示"应用中"。
        """
        for change in changes:
            released = await self._repo.set_status(change.id, VirtualChangeStatus.ACCEPTED)
            await self._publish(
                released, previous=VirtualChangeStatus.APPLYING, extra={"released": True}
            )

    # --- 崩溃恢复（P0-1）---

    async def recover_interrupted_applies(self) -> list[ApplyBatch]:
        """对账所有中断的 Apply 批（幂等）：启动全量扫与 Apply 入口懒检查共用。

        只处理"不属于本进程活跃集合"的 in_progress 批：正在执行的批绝不能当成
        崩溃现场恢复。单批恢复失败只记日志、不拖死调用方——残留批留给下一次扫描。
        """
        if self._batches is None:
            return []
        async with self._recovery_lock:
            batches = await self._batches.list_in_progress()
            interrupted = [b for b in batches if str(b.id) not in self._active_batches]
            recovered: list[ApplyBatch] = []
            for batch in interrupted:
                try:
                    recovered.append(await self._recover_batch(batch))
                except Exception:  # noqa: BLE001 - 单批失败不能拖住其它批与启动流程
                    logger.exception("apply.recovery 批次恢复失败 batch=%s", batch.id)
            return recovered

    async def _recover_batch(self, batch: ApplyBatch) -> ApplyBatch:
        """恢复一批：写对了的不动、能还原的还原、动不了的显式告警（幂等，见 apply_engine）。"""
        entries: list[RecoveryEntry] = []
        previous: dict[str, VirtualChangeStatus] = {}
        notes: list[str] = []
        for raw_id in batch.change_ids:
            try:
                change = await self._repo.get(raw_id)
            except NotFoundError:
                notes.append(f"{raw_id}：提案已不存在，跳过")
                continue
            status = VirtualChangeStatus(change.status)
            if status not in (VirtualChangeStatus.APPLYING, VirtualChangeStatus.ACCEPTED):
                # 状态已是终态（或已被人工处理）：磁盘结论以状态为准，不再碰盘
                notes.append(f"{raw_id}：状态为 {status.value}，无需恢复")
                continue
            previous[str(change.id)] = status
            proposed = change.proposed_content
            entries.append(
                RecoveryEntry(
                    change_id=str(change.id),
                    file_path=change.file_path,
                    kind=change.kind or ChangeKind.MODIFY.value,
                    original_hash=change.original_hash,
                    original_content=change.original_content,
                    # delete 类没有"改动后内容"：proposed_hash 为 None 表示"应用后文件应不存在"
                    proposed_hash=content_hash(proposed) if proposed is not None else None,
                )
            )

        results: list[ChangeRecovery] = []
        if entries:
            try:
                root = self._engine.current_root()
            except ValidationError as exc:
                note = f"工作区根未配置，无法对账磁盘：{exc.message}"
                notes.append(note)
                finished = await self._finish_batch(
                    batch.id,
                    ApplyBatchStatus.NEEDS_ATTENTION,
                    error=note,
                    recovery_note=_clip("；".join(notes), RECOVERY_NOTE_CHARS),
                )
                return finished or batch
            results = await asyncio.to_thread(
                recover_batch_on_disk,
                root,
                entries,
                backup_rel_root=batch.backup_root or BACKUP_RELATIVE_ROOT.as_posix(),
            )

        flagged = False
        for result in results:
            if result.outcome == OUTCOME_NEEDS_ATTENTION:
                flagged = True
                updated = await self._repo.set_apply_result(
                    result.change_id,
                    status=VirtualChangeStatus.FAILED,
                    apply_error=f"崩溃恢复：{result.detail}",
                )
            else:
                updated = await self._repo.set_apply_result(
                    result.change_id,
                    status=VirtualChangeStatus.ACCEPTED,
                    backup_path=result.backup_path,
                )
            await self._publish(
                updated,
                previous=previous.get(result.change_id),
                extra={
                    "recovered": True,
                    "batch_id": str(batch.id),
                    "outcome": result.outcome,
                    "detail": result.detail,
                },
            )
            notes.append(f"{result.file_path}：{result.outcome}（{result.detail}）")

        status = ApplyBatchStatus.NEEDS_ATTENTION if flagged else ApplyBatchStatus.RECOVERED
        finished = await self._finish_batch(
            batch.id, status, recovery_note=_clip("；".join(notes), RECOVERY_NOTE_CHARS)
        )
        recovered_batch = finished or batch
        if self._bus is not None:
            await self._bus.publish(
                Events.APPLY_RECOVERED,
                {
                    "batch_id": str(batch.id),
                    "status": recovered_batch.status,
                    "change_ids": list(batch.change_ids),
                    "outcomes": [result.outcome for result in results],
                },
            )
        return recovered_batch

    async def _finish_batch(
        self,
        batch_id: str,
        status: ApplyBatchStatus,
        *,
        error: str | None = None,
        recovery_note: str | None = None,
    ) -> ApplyBatch | None:
        if self._batches is None:
            return None
        return await self._batches.finish(
            batch_id, status=status, error=error, recovery_note=recovery_note
        )

    def _phase_journal(
        self, batch_id: str, loop: asyncio.AbstractEventLoop
    ) -> Callable[[str], None] | None:
        """引擎（工作线程）→ 事件循环的批日志回调：同步等阶段写库落定才返回。

        阶段记录必须先于对应磁盘操作持久化；日志写不进去就抛，引擎按失败回滚——
        绝不能留下"盘上动过、批日志没记"的批。
        """
        if self._batches is None:
            return None
        repository = self._batches

        def journal(phase: str) -> None:
            future = asyncio.run_coroutine_threadsafe(repository.set_phase(batch_id, phase), loop)
            future.result()

        return journal

    # --- 崩溃恢复的人工决策（P0-1 §2.2，2026-10-05 定案）---

    async def list_pending_recovery_items(self) -> list[RecoveryItem]:
        """列出所有待人工决策的崩溃恢复项（只读、幂等，不改任何状态）。

        判定显式查列：批为 needs_attention、提案 failed 且 apply_error 带恢复前缀、
        且 recovery_resolution 仍为空。三版本内容一并返回，供 UI / CLI 直接展示与决策。
        """
        if self._batches is None:
            return []
        root = self._current_root_or_none()
        items: list[RecoveryItem] = []
        for batch in await self._batches.list_by_status(ApplyBatchStatus.NEEDS_ATTENTION):
            for raw_id in batch.change_ids:
                try:
                    change = await self._repo.get(raw_id)
                except NotFoundError:
                    continue
                if not self._is_pending_recovery(change):
                    continue
                items.append(self._build_recovery_item(batch, change, root))
        return items

    async def resolve_recovery(
        self, change_id: str | uuid.UUID, resolution: RecoveryResolution
    ) -> VirtualChange:
        """人工决策一条挂起的恢复项。

        - COVER：用备份覆盖当前内容（还原为改动前原文）→ 提案回 accepted，可重试；
        - KEEP：保持磁盘现状、绝不覆盖用户改动 → 提案留 failed（作废）并记下决策。

        备份缺失时 COVER 明确拒绝（409），不会静默降级成 KEEP。
        """
        change = await self._repo.get(change_id)
        if change.recovery_resolution is not None:
            raise ConflictError(
                f"该恢复项已处理过（{change.recovery_resolution}），不能重复决策",
                details={"change_id": str(change.id), "resolution": change.recovery_resolution},
            )
        if not self._is_pending_recovery(change):
            raise ValidationError(
                "该提案不是待决策的崩溃恢复项，不能用恢复决策处理",
                details={"change_id": str(change.id), "status": change.status},
            )
        previous = VirtualChangeStatus(change.status)
        if resolution is RecoveryResolution.COVER:
            await self._cover_from_backup(change)
            updated = await self._repo.set_apply_result(
                change.id,
                status=VirtualChangeStatus.ACCEPTED,
                backup_path=change.backup_path,
                apply_error=None,
            )
        else:
            updated = await self._repo.set_apply_result(
                change.id,
                status=VirtualChangeStatus.FAILED,
                backup_path=change.backup_path,
                apply_error=f"{change.apply_error}（人工选择：保持现状）",
            )
        updated = await self._repo.set_recovery_resolution(change.id, resolution)
        await self._publish(
            updated, previous=previous, extra={"recovery_resolved": resolution.value}
        )
        await self._close_batch_if_resolved(change.id)
        return updated

    def _is_pending_recovery(self, change: VirtualChange) -> bool:
        """待决策判定：未决策 + 提案已 failed + 失败原因是崩溃恢复写下的。"""
        return (
            change.recovery_resolution is None
            and VirtualChangeStatus(change.status) is VirtualChangeStatus.FAILED
            and (change.apply_error or "").startswith(RECOVERY_ERROR_PREFIX)
        )

    def _current_root_or_none(self) -> Path | None:
        """看盘需要工作区根；未配置时返回 None（列表仍可给出提案侧的两个版本）。"""
        try:
            return self._engine.current_root()
        except ValidationError:
            return None

    @staticmethod
    def _backup_file_for(root: Path, change: VirtualChange) -> Path | None:
        try:
            relative = safe_relative_path(change.file_path)
            return resolve_within_root(root, BACKUP_RELATIVE_ROOT / str(change.id) / relative)
        except ValidationError:
            return None

    def _build_recovery_item(
        self, batch: ApplyBatch, change: VirtualChange, root: Path | None
    ) -> RecoveryItem:
        """组装一条待决策项：备份原文 / 磁盘现状 / 提案内容三版本 + 备份是否可用。"""
        backup = self._backup_file_for(root, change) if root is not None else None
        disk_content: str | None = None
        disk_state = "unknown"
        if root is not None:
            try:
                target = resolve_within_root(root, safe_relative_path(change.file_path))
            except ValidationError:
                target = None
            if target is not None:
                if not target.exists():
                    disk_state = "deleted"
                elif not target.is_file():
                    disk_state = "not_file"
                else:
                    disk_state = "modified"
                    try:
                        disk_content = _clip(
                            target.read_text(encoding="utf-8"), RECOVERY_CONTENT_CHARS
                        )
                    except (OSError, UnicodeDecodeError):
                        disk_content = None
        return RecoveryItem(
            change_id=str(change.id),
            batch_id=str(batch.id),
            file_path=change.file_path,
            original_content=_clip(change.original_content, RECOVERY_CONTENT_CHARS),
            disk_content=disk_content,
            proposed_content=_clip(change.proposed_content, RECOVERY_CONTENT_CHARS),
            backup_available=bool(backup is not None and backup.is_file()),
            disk_state=disk_state,
            note=change.apply_error or "",
            detected_at=batch.finished_at,
        )

    async def _cover_from_backup(self, change: VirtualChange) -> None:
        """用备份覆盖当前内容（还原为改动前原文）；备份缺失或根未配置时明确拒绝。"""
        root = self._current_root_or_none()
        if root is None:
            raise ValidationError(
                "未配置工作区根目录，无法执行覆盖",
                details={"hint": "设置 FLUX_WORKSPACE_ROOT 后重试"},
            )
        backup = self._backup_file_for(root, change)
        if backup is None or not backup.is_file():
            raise ConflictError(
                "备份不存在，无法执行『覆盖备份』；只能保持现状或人工处理",
                details={"change_id": str(change.id), "file_path": change.file_path},
            )
        await asyncio.to_thread(
            BackupService(workspace_root=root).restore,
            backup_path=backup,
            relative_target=safe_relative_path(change.file_path),
        )

    async def _close_batch_if_resolved(self, change_id: uuid.UUID) -> None:
        """批内所有挂起项都决策完 → 批从 needs_attention 收为 recovered。"""
        if self._batches is None:
            return
        for batch in await self._batches.list_by_status(ApplyBatchStatus.NEEDS_ATTENTION):
            if str(change_id) not in batch.change_ids:
                continue
            for raw_id in batch.change_ids:
                try:
                    other = await self._repo.get(raw_id)
                except NotFoundError:
                    continue
                if other.recovery_resolution is None:
                    return
            finished = await self._finish_batch(
                batch.id,
                ApplyBatchStatus.RECOVERED,
                recovery_note=_clip(
                    f"{batch.recovery_note or ''}；人工决策完成（全部挂起项已处理）",
                    RECOVERY_NOTE_CHARS,
                ),
            )
            if self._bus is not None and finished is not None:
                await self._bus.publish(
                    Events.APPLY_RECOVERED,
                    {
                        "batch_id": str(batch.id),
                        "status": finished.status,
                        "change_ids": list(batch.change_ids),
                        "outcomes": ["manually_resolved"],
                    },
                )
            return

    # --- 整组审核（P0-02：一次提交的多文件一起批准/拒绝/落盘）---

    async def accept_group(self, group_id: str | uuid.UUID) -> list[VirtualChange]:
        return await self.accept_many(await self._ids_of_group(group_id, action="accept"))

    async def reject_group(
        self, group_id: str | uuid.UUID, reason: str | None = None
    ) -> list[VirtualChange]:
        return await self.reject_many(
            await self._ids_of_group(group_id, action="reject"), reason=reason
        )

    async def apply_group(
        self,
        group_id: str | uuid.UUID,
        *,
        workspace_root: str | Path | None = None,
        run_tests: bool = True,
    ) -> list[VirtualChange]:
        """整组原子落盘：与 apply_many 同语义，只是入口按 group_id 取全体提案。"""
        return await self.apply_many(
            await self._ids_of_group(group_id, action="apply"),
            workspace_root=workspace_root,
            run_tests=run_tests,
        )

    # --- 正式回滚（P1-2 §5.2）---

    async def list_recent_batches(
        self, *, recent: bool = False, limit: int = 20
    ) -> list[ApplyBatch]:
        """最近若干 Apply 批（最新在前）；recent=True 时只返回可回滚的 applied 批。"""
        if self._batches is None:
            return []
        batches = await self._batches.list_recent(limit=limit)
        if recent:
            batches = [b for b in batches if b.status == ApplyBatchStatus.APPLIED.value]
        return batches

    async def rollback_batch(
        self, batch_id: str | uuid.UUID, *, workspace_root: str | Path | None = None
    ) -> ApplyBatch:
        """整批回滚一次 Apply：先全量预检、再按逆序还原（P1-2）。

        只允许 applied 批；recovered/failed/needs_attention 不可回滚，二次回滚（已 rolled_back）
        一律 409 且不产生任何写入。
        """
        if self._batches is None:
            raise ConflictError("未装配批日志，无法执行回滚", details={"batch_id": str(batch_id)})
        batch = await self._batches.get(batch_id)
        changes = await self._load_many(batch.change_ids, action="rollback")
        await self._execute_rollback(batch, changes, workspace_root=workspace_root)
        return await self._batches.get(batch_id)

    async def rollback_change(
        self, change_id: str | uuid.UUID, *, workspace_root: str | Path | None = None
    ) -> VirtualChange:
        """单条回滚：撤销一条已落盘提案对文件的改动。"""
        change = await self._repo.get(change_id)
        batch = (
            await self._batches.find_for_change(change.id) if self._batches is not None else None
        )
        if batch is None:
            raise ConflictError(
                "该提案没有关联的 Apply 批次，无法回滚",
                details={"change_id": str(change.id)},
            )
        await self._execute_rollback(batch, [change], workspace_root=workspace_root)
        return await self._repo.get(change_id)

    async def _execute_rollback(
        self,
        batch: ApplyBatch,
        changes: Sequence[VirtualChange],
        *,
        workspace_root: str | Path | None,
    ) -> list[VirtualChange]:
        """执行回滚：批状态 + 逐条状态先全量校验，再交给引擎按磁盘事实还原。

        引擎在动盘前会对整组做一次磁盘预检，任一不满足即抛 ConflictError（未写盘）。
        """
        if ApplyBatchStatus(batch.status) is not ApplyBatchStatus.APPLIED:
            raise ConflictError(
                f"Apply 批次当前状态为 {batch.status}，不可回滚",
                details={"batch_id": str(batch.id), "status": batch.status},
            )
        for change in changes:
            current = VirtualChangeStatus(change.status)
            if current in (VirtualChangeStatus.APPLIED, VirtualChangeStatus.ROLLED_BACK):
                continue
            raise ConflictError(
                f"提案 {change.id} 当前状态为 {change.status}，不可回滚",
                details={"change_id": str(change.id), "status": change.status},
            )

        items = [
            RollbackItem(
                change_id=str(change.id),
                file_path=change.file_path,
                kind=change.kind or ChangeKind.MODIFY.value,
                original_hash=change.original_hash,
                proposed_hash=(
                    content_hash(change.proposed_content)
                    if change.proposed_content is not None
                    else None
                ),
                backup_path=change.backup_path,
            )
            for change in changes
            if VirtualChangeStatus(change.status) is VirtualChangeStatus.APPLIED
        ]
        if items:
            root = self._engine.current_root()
            await asyncio.to_thread(rollback_batch_on_disk, root, items)

        rolled: list[VirtualChange] = []
        for change in changes:
            if VirtualChangeStatus(change.status) is VirtualChangeStatus.APPLIED:
                updated = await self._repo.set_status(change.id, VirtualChangeStatus.ROLLED_BACK)
                await self._publish(
                    updated,
                    previous=VirtualChangeStatus.APPLIED,
                    extra={"rolled_back": True, "batch_id": str(batch.id)},
                )
                rolled.append(updated)
            else:
                rolled.append(change)

        finished = await self._finish_batch_if_all_rolled_back(batch)
        if self._bus is not None:
            await self._bus.publish(
                Events.APPLY_ROLLED_BACK,
                {
                    "batch_id": str(batch.id),
                    "status": (finished or batch).status,
                    "change_ids": [str(change.id) for change in rolled],
                },
            )
        return rolled

    async def _finish_batch_if_all_rolled_back(self, batch: ApplyBatch) -> ApplyBatch | None:
        """批内所有 change 都已 rolled_back → 把批收为 rolled_back（落 finished_at）。"""
        if self._batches is None:
            return None
        for raw_id in batch.change_ids:
            try:
                other = await self._repo.get(raw_id)
            except NotFoundError:
                return None
            if VirtualChangeStatus(other.status) is not VirtualChangeStatus.ROLLED_BACK:
                return None
        return await self._finish_batch(batch.id, ApplyBatchStatus.ROLLED_BACK)

    # --- 失效（P0-02：提案过期 / 被取代）---

    async def expire(self, change_id: str | uuid.UUID, reason: str | None = None) -> VirtualChange:
        """显式让一条提案失效（人工/运维出口，与 mark_failed 同一性质）。"""
        return await self._transition(
            change_id,
            VirtualChangeStatus.EXPIRED,
            extra={"reason": reason or EXPIRE_REASON_TIMEOUT},
        )

    async def expire_many(
        self, change_ids: Sequence[str | uuid.UUID], reason: str | None = None
    ) -> list[VirtualChange]:
        return await self._transition_many(
            change_ids,
            VirtualChangeStatus.EXPIRED,
            extra={"reason": reason or EXPIRE_REASON_TIMEOUT},
        )

    async def expire_stale(self) -> list[VirtualChange]:
        """把超过审核有效期的提案统一置为 expired（幂等，可被定时任务/API 调用）。

        时间比较在 Python 侧完成：SQLite 不保存时区，只有把库里读出的时间归一后
        才能得到与 Postgres 一致的结果（见 _as_utc）。
        """
        candidates = await self._repo.list_expirable()
        stale = [c for c in candidates if self._is_stale(c)]
        if not stale:
            return []
        updated = await self._repo.mark_expired([c.id for c in stale], reason=EXPIRE_REASON_TIMEOUT)
        for change, previous in zip(updated, stale, strict=True):
            await self._publish(
                change,
                previous=VirtualChangeStatus(previous.status),
                extra={"reason": EXPIRE_REASON_TIMEOUT},
            )
        return updated

    async def mark_failed(self, change_id: str | uuid.UUID) -> VirtualChange:
        """把一条已批准的提案手动置为 failed（保留给人工/运维的显式出口）。

        正常路径下 failed 由 apply 在落盘或测试失败时写入（§7.6），这个方法让"已批准但
        决定放弃"的提案也能走到终态，而不是永久停留在 accepted。
        """
        return await self._transition(change_id, VirtualChangeStatus.FAILED)

    async def _transition(
        self,
        change_id: str | uuid.UUID,
        target: VirtualChangeStatus,
        *,
        extra: Mapping[str, object] | None = None,
    ) -> VirtualChange:
        change = await self._repo.get(change_id)
        # 目标就是 expired 时不做过期拦截（否则"把已过期的提案标记为过期"会互相打架）
        if target is not VirtualChangeStatus.EXPIRED:
            await self._guard_expired((change,))
        current = VirtualChangeStatus(change.status)
        self._assert_transition(change, target)
        updated = await self._repo.set_status(change.id, target)
        await self._publish(updated, previous=current, extra=extra)
        return updated

    async def _transition_many(
        self,
        change_ids: Sequence[str | uuid.UUID],
        target: VirtualChangeStatus,
        *,
        extra: Mapping[str, object] | None = None,
    ) -> list[VirtualChange]:
        """整批状态跃迁：先确认每一条都合法，再统一执行（P0-03 的"不半途而废"）。"""
        changes = await self._load_many(change_ids, action=str(target))
        if target is not VirtualChangeStatus.EXPIRED:
            await self._guard_expired(changes)
        for change in changes:
            self._assert_transition(change, target)
        results: list[VirtualChange] = []
        for change in changes:
            current = VirtualChangeStatus(change.status)
            updated = await self._repo.set_status(change.id, target)
            await self._publish(updated, previous=current, extra=extra)
            results.append(updated)
        return results

    async def _load_many(
        self, change_ids: Sequence[str | uuid.UUID], *, action: str
    ) -> list[VirtualChange]:
        """整批读取提案：重复 ID 直接拒绝（否则第二条会在执行阶段才炸，留下半状态）。"""
        keys = [str(change_id) for change_id in change_ids]
        if not keys:
            raise ValidationError(f"批量 {action} 至少需要一条提案", details={"action": action})
        if len(set(keys)) != len(keys):
            raise ValidationError(
                f"批量 {action} 的提案列表里存在重复 ID",
                details={"action": action, "change_ids": keys},
            )
        return [await self._repo.get(key) for key in keys]

    async def _ids_of_group(self, group_id: str | uuid.UUID, *, action: str) -> list[str]:
        changes = await self._repo.list(group_id=group_id)
        if not changes:
            raise NotFoundError(
                f"提案组 {group_id} 不存在或没有任何提案",
                details={"group_id": str(group_id), "action": action},
            )
        return [str(c.id) for c in changes]

    def _deadline(self, ttl_seconds: int | None) -> datetime | None:
        """按 TTL 算出审核截止时间；<= 0 表示不启用超时失效。"""
        ttl = self._ttl_seconds if ttl_seconds is None else ttl_seconds
        if ttl <= 0:
            return None
        return _utcnow() + timedelta(seconds=ttl)

    @staticmethod
    def _is_stale(change: VirtualChange) -> bool:
        if change.expires_at is None:
            return False
        if VirtualChangeStatus(change.status) not in (
            VirtualChangeStatus.PENDING,
            VirtualChangeStatus.ACCEPTED,
        ):
            return False
        return _as_utc(change.expires_at) <= _utcnow()

    async def _guard_expired(self, changes: Sequence[VirtualChange]) -> None:
        """惰性过期：读到超时的提案就顺手落 expired 终态，并拒绝本次审核/落盘。

        先落状态再报错——否则调用方拿到的错误说"过期了"，库里却还是 pending，
        下一个人还会再撞一次同样的墙（fail-closed：状态必须反映事实）。
        """
        stale = [change for change in changes if self._is_stale(change)]
        if not stale:
            return
        updated = await self._repo.mark_expired(
            [change.id for change in stale], reason=EXPIRE_REASON_TIMEOUT
        )
        for change, previous in zip(updated, stale, strict=True):
            await self._publish(
                change,
                previous=VirtualChangeStatus(previous.status),
                extra={"reason": EXPIRE_REASON_TIMEOUT},
            )
        ids = [str(change.id) for change in stale]
        raise ProposalExpiredError(
            f"提案已过期，不能再审核或落盘：{', '.join(ids)}",
            details={"change_ids": ids, "reason": EXPIRE_REASON_TIMEOUT},
        )

    async def _supersede_pending_for_file(self, new_change: VirtualChange) -> list[VirtualChange]:
        """同一文件 + 同一项目/任务范围内的旧 pending 提案因被取代而失效。

        只失效 pending：已被人工批准（accepted）的提案不因新提案出现而静默消失，
        它是否还能落盘由 Apply 前的 original_hash 复验来裁决。
        """
        older = await self._repo.list(
            file_path=new_change.file_path, status=VirtualChangeStatus.PENDING
        )
        superseded = [
            change
            for change in older
            if change.id != new_change.id
            and change.project_id == new_change.project_id
            and change.task_id == new_change.task_id
        ]
        if not superseded:
            return []
        updated = await self._repo.mark_expired(
            [change.id for change in superseded], reason=EXPIRE_REASON_SUPERSEDED
        )
        for change, previous in zip(updated, superseded, strict=True):
            await self._publish(
                change,
                previous=VirtualChangeStatus(previous.status),
                extra={"reason": EXPIRE_REASON_SUPERSEDED, "superseded_by": str(new_change.id)},
            )
        return updated

    @staticmethod
    def _assert_transition(change: VirtualChange, target: VirtualChangeStatus) -> None:
        current = VirtualChangeStatus(change.status)
        if target not in ALLOWED[current]:
            raise InvalidTransitionError(
                f"非法状态跃迁：{current} → {target}",
                details={"from": str(current), "to": str(target), "change_id": str(change.id)},
            )

    async def _publish(
        self,
        change: VirtualChange,
        *,
        previous: VirtualChangeStatus | None,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        if self._bus is None:
            return
        payload: dict[str, object] = {
            "change_id": str(change.id),
            "file_path": change.file_path,
            "from": str(previous) if previous is not None else None,
            "to": change.status,
        }
        if extra:
            payload.update(extra)
        await self._bus.publish(Events.WORKSPACE_CHANGED, payload)

    async def _publish_applied(
        self,
        applied: Sequence[VirtualChange],
        outcomes: Sequence[ApplyOutcome],
        batch_id: str,
    ) -> None:
        """整批落盘成功后发一条批次级事件（任务收尾沉淀的事实来源）。

        真实测试结果只存在于 `ApplyOutcome.test`，逐条 applied 事件带不出去；没有这条
        事件，沉淀只能写"改了什么"，写不出"测试到底过没过"。
        """
        if self._bus is None or not applied:
            return
        try:
            first = applied[0]
            test = outcomes[0].test if outcomes else None
            await self._bus.publish(
                Events.WORKSPACE_APPLIED,
                {
                    "batch_id": batch_id,
                    "change_ids": [str(item.id) for item in applied],
                    "files": [item.file_path for item in applied],
                    "task_id": str(first.task_id) if first.task_id else None,
                    "project_id": str(first.project_id) if first.project_id else None,
                    "added_lines": sum(getattr(item, "added_lines", 0) or 0 for item in applied),
                    "removed_lines": sum(
                        getattr(item, "removed_lines", 0) or 0 for item in applied
                    ),
                    "test": test.to_dict() if test is not None else None,
                },
            )
        except Exception:  # noqa: BLE001 - 沉淀事件不能把已经成功的 Apply 反转成失败
            logger.warning("Apply 批次事件发布失败 batch=%s", batch_id, exc_info=True)


def _clip(text: str, limit: int) -> str:
    """日志字段截断：批日志是给人看的摘要，不保留无限长文本。"""
    return text if len(text) <= limit else text[:limit]


def _failure_reason(exc: Exception) -> str:
    """拼出落到 apply_error 的失败原因（§7.6 要求"失败必留痕"）。

    只写异常消息的话，用户看到的是"Apply 后测试未通过（exit=1）"，既不知道哪个用例挂了、
    也不知道下一轮该改什么——测试输出才是修复的依据，所以从 details 里取出来一并记下。
    """
    message = str(exc)
    details = getattr(exc, "details", None)
    test = details.get("test") if isinstance(details, Mapping) else None
    output = test.get("output") if isinstance(test, Mapping) else None
    if not isinstance(output, str) or not output.strip():
        return message
    return f"{message}\n\n{output.strip()[-APPLY_ERROR_OUTPUT_CHARS:]}"


def _resolve_kind(kind: ChangeKind | str | None, proposed_content: str | None) -> ChangeKind:
    """确定一条提案的动作类型：显式 kind 优先，否则按有无改动后内容推断。"""
    if kind is None:
        return ChangeKind.DELETE if proposed_content is None else ChangeKind.MODIFY
    return kind if isinstance(kind, ChangeKind) else ChangeKind(kind)


def _auto_kind(op: str | None, exists: bool) -> ChangeKind:
    """parser 缺省 op 时的自动判定：原文件存在 → modify，不存在 → create。"""
    if op:
        return ChangeKind(op)
    return ChangeKind.MODIFY if exists else ChangeKind.CREATE


def _as_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise ValidationError(
            f"project_id / task_id 必须是合法 UUID：{value}", details={"value": str(value)}
        ) from exc
