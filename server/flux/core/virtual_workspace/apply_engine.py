"""Apply Engine（主规格 §7.6；实施计划 ⑦）。

固定流程，顺序不可调换：

```
校验提案 → 复验 original_hash → 备份原文件 → 打补丁 → 校验文件 → 跑配置的测试
```

任何一步失败都尽可能把原文件还原回去，并把完整错误交给上层记录。
**这里是 Flux 里唯一会写用户真实文件的地方**——API、Agent、UI 都不允许自己落盘。

路径在碰盘前先经 `resolve_within_root` 校验：沿途任何一段是软链一律 fail-closed，
落盘永远发生在工作区根之内（否则仓库里一个提交的软链就能把改动写到根外）。

`apply_many` 是原子批量入口（P0-03）：**全量预检 → 全量备份 → 全量写入 → 全量校验 →
只跑一次测试 → 任一失败全部回滚**。绝不接受"A ✓ B ✓ C ✗ 但 A/B 留在盘上"的半应用；
批次内同一文件出现多条、同一提案重复出现都在预检阶段整批拒绝。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from flux.core.virtual_workspace.backup import BACKUP_RELATIVE_ROOT, BackupService
from flux.core.virtual_workspace.diff_engine import content_hash
from flux.core.virtual_workspace.path_guard import ensure_not_git_internal, resolve_within_root
from flux.core.virtual_workspace.test_runner import TestOutcome, TestRunner
from flux.enums import ChangeKind
from flux.errors import ApplyFailedError, ConflictError, ValidationError
from flux.logging import get_logger
from flux.models.workspace import VirtualChange

logger = get_logger(__name__)

#: 进程内串行化所有落盘（P0-03 的"并发 Apply"）。
#:
#: 两个并发批次同时改同一文件时，各自在对方写入与回滚之间都拿的是过期快照：
#: B 在 A 回滚（用备份覆盖）之后写入，A 的回滚就会把 B 的成果抹掉。锁的范围覆盖
#: "预检 → 备份 → 写入 → 校验 → 测试"整段，第二个批次在第一个结束前连预检都进不来。
#: 多进程部署时还需文件锁（当前是单进程装配，M0 范围）。
_APPLY_LOCK = threading.Lock()

#: 一条提案在批量回滚里的失败记录（回滚失败必须被报出来，不能静默）。
RollbackError = dict[str, str]


def _kind_of(change: VirtualChange) -> ChangeKind:
    """读提案的动作类型；脱离会话构造的实例 kind 可能为 None，按 modify 处理。"""
    value = getattr(change, "kind", None)
    return ChangeKind(value) if value else ChangeKind.MODIFY


# --- apply_batches.phase 的进度标记（P0-1）---
#
# 服务层建批记录时写 prepared；引擎每进入下一个阶段写一次，交服务层落库。
# 崩溃时"盘上做到的"最多领先日志一步，恢复算法按磁盘事实对账（不依赖 phase 猜测）。
PHASE_PREPARED = "prepared"
PHASE_BACKED_UP = "backed_up"
PHASE_WRITING = "writing"
PHASE_VERIFYING = "verifying"
PHASE_TESTING = "testing"

# --- 崩溃恢复的单条结论 ---
OUTCOME_SKIPPED = "skipped"
OUTCOME_RESTORED = "restored"
OUTCOME_DELETED = "deleted"
OUTCOME_NEEDS_ATTENTION = "needs_attention"


@dataclass(frozen=True)
class ApplyOutcome:
    """一次成功落盘的完整凭据：改了哪个文件、备份在哪、测试结果如何。"""

    change_id: str
    file_path: str
    backup_path: str | None
    test: TestOutcome | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id,
            "file_path": self.file_path,
            "backup_path": self.backup_path,
            "test": self.test.to_dict() if self.test is not None else None,
        }


def safe_relative_path(file_path: str) -> Path:
    """把提案里的路径规整成"一定落在工作区根之内"的相对路径。"""
    candidate = (file_path or "").strip().replace("\\", "/")
    pure = PurePosixPath(candidate)
    if not candidate or not pure.parts:
        raise ValidationError(
            f"提案的文件路径非法：{file_path!r}", details={"file_path": file_path}
        )
    if candidate.startswith("/") or pure.is_absolute():
        raise ValidationError(
            f"提案的文件路径不得是绝对路径：{file_path}", details={"file_path": file_path}
        )
    if ".." in pure.parts:
        raise ValidationError(
            f"提案的文件路径不得越出工作区根：{file_path}", details={"file_path": file_path}
        )
    relative = Path(*pure.parts)
    # `.git/` 内部永不落盘（提案入口已拦，这里是落盘前的最后一道防线）
    ensure_not_git_internal(relative)
    return relative


def resolve_workspace_root(candidate: str | Path | None) -> Path:
    """解析"被改项目根目录"。未配置或不是目录一律拒绝，绝不猜一个默认目录。

    Apply Engine、测试执行、Git 集成、文件浏览与扫描都必须在同一个根下工作，
    所以这段判断放在一处。
    """
    if candidate is None:
        raise ValidationError(
            "未配置工作区根目录（FLUX_WORKSPACE_ROOT）",
            details={"hint": "设置 FLUX_WORKSPACE_ROOT 指向被改项目的根目录"},
        )
    root = Path(candidate).expanduser().resolve()
    if not root.is_dir():
        raise ValidationError(
            f"工作区根目录不存在或不是目录：{root}", details={"workspace_root": str(root)}
        )
    return root


class ApplyEngine:
    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        test_command: str | None = None,
        test_timeout_seconds: float = 300.0,
    ) -> None:
        self._configured_root = Path(workspace_root).expanduser() if workspace_root else None
        self._test_command = test_command
        self._test_timeout = test_timeout_seconds

    @property
    def workspace_root(self) -> Path | None:
        """当前落盘根（桌面端切换工作目录时由 Container 赋值）。"""
        return self._configured_root

    @workspace_root.setter
    def workspace_root(self, value: str | Path | None) -> None:
        self._configured_root = Path(value).expanduser() if value else None

    # --- 主流程 ---

    def apply(
        self,
        change: VirtualChange,
        *,
        workspace_root: str | Path | None = None,
        run_tests: bool = True,
    ) -> ApplyOutcome:
        """应用单条提案（等价于只有一条的原子批次，走同一条代码路径）。"""
        return self.apply_many((change,), workspace_root=workspace_root, run_tests=run_tests)[0]

    def apply_many(
        self,
        changes: Sequence[VirtualChange],
        *,
        workspace_root: str | Path | None = None,
        run_tests: bool = True,
        on_phase: Callable[[str], None] | None = None,
    ) -> list[ApplyOutcome]:
        """原子批量落盘：要么全部成功，要么全部恢复原样（P0-03）。

        顺序固定：全量预检（路径 + 重复 + original_hash）→ 全量备份 → 全量写入 →
        全量校验 → 只跑一次测试。写入阶段开始后任何一步失败，已经碰过的文件全部回滚，
        并把回滚失败逐条报在错误的 `details["rollback_errors"]` 里。

        `on_phase` 是批日志的阶段回调（P0-1）：每进入下一个阶段调用一次，回调失败
        视同 Apply 失败并走回滚——日志写不进去就不该继续碰盘。
        """
        items = list(changes)
        if not items:
            raise ValidationError("批量 Apply 至少需要一条提案")
        # 整段持锁：并发批次拿到的盘上快照必须是一致的
        with _APPLY_LOCK:
            return self._apply_batch(
                items, workspace_root=workspace_root, run_tests=run_tests, on_phase=on_phase
            )

    # --- 批量主流程（调用方已持锁）---

    def _apply_batch(
        self,
        changes: list[VirtualChange],
        *,
        workspace_root: str | Path | None,
        run_tests: bool,
        on_phase: Callable[[str], None] | None = None,
    ) -> list[ApplyOutcome]:
        root = self._resolve_root(workspace_root)
        backups = BackupService(workspace_root=root)

        # 1) 全量预检：任何一条不通过都在"还没碰盘"时整批拒绝
        prepared = self._prepare_all(changes, root)

        # 2) 全量备份（新建文件没有可备份的内容，返回 None）
        try:
            backup_paths = {
                str(change.id): backups.backup(
                    change_id=str(change.id),
                    file_path=change.file_path,
                    relative_target=relative,
                )
                for change, relative, _ in prepared
            }
        except Exception as exc:
            raise ApplyFailedError(
                f"Apply 失败：备份原文件时出错（{exc}）",
                details={"files": [c.file_path for c, _, _ in prepared]},
            ) from exc

        # 备份完成：推进批日志（批记录自身在服务层已先落 prepared）
        _journal(on_phase, PHASE_BACKED_UP)

        # 3) 全量写入（记录"写到第几条"，失败时把已经碰过的都回滚，含写了一半的那条）
        attempted = 0
        try:
            _journal(on_phase, PHASE_WRITING)
            for change, _, target in prepared:
                attempted += 1
                self._write(change, target)
            _journal(on_phase, PHASE_VERIFYING)
            # 4) 全量校验
            for change, _, target in prepared:
                self._verify(change, target)
            # 5) 跑项目自己配置的测试（整批只跑一次）
            test: TestOutcome | None = None
            if run_tests:
                _journal(on_phase, PHASE_TESTING)
                test = self._run_tests(root)
            if test is not None and not test.passed:
                raise ApplyFailedError(
                    f"Apply 后测试未通过（exit={test.exit_code}，{test.command}）",
                    details={
                        "files": [c.file_path for c, _, _ in prepared],
                        "test": test.to_dict(),
                    },
                )
        except Exception as exc:
            rollback_errors = self._rollback_all(
                backups,
                prepared[:attempted],
                backup_paths=backup_paths,
            )
            error = (
                exc
                if isinstance(exc, (ApplyFailedError, ConflictError, ValidationError))
                else ApplyFailedError(f"Apply 失败：{exc}", details={})
            )
            if rollback_errors:
                # 回滚失败是最严重的情形（用户文件可能停在中间态），必须显式报出来
                raise ApplyFailedError(
                    f"{error.message}；且回滚未完全成功："
                    + "；".join(
                        f"{item['file_path']}（{item['error']}）" for item in rollback_errors
                    ),
                    details={**(error.details or {}), "rollback_errors": rollback_errors},
                ) from exc
            if error is exc:
                raise
            raise error from exc

        logger.info(
            "apply.done changes=%s files=%s test=%s",
            len(prepared),
            [c.file_path for c, _, _ in prepared],
            test.passed if test is not None else "skipped",
        )
        return [
            ApplyOutcome(
                change_id=str(change.id),
                file_path=change.file_path,
                backup_path=(
                    str(backup_paths[str(change.id)])
                    if backup_paths[str(change.id)] is not None
                    else None
                ),
                test=test,
            )
            for change, _, _ in prepared
        ]

    # --- 各步骤 ---

    def _resolve_root(self, override: str | Path | None) -> Path:
        return resolve_workspace_root(override if override is not None else self._configured_root)

    def current_root(self) -> Path:
        """当前配置的工作区根（崩溃恢复等运维路径使用）；未配置时明确拒绝。"""
        return self._resolve_root(None)

    def _prepare_all(
        self, changes: Sequence[VirtualChange], root: Path
    ) -> list[tuple[VirtualChange, Path, Path]]:
        """全量预检：路径合法、批次内不重复、逐条复验 original_hash。

        返回 (提案, 归一化相对路径, 落盘目标) 三元组列表，供后续步骤直接用——
        预检通过的路径不再二次解析，避免两步之间路径含义变化。
        """
        prepared: list[tuple[VirtualChange, Path, Path]] = []
        seen_ids: set[str] = set()
        seen_paths: dict[str, str] = {}
        for change in changes:
            change_id = str(change.id)
            if change_id in seen_ids:
                raise ValidationError(
                    f"同一提案在批次里出现了多次：{change_id}", details={"change_id": change_id}
                )
            seen_ids.add(change_id)

            relative = safe_relative_path(change.file_path)
            key = relative.as_posix()
            if key in seen_paths:
                # 同一文件两条提案：后写的会覆盖前写的，但两条的状态都会被标 applied，
                # 留下对不上账的审计记录——整批拒绝，让人工先把提案合并成一条
                raise ValidationError(
                    f"同一文件在同一批次里出现了多条提案：{change.file_path}",
                    details={
                        "file_path": change.file_path,
                        "change_ids": [seen_paths[key], change_id],
                    },
                )
            seen_paths[key] = change_id

            target = resolve_within_root(root, relative)
            self._assert_unchanged(change, target)
            prepared.append((change, relative, target))
        return prepared

    @staticmethod
    def _rollback_all(
        backups: BackupService,
        prepared: Sequence[tuple[VirtualChange, Path, Path]],
        *,
        backup_paths: dict[str, Path | None],
    ) -> list[RollbackError]:
        """按写入的逆序尽力回滚每一条；单条失败不打断其它条，逐条收集错误返回。

        只回滚 `prepared` 里传进来的条目（调用方已裁剪为"真正碰过盘"的集合）。
        """
        errors: list[RollbackError] = []
        for change, relative, target in reversed(list(prepared)):
            change_id = str(change.id)
            try:
                ApplyEngine._rollback(
                    backups,
                    backup_path=backup_paths.get(change_id),
                    relative=relative,
                    target=target,
                    wrote=True,
                )
            except Exception as exc:  # noqa: BLE001 - 回滚失败要留痕，不能吞
                logger.exception(
                    "apply.rollback 失败 change=%s file=%s", change_id, change.file_path
                )
                errors.append(
                    {"change_id": change_id, "file_path": change.file_path, "error": str(exc)}
                )
        return errors

    @staticmethod
    def _assert_unchanged(change: VirtualChange, target: Path) -> None:
        if _kind_of(change) is ChangeKind.DELETE:
            # 删除类：文件必须存在且与提案基线一致，才能安全备份 + unlink
            if not target.exists():
                raise ConflictError(
                    f"文件 {change.file_path} 已不存在，删除提案无法复验原始内容",
                    details={"file_path": change.file_path},
                )
            if not target.is_file():
                raise ConflictError(
                    f"路径 {change.file_path} 不是普通文件，拒绝 Apply",
                    details={"file_path": change.file_path},
                )
            current_hash = content_hash(target.read_text(encoding="utf-8"))
            if current_hash != change.original_hash:
                raise ConflictError(
                    f"文件 {change.file_path} 已被改动，删除提案已过期，禁止直接 Apply",
                    details={
                        "file_path": change.file_path,
                        "expected_hash": change.original_hash,
                        "actual_hash": current_hash,
                    },
                )
            return
        if not target.exists():
            # 磁盘上没有这个文件：只有"新建文件"的提案才允许继续
            if change.original_content != "":
                raise ConflictError(
                    f"文件 {change.file_path} 已不存在，提案基于的原始内容无法复验",
                    details={"file_path": change.file_path},
                )
            return
        if not target.is_file():
            raise ConflictError(
                f"路径 {change.file_path} 不是普通文件，拒绝 Apply",
                details={"file_path": change.file_path},
            )
        current_hash = content_hash(target.read_text(encoding="utf-8"))
        if current_hash != change.original_hash:
            raise ConflictError(
                f"文件 {change.file_path} 已被改动，提案已过期，禁止直接 Apply；"
                "请让 Agent 基于最新文件重新提交提案，或手动修改该文件",
                details={
                    "file_path": change.file_path,
                    "expected_hash": change.original_hash,
                    "actual_hash": current_hash,
                },
            )

    @staticmethod
    def _write(change: VirtualChange, target: Path) -> None:
        if _kind_of(change) is ChangeKind.DELETE:
            target.unlink()
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        # 必须以字节落盘：Path.write_text 在 newline=None 时会把 \n 翻成 os.linesep，
        # Windows 上会把提案的 LF 变成 CRLF，写出的文件与提案/备份字节不一致，污染后续
        # hash 比对与 git 差异。提案内容与备份都是字节级事实，这里保持字节级写入。
        target.write_bytes((change.proposed_content or "").encode("utf-8"))

    @staticmethod
    def _verify(change: VirtualChange, target: Path) -> None:
        if _kind_of(change) is ChangeKind.DELETE:
            if target.exists():
                raise ApplyFailedError(
                    f"删除后文件仍存在：{change.file_path}",
                    details={"file_path": change.file_path},
                )
            return
        if not target.is_file():
            raise ApplyFailedError(
                f"写入后文件不存在：{change.file_path}", details={"file_path": change.file_path}
            )
        actual = content_hash(target.read_text(encoding="utf-8"))
        expected = content_hash(change.proposed_content or "")
        if actual != expected:
            raise ApplyFailedError(
                f"写入后内容与提案不一致：{change.file_path}",
                details={
                    "file_path": change.file_path,
                    "expected_hash": expected,
                    "actual_hash": actual,
                },
            )

    def _run_tests(self, root: Path) -> TestOutcome | None:
        if not self._test_command:
            return None
        return TestRunner(timeout_seconds=self._test_timeout).run(self._test_command, cwd=root)

    @staticmethod
    def _rollback(
        backups: BackupService,
        *,
        backup_path: Path | None,
        relative: Path,
        target: Path,
        wrote: bool,
    ) -> None:
        """还原原文件；只碰 apply() 一开始就校验过的 `target`，回滚阶段不再重新解析路径。"""
        if backup_path is not None and backup_path.is_file():
            backups.restore(backup_path=backup_path, relative_target=relative)
            return
        # 新建文件：没有备份可还原，直接删掉刚写下的文件
        if wrote and target.is_file():
            target.unlink()
            logger.warning("apply.rollback 删除新建文件 %s", target)


def _journal(on_phase: Callable[[str], None] | None, phase: str) -> None:
    """推进批日志阶段；写不进去就抛（继续碰盘会留下没有记录的操作）。"""
    if on_phase is not None:
        on_phase(phase)


# --- 崩溃恢复（P0-1 §2.2）---
#
# 服务被杀时批日志停在 in_progress，磁盘可能停在任意一步。恢复算法只认磁盘事实、
# 逐条幂等处理，绝不按 phase 猜进度：同一算法重复跑的结果必须与跑一次相同。


@dataclass(frozen=True)
class RecoveryEntry:
    """恢复算法需要的提案事实（从库里读，不信内存/磁盘里的副本）。

    `proposed_hash` 为 None 表示 delete 类：应用后的预期状态是"文件不存在"。
    """

    change_id: str
    file_path: str
    original_hash: str
    original_content: str
    proposed_hash: str | None
    kind: str = ChangeKind.MODIFY.value


@dataclass(frozen=True)
class ChangeRecovery:
    """一条 change 的恢复结论（写入批日志的 recovery_note 与事件）。"""

    change_id: str
    file_path: str
    outcome: str
    detail: str
    backup_path: str | None


def recover_batch_on_disk(
    root: Path,
    entries: Sequence[RecoveryEntry],
    *,
    backup_rel_root: str = BACKUP_RELATIVE_ROOT.as_posix(),
) -> list[ChangeRecovery]:
    """把一批中断的 Apply 恢复到"可以安全重试"的状态（幂等）。

    单条处理失败不打断其它条（各自记成 needs_attention），且整段与正常 Apply
    共用同一把进程内锁——恢复不会插进正在执行的批次中间。
    """
    backups = BackupService(workspace_root=root)
    results: list[ChangeRecovery] = []
    with _APPLY_LOCK:
        for entry in entries:
            try:
                results.append(_recover_one(root, backups, entry, backup_rel_root))
            except Exception as exc:  # noqa: BLE001 - 恢复失败必须留痕，转成显式状态
                logger.exception("apply.recovery 单条恢复失败 change=%s", entry.change_id)
                results.append(
                    ChangeRecovery(
                        entry.change_id,
                        entry.file_path,
                        OUTCOME_NEEDS_ATTENTION,
                        f"恢复过程出错：{exc}",
                        None,
                    )
                )
    return results


def _recover_one(
    root: Path,
    backups: BackupService,
    entry: RecoveryEntry,
    backup_rel_root: str,
) -> ChangeRecovery:
    """单条恢复：只认磁盘事实，且只在"内容确是原文或提案内容"时才由 Flux 自行收拾。

    内容既非原文也非提案内容 = 崩溃后被外部改动：**无论有无备份一律不覆盖**，转
    needs_attention 交人工决策（2026-10-05 定案，见设计 §2.2）。有备份的"修改类"
    同样适用——有备份就写回，等于拿旧内容盖掉用户在崩溃窗口里的手动修改。
    """
    try:
        relative = safe_relative_path(entry.file_path)
        target = resolve_within_root(root, relative)
        backup_path = resolve_within_root(root, Path(backup_rel_root) / entry.change_id / relative)
    except ValidationError as exc:
        return ChangeRecovery(
            entry.change_id,
            entry.file_path,
            OUTCOME_NEEDS_ATTENTION,
            f"路径校验失败：{exc.message}",
            None,
        )

    backup_is_file = backup_path.is_file()
    target_exists = target.exists()
    target_is_file = target.is_file()
    current_hash = content_hash(target.read_text(encoding="utf-8")) if target_is_file else None

    if backup_is_file:
        # 修改/删除类：备份就是"改动前的事实"
        if target_is_file and current_hash == entry.original_hash:
            return ChangeRecovery(
                entry.change_id,
                entry.file_path,
                OUTCOME_SKIPPED,
                "文件已是改动前的内容",
                str(backup_path),
            )
        if entry.kind == ChangeKind.DELETE.value and not target_exists:
            # 删除类：文件已按提案被删掉（崩溃发生在 unlink 之后），从备份还原
            return _restore_from_backup(root, backups, entry, relative, backup_path)
        if target_is_file and current_hash != entry.proposed_hash:
            # 既非原文也非 Flux 写入的内容 → 崩溃后被外部改动，不许拿备份覆盖
            return ChangeRecovery(
                entry.change_id,
                entry.file_path,
                OUTCOME_NEEDS_ATTENTION,
                "崩溃后检测到外部修改，未覆盖当前内容",
                str(backup_path),
            )
        if not target_exists:
            # 备份在、文件却没了 → 崩溃后被外部删除。绝不自动复活（2026-10-05 定案）
            return ChangeRecovery(
                entry.change_id,
                entry.file_path,
                OUTCOME_NEEDS_ATTENTION,
                "崩溃后检测到文件被删除，未自动恢复（备份已保留）",
                str(backup_path),
            )
        if not target_is_file:
            return ChangeRecovery(
                entry.change_id,
                entry.file_path,
                OUTCOME_NEEDS_ATTENTION,
                "目标路径不是普通文件",
                str(backup_path),
            )
        return _restore_from_backup(root, backups, entry, relative, backup_path)

    # 无备份：要么是新建类，要么崩溃发生在备份完成之前（文件还没被动过）
    if not target_exists:
        return ChangeRecovery(
            entry.change_id, entry.file_path, OUTCOME_SKIPPED, "目标文件不存在，无需处理", None
        )
    if not target_is_file:
        return ChangeRecovery(
            entry.change_id,
            entry.file_path,
            OUTCOME_NEEDS_ATTENTION,
            "目标路径不是普通文件",
            None,
        )
    if current_hash == entry.original_hash:
        return ChangeRecovery(
            entry.change_id, entry.file_path, OUTCOME_SKIPPED, "文件仍是改动前的内容", None
        )
    if current_hash == entry.proposed_hash:
        if entry.original_content == "":
            target.unlink()
            logger.warning("apply.recovery 删除崩溃遗留的新建文件 %s", target)
            return ChangeRecovery(
                entry.change_id,
                entry.file_path,
                OUTCOME_DELETED,
                "已删除崩溃遗留的新建文件",
                None,
            )
        # 文件原本存在（所以本应有备份）：写入已完成但备份缺失，删除等于毁数据
        return ChangeRecovery(
            entry.change_id,
            entry.file_path,
            OUTCOME_NEEDS_ATTENTION,
            "备份缺失，无法还原改动前的内容",
            None,
        )
    return ChangeRecovery(
        entry.change_id,
        entry.file_path,
        OUTCOME_NEEDS_ATTENTION,
        "崩溃后检测到外部修改，未覆盖当前内容",
        None,
    )


# --- 正式回滚（P1-2 §5.2）---
#
# 与崩溃恢复不同：回滚是人工显式动作，前置条件更严——批必须 applied、每条 change 必须
# APPLIED、当前内容必须与提案写入的内容一致。先全量预检（不写盘），再按批内逆序执行。


@dataclass(frozen=True)
class RollbackItem:
    """回滚算法需要的一条提案事实（从库里读）。proposed_hash 为 None 表示 delete。"""

    change_id: str
    file_path: str
    kind: str
    original_hash: str
    proposed_hash: str | None
    backup_path: str | None


#: 单条回滚的规划结果
_ROLLBACK_SKIP = "skip"
_ROLLBACK_RESTORE = "restore"
_ROLLBACK_DELETE = "delete"


def rollback_batch_on_disk(
    root: Path,
    items: Sequence[RollbackItem],
    *,
    backup_rel_root: str = BACKUP_RELATIVE_ROOT.as_posix(),
) -> list[ChangeRecovery]:
    """把一批已落盘的改动整体撤销（P1-2）：先全量预检，再按逆序回滚。

    预检不通过（文件被外部改动 / 备份缺失）时抛 ConflictError，**一个字节都不写**。
    已恢复（当前内容已是 original）的条目幂等跳过，因此部分失败后可重复执行。
    执行阶段单条失败则停止并抛 ApplyFailedError，`details["processed"]` 记录已恢复的条目。
    """
    backups = BackupService(workspace_root=root)
    with _APPLY_LOCK:
        plans = [_plan_rollback(root, item, backup_rel_root) for item in items]
        results: list[ChangeRecovery] = []
        for item, plan in zip(items, plans, strict=True):
            if plan == _ROLLBACK_SKIP:
                results.append(
                    ChangeRecovery(
                        item.change_id,
                        item.file_path,
                        OUTCOME_SKIPPED,
                        "文件已是改动前的内容",
                        item.backup_path,
                    )
                )
        for item, plan in reversed(list(zip(items, plans, strict=True))):
            if plan == _ROLLBACK_SKIP:
                continue
            try:
                _apply_rollback(root, backups, item, plan, backup_rel_root)
            except Exception as exc:  # noqa: BLE001 - 回滚失败必须留痕
                raise ApplyFailedError(
                    f"回滚失败：{item.file_path}（{exc}）",
                    details={
                        "change_id": item.change_id,
                        "file_path": item.file_path,
                        "processed": [r.change_id for r in results],
                    },
                ) from exc
            outcome = OUTCOME_DELETED if plan == _ROLLBACK_DELETE else OUTCOME_RESTORED
            detail = (
                "已删除新建的文件" if plan == _ROLLBACK_DELETE else "已从备份还原为改动前的内容"
            )
            results.append(
                ChangeRecovery(item.change_id, item.file_path, outcome, detail, item.backup_path)
            )
        return results


def _plan_rollback(root: Path, item: RollbackItem, backup_rel_root: str) -> str:
    """规划一条回滚动作，并在预检阶段拒绝一切不安全情形（不写盘）。"""
    relative = safe_relative_path(item.file_path)
    target = resolve_within_root(root, relative)
    kind = item.kind or ChangeKind.MODIFY.value
    exists = target.exists()
    is_file = target.is_file()
    current = content_hash(target.read_text(encoding="utf-8")) if is_file else None
    backup = _resolve_backup(root, item, relative, backup_rel_root)

    if kind == ChangeKind.DELETE.value:
        if backup is None:
            raise ConflictError(
                f"备份不存在，无法回滚删除：{item.file_path}",
                details={"change_id": item.change_id, "file_path": item.file_path},
            )
        if not exists:
            # 删除类的应用态就是"文件不存在"：需要还原
            return _ROLLBACK_RESTORE
        if is_file and current == item.original_hash:
            return _ROLLBACK_SKIP
        raise ConflictError(
            f"文件已变化，拒绝覆盖回滚：{item.file_path}",
            details={"change_id": item.change_id, "file_path": item.file_path},
        )

    if kind == ChangeKind.CREATE.value:
        if not exists:
            return _ROLLBACK_SKIP
        if is_file and current == item.proposed_hash:
            return _ROLLBACK_DELETE
        raise ConflictError(
            f"文件已变化，拒绝删除回滚：{item.file_path}",
            details={"change_id": item.change_id, "file_path": item.file_path},
        )

    # modify
    if backup is None:
        raise ConflictError(
            f"备份不存在，无法回滚：{item.file_path}",
            details={"change_id": item.change_id, "file_path": item.file_path},
        )
    if not exists:
        raise ConflictError(
            f"文件已不存在，无法回滚：{item.file_path}",
            details={"change_id": item.change_id, "file_path": item.file_path},
        )
    if not is_file:
        raise ConflictError(
            f"路径不是普通文件，拒绝回滚：{item.file_path}",
            details={"change_id": item.change_id, "file_path": item.file_path},
        )
    if current == item.original_hash:
        return _ROLLBACK_SKIP
    if current == item.proposed_hash:
        return _ROLLBACK_RESTORE
    raise ConflictError(
        f"文件已变化，拒绝覆盖回滚：{item.file_path}",
        details={"change_id": item.change_id, "file_path": item.file_path},
    )


def _resolve_backup(
    root: Path, item: RollbackItem, relative: Path, backup_rel_root: str
) -> Path | None:
    """定位一条 change 的备份文件；不存在返回 None（预检据此拒绝）。"""
    if item.backup_path:
        candidate = Path(item.backup_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            candidate = resolve_within_root(root, candidate.relative_to(root))
        except (ValueError, ValidationError):
            return None
    else:
        candidate = resolve_within_root(root, Path(backup_rel_root) / item.change_id / relative)
    return candidate if candidate.is_file() else None


def _apply_rollback(
    root: Path,
    backups: BackupService,
    item: RollbackItem,
    plan: str,
    backup_rel_root: str,
) -> None:
    """执行一条回滚并复核结果。"""
    relative = safe_relative_path(item.file_path)
    target = resolve_within_root(root, relative)
    if plan == _ROLLBACK_DELETE:
        target.unlink()
        if target.exists():
            raise ApplyFailedError(
                f"回滚后文件仍存在：{item.file_path}", details={"file_path": item.file_path}
            )
        return
    backup = _resolve_backup(root, item, relative, backup_rel_root)
    if backup is None:
        raise ApplyFailedError(
            f"备份缺失，无法还原：{item.file_path}", details={"file_path": item.file_path}
        )
    backups.restore(backup_path=backup, relative_target=relative)
    if (
        not target.is_file()
        or content_hash(target.read_text(encoding="utf-8")) != item.original_hash
    ):
        raise ApplyFailedError(
            f"回滚后内容与备份不一致：{item.file_path}", details={"file_path": item.file_path}
        )


def _restore_from_backup(
    root: Path,
    backups: BackupService,
    entry: RecoveryEntry,
    relative: Path,
    backup_path: Path,
) -> ChangeRecovery:
    """从备份还原为改动前原文，并复核结果（修改/删除类崩溃恢复的收尾）。"""
    target = resolve_within_root(root, relative)
    try:
        backups.restore(backup_path=backup_path, relative_target=relative)
    except Exception as exc:  # noqa: BLE001 - 单条还原失败转成显式状态
        return ChangeRecovery(
            entry.change_id,
            entry.file_path,
            OUTCOME_NEEDS_ATTENTION,
            f"从备份还原失败：{exc}",
            str(backup_path),
        )
    if (
        not target.is_file()
        or content_hash(target.read_text(encoding="utf-8")) != entry.original_hash
    ):
        return ChangeRecovery(
            entry.change_id,
            entry.file_path,
            OUTCOME_NEEDS_ATTENTION,
            "还原后内容与备份不一致",
            str(backup_path),
        )
    return ChangeRecovery(
        entry.change_id,
        entry.file_path,
        OUTCOME_RESTORED,
        "已从备份还原为改动前的内容",
        str(backup_path),
    )
