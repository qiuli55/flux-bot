"""Apply 崩溃恢复测试（P0-1 设计 §2.3：T-APPLY-010~014 + 边界）。

进程被杀时磁盘可能停在任意一步、批日志停在 in_progress。这里用"手工造崩溃现场"
复现各类中断，断言恢复算法只认磁盘事实、幂等、且绝不覆盖用户的外部改动。

不变量：
- 恢复后提案必须回到 accepted（可安全重试）或明确 failed（留原因），不存在无法解释的中间态；
- 需要人工介入时批置 needs_attention，且磁盘上的文件一个字节都不许被进一步破坏。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flux.config import Settings
from flux.container import Container
from flux.core.event.bus import Events
from flux.core.virtual_workspace.apply_engine import PHASE_TESTING
from flux.core.virtual_workspace.backup import BACKUP_RELATIVE_ROOT, BackupService
from flux.enums import RecoveryResolution, VirtualChangeStatus
from flux.errors import ConflictError, ValidationError
from flux.main import create_app

ORIGINAL = "def login(user):\n    return False\n"
PROPOSED = "def login(user):\n    return check_password(user)\n"
FRESH = "# 新建模块\n"
EXTERNAL = "外部改的内容\n"


def _write(root: Path, relative: str, content: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _propose(
    container: Container,
    *,
    file_path: str = "auth/login.py",
    original: str = ORIGINAL,
    proposed: str = PROPOSED,
):
    return asyncio.run(
        container.workspace.propose(
            file_path=file_path,
            original_content=original,
            proposed_content=proposed,
            agent_source="developer-agent",
        )
    )


def _interrupt(container: Container, change_id: str) -> str:
    """把一条 accepted 提案推进到"Apply 进行中"并落一条 in_progress 批。

    顺序与真实 apply_many 一致：先抢占 applying，再落批记录——然后"进程被杀"，
    也就是本函数返回之后不再有任何推进。
    """
    asyncio.run(container.proposal_repo.claim_applying(change_id))
    batch = asyncio.run(
        container.batch_repo.create(
            change_ids=[change_id],
            backup_root=BACKUP_RELATIVE_ROOT.as_posix(),
        )
    )
    return str(batch.id)


def _batch(container: Container, batch_id: str):
    return asyncio.run(container.batch_repo.get(batch_id))


def _change(container: Container, change_id: str):
    return asyncio.run(container.proposal_repo.get(change_id))


def _accept(container: Container, change_id: str) -> None:
    asyncio.run(container.workspace.accept(change_id))


def _recover(container: Container):
    return asyncio.run(container.workspace.recover_interrupted_applies())


def _prepare_written_modify(container: Container, workspace_root: Path):
    """造一个"已备份 + 已写盘"的修改类崩溃现场，返回 (目标文件, 提案, 批 ID)。"""
    target = _write(workspace_root, "auth/login.py", ORIGINAL)
    proposal = _propose(container)
    _accept(container, str(proposal.id))
    BackupService(workspace_root=workspace_root).backup(
        change_id=str(proposal.id),
        file_path="auth/login.py",
        relative_target=Path("auth/login.py"),
    )
    target.write_text(PROPOSED, encoding="utf-8")
    batch_id = _interrupt(container, str(proposal.id))
    container.bus.clear()
    return target, proposal, batch_id


# --- T-APPLY-010：正常 Apply 的批日志与阶段序列 ---


def test_normal_apply_records_batch_and_full_phase_sequence(
    apply_container: Container, workspace_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(workspace_root, "auth/login.py", ORIGINAL)
    proposal = _propose(apply_container)

    phases: list[str] = []
    original_set_phase = apply_container.batch_repo.set_phase

    async def recording_set_phase(batch_id, phase):
        phases.append(phase)
        return await original_set_phase(batch_id, phase)

    monkeypatch.setattr(apply_container.batch_repo, "set_phase", recording_set_phase)

    applied = asyncio.run(apply_container.workspace.apply(str(proposal.id)))

    assert applied.status == VirtualChangeStatus.APPLIED.value
    assert applied.backup_path and Path(applied.backup_path).is_file()
    # 阶段必须先于对应磁盘操作落库，顺序不能少也不能乱
    assert phases == ["backed_up", "writing", "verifying", "testing"]

    batches = asyncio.run(apply_container.batch_repo.list_recent())
    assert len(batches) == 1
    assert batches[0].status == "applied"
    assert batches[0].phase == PHASE_TESTING
    assert batches[0].finished_at is not None
    assert batches[0].change_ids == [str(proposal.id)]


# --- T-APPLY-011 / T-APPLY-012 / T-APPLY-013：已写盘的修改类崩溃 ---


def test_recovery_restores_written_modify_change_and_reopens_it(
    apply_container: Container, workspace_root: Path
) -> None:
    target, proposal, batch_id = _prepare_written_modify(apply_container, workspace_root)

    recovered = _recover(apply_container)

    assert [str(batch.id) for batch in recovered] == [batch_id]
    assert recovered[0].status == "recovered"
    assert recovered[0].finished_at is not None
    assert "restored" in (recovered[0].recovery_note or "")
    # 磁盘回到改动前
    assert target.read_text(encoding="utf-8") == ORIGINAL
    # 提案回 accepted，并回填备份路径（补上"备份在盘上但 DB 未记录"的追踪缺口）
    change = _change(apply_container, str(proposal.id))
    assert change.status == VirtualChangeStatus.ACCEPTED.value
    assert change.backup_path and Path(change.backup_path).is_file()
    assert any(name == Events.APPLY_RECOVERED for name, _ in apply_container.bus.history)


def test_retry_after_recovery_applies_cleanly(
    apply_container: Container, workspace_root: Path
) -> None:
    """T-APPLY-012：恢复后重试必须成功且不造成二次破坏。"""
    target, proposal, _ = _prepare_written_modify(apply_container, workspace_root)
    _recover(apply_container)

    applied = asyncio.run(apply_container.workspace.apply(str(proposal.id)))

    assert applied.status == VirtualChangeStatus.APPLIED.value
    assert target.read_text(encoding="utf-8") == PROPOSED
    assert asyncio.run(apply_container.batch_repo.list_in_progress()) == []


def test_recovery_is_idempotent(apply_container: Container, workspace_root: Path) -> None:
    """T-APPLY-013：对同一批重复恢复必须无副作用。"""
    target, proposal, _ = _prepare_written_modify(apply_container, workspace_root)
    _recover(apply_container)

    again = _recover(apply_container)

    assert again == []
    assert target.read_text(encoding="utf-8") == ORIGINAL
    assert _change(apply_container, str(proposal.id)).status == VirtualChangeStatus.ACCEPTED.value


# --- T-APPLY-014 与备份缺失边界 ---


def test_recovery_never_overwrites_external_modification(
    apply_container: Container, workspace_root: Path
) -> None:
    """T-APPLY-014：崩溃后文件被外部改动 → 不覆盖、不删除，转 needs_attention 交人工。

    新建类提案本就没有备份；崩溃后文件内容既不是原文（空）也不是提案内容，
    说明有人在崩溃之后动过它——此时"自动修好"才是真正的破坏。
    """
    _write(workspace_root, "app/new.py", EXTERNAL)
    proposal = _propose(apply_container, file_path="app/new.py", original="", proposed=FRESH)
    _accept(apply_container, str(proposal.id))
    _interrupt(apply_container, str(proposal.id))

    recovered = _recover(apply_container)

    assert recovered[0].status == "needs_attention"
    assert "外部修改" in (recovered[0].recovery_note or "")
    assert (workspace_root / "app" / "new.py").read_text(encoding="utf-8") == EXTERNAL
    change = _change(apply_container, str(proposal.id))
    assert change.status == VirtualChangeStatus.FAILED.value
    assert "外部修改" in (change.apply_error or "")


def test_recovery_does_not_overwrite_external_edit_of_existing_file(
    apply_container: Container, workspace_root: Path
) -> None:
    """修改类（有备份）崩溃后被外部改动 → 同样不覆盖，转 needs_attention。

    "有备份就写回备份"与"绝不覆盖外部改动"这两条规则在这里冲突，2026-10-05 定案取后者。
    """
    target, proposal, _ = _prepare_written_modify(apply_container, workspace_root)
    external = "def login(user):\n    return user.is_active\n"
    target.write_text(external, encoding="utf-8")

    recovered = _recover(apply_container)

    assert recovered[0].status == "needs_attention"
    assert "外部修改" in (recovered[0].recovery_note or "")
    # 用户手改的内容必须原样保留
    assert target.read_text(encoding="utf-8") == external
    change = _change(apply_container, str(proposal.id))
    assert change.status == VirtualChangeStatus.FAILED.value
    assert "外部修改" in (change.apply_error or "")
    # 备份仍在盘上且是改动前原文，供人工决策时选"覆盖备份"
    backup = workspace_root / ".flux" / "backups" / str(proposal.id) / "auth" / "login.py"
    assert backup.is_file()
    assert backup.read_text(encoding="utf-8") == ORIGINAL


def test_recovery_deletes_crashed_new_file_without_backup(
    apply_container: Container, workspace_root: Path
) -> None:
    """新建文件崩溃（无备份 + 内容 == 提案）→ 删除后提案回 accepted。"""
    proposal = _propose(apply_container, file_path="app/new.py", original="", proposed=FRESH)
    _accept(apply_container, str(proposal.id))
    _write(workspace_root, "app/new.py", FRESH)
    _interrupt(apply_container, str(proposal.id))

    recovered = _recover(apply_container)

    assert recovered[0].status == "recovered"
    assert "deleted" in (recovered[0].recovery_note or "")
    assert not (workspace_root / "app" / "new.py").exists()
    assert _change(apply_container, str(proposal.id)).status == VirtualChangeStatus.ACCEPTED.value


def test_recovery_flags_missing_backup_instead_of_deleting_existing_file(
    apply_container: Container, workspace_root: Path
) -> None:
    """修改类但备份缺失（内容 == 提案、原文非空）→ needs_attention，绝不删除已有文件。"""
    target = _write(workspace_root, "auth/login.py", PROPOSED)
    proposal = _propose(apply_container)
    _accept(apply_container, str(proposal.id))
    _interrupt(apply_container, str(proposal.id))

    recovered = _recover(apply_container)

    assert recovered[0].status == "needs_attention"
    assert "备份缺失" in (recovered[0].recovery_note or "")
    assert target.read_text(encoding="utf-8") == PROPOSED
    change = _change(apply_container, str(proposal.id))
    assert change.status == VirtualChangeStatus.FAILED.value
    assert "备份缺失" in (change.apply_error or "")


def test_recovery_without_workspace_root_flags_needs_attention(container: Container) -> None:
    """未配置工作区根时无法对账磁盘：批置 needs_attention，提案状态一个都不许猜。"""
    proposal = _propose(container)
    _accept(container, str(proposal.id))
    batch_id = _interrupt(container, str(proposal.id))

    recovered = _recover(container)

    assert [str(batch.id) for batch in recovered] == [batch_id]
    assert recovered[0].status == "needs_attention"
    assert "未配置工作区根目录" in (recovered[0].error or "")
    # 无法对账就不动提案：仍是 applying，修好配置后下一轮扫描会继续处理
    assert _change(container, str(proposal.id)).status == VirtualChangeStatus.APPLYING.value


# --- 备份可追踪：重试覆盖前留 .prev 快照 ---


def test_backup_keeps_prev_snapshot_before_overwrite(workspace_root: Path) -> None:
    target = _write(workspace_root, "a.py", ORIGINAL)
    backups = BackupService(workspace_root=workspace_root)
    backups.backup(change_id="c1", file_path="a.py", relative_target=Path("a.py"))

    target.write_text("第二轮待备份内容\n", encoding="utf-8")
    second = backups.backup(change_id="c1", file_path="a.py", relative_target=Path("a.py"))

    assert second is not None
    prev = second.with_name(second.name + ".prev")
    assert prev.is_file()
    assert prev.read_text(encoding="utf-8") == ORIGINAL
    assert second.read_text(encoding="utf-8") == "第二轮待备份内容\n"


# --- 预检失败契约必须保持（回归易踩）---


def test_preflight_failure_keeps_change_accepted_and_marks_batch_failed(
    apply_container: Container, workspace_root: Path
) -> None:
    """预检未通过（文件被用户改过）→ 还没碰盘：提案留 accepted、apply_error 保持空，批 failed。"""
    _write(workspace_root, "auth/login.py", "user 自己改的\n")
    proposal = _propose(apply_container)

    with pytest.raises(ConflictError):
        asyncio.run(apply_container.workspace.apply(str(proposal.id)))

    change = _change(apply_container, str(proposal.id))
    assert change.status == VirtualChangeStatus.ACCEPTED.value
    assert change.apply_error is None
    batches = asyncio.run(apply_container.batch_repo.list_recent())
    assert batches[0].status == "failed"
    assert "预检未通过" in (batches[0].error or "")


# --- 人工决策入口（P0-1 §2.2，2026-10-05 定案）---


EXTERNAL_EDIT = "def login(user):\n    return user.is_active\n"


def _prepare_pending_recovery(container: Container, workspace_root: Path):
    """造一个"修改类 + 有备份 + 崩溃后被外部改动"的待决策现场。"""
    target, proposal, batch_id = _prepare_written_modify(container, workspace_root)
    target.write_text(EXTERNAL_EDIT, encoding="utf-8")
    _recover(container)
    return target, proposal, batch_id


def test_pending_recovery_item_exposes_three_versions(
    apply_container: Container, workspace_root: Path
) -> None:
    _, proposal, batch_id = _prepare_pending_recovery(apply_container, workspace_root)

    items = asyncio.run(apply_container.workspace.list_pending_recovery_items())

    assert len(items) == 1
    item = items[0]
    assert item.change_id == str(proposal.id)
    assert item.batch_id == batch_id
    assert item.original_content == ORIGINAL
    assert item.disk_content == EXTERNAL_EDIT
    assert item.proposed_content == PROPOSED
    assert item.backup_available is True
    assert item.disk_state == "modified"
    assert "外部修改" in item.note


def test_resolve_cover_restores_original_and_reopens_change(
    apply_container: Container, workspace_root: Path
) -> None:
    target, proposal, batch_id = _prepare_pending_recovery(apply_container, workspace_root)

    updated = asyncio.run(
        apply_container.workspace.resolve_recovery(str(proposal.id), RecoveryResolution.COVER)
    )

    assert updated.status == VirtualChangeStatus.ACCEPTED.value
    assert updated.recovery_resolution == RecoveryResolution.COVER.value
    assert updated.apply_error is None
    assert target.read_text(encoding="utf-8") == ORIGINAL
    assert _batch(apply_container, batch_id).status == "recovered"
    assert asyncio.run(apply_container.workspace.list_pending_recovery_items()) == []


def test_resolve_keep_keeps_disk_and_voids_change(
    apply_container: Container, workspace_root: Path
) -> None:
    target, proposal, batch_id = _prepare_pending_recovery(apply_container, workspace_root)

    updated = asyncio.run(
        apply_container.workspace.resolve_recovery(str(proposal.id), RecoveryResolution.KEEP)
    )

    assert updated.status == VirtualChangeStatus.FAILED.value
    assert updated.recovery_resolution == RecoveryResolution.KEEP.value
    assert "保持现状" in (updated.apply_error or "")
    # 用户手改的内容原样保留
    assert target.read_text(encoding="utf-8") == EXTERNAL_EDIT
    assert _batch(apply_container, batch_id).status == "recovered"
    assert asyncio.run(apply_container.workspace.list_pending_recovery_items()) == []


def test_resolve_recovery_rejects_second_decision(
    apply_container: Container, workspace_root: Path
) -> None:
    _, proposal, _ = _prepare_pending_recovery(apply_container, workspace_root)
    asyncio.run(
        apply_container.workspace.resolve_recovery(str(proposal.id), RecoveryResolution.KEEP)
    )

    with pytest.raises(ConflictError):
        asyncio.run(
            apply_container.workspace.resolve_recovery(str(proposal.id), RecoveryResolution.COVER)
        )


def test_resolve_cover_without_backup_is_rejected(
    apply_container: Container, workspace_root: Path
) -> None:
    """备份缺失（新建类）时没有东西可覆盖：明确 409，不静默降级成保持现状。"""
    _write(workspace_root, "app/new.py", EXTERNAL)
    proposal = _propose(apply_container, file_path="app/new.py", original="", proposed=FRESH)
    _accept(apply_container, str(proposal.id))
    _interrupt(apply_container, str(proposal.id))
    _recover(apply_container)

    items = asyncio.run(apply_container.workspace.list_pending_recovery_items())
    assert len(items) == 1 and items[0].backup_available is False

    with pytest.raises(ConflictError):
        asyncio.run(
            apply_container.workspace.resolve_recovery(str(proposal.id), RecoveryResolution.COVER)
        )
    assert (workspace_root / "app" / "new.py").read_text(encoding="utf-8") == EXTERNAL


def test_resolve_recovery_rejects_non_pending_change(
    apply_container: Container, workspace_root: Path
) -> None:
    _write(workspace_root, "auth/login.py", ORIGINAL)
    proposal = _propose(apply_container)

    with pytest.raises(ValidationError):
        asyncio.run(
            apply_container.workspace.resolve_recovery(str(proposal.id), RecoveryResolution.KEEP)
        )


def test_recovery_api_lists_and_resolves(
    apply_settings: Settings, db_schema: None, workspace_root: Path
) -> None:
    """REST 闭环：列表看到待决策项（含三版本）→ keep 决策 → 待决策清空且文件未被动。"""
    container = Container(apply_settings)
    target, proposal, _ = _prepare_pending_recovery(container, workspace_root)

    app = create_app(apply_settings)
    with TestClient(app) as client:
        listed = client.get("/api/v1/workspace/recovery").json()
        assert listed["success"] is True
        assert listed["metadata"]["count"] == 1
        assert listed["data"][0]["change_id"] == str(proposal.id)
        assert listed["data"][0]["backup_available"] is True
        assert listed["data"][0]["disk_content"] == EXTERNAL_EDIT

        resolved = client.post(
            "/api/v1/workspace/recovery/resolve",
            json={"change_id": str(proposal.id), "action": "keep"},
        ).json()
        assert resolved["success"] is True
        assert resolved["data"]["recovery_resolution"] == "keep"
        assert resolved["data"]["status"] == VirtualChangeStatus.FAILED.value

        assert client.get("/api/v1/workspace/recovery").json()["metadata"]["count"] == 0

    assert target.read_text(encoding="utf-8") == EXTERNAL_EDIT
