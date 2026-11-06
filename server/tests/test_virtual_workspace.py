"""Virtual Workspace 测试（主规格 §7；实施计划 ④）。

提案的权威存储是 virtual_changes 表，因此这里全部走真实数据库，
并且专门验证"重启后审核队列不丢"这条 M1 起就确立的原则。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from flux.container import Container
from flux.core.event.bus import Events
from flux.core.virtual_workspace.diff_engine import content_hash
from flux.core.virtual_workspace.proposal_parser import CodeChangeSet, FileChange
from flux.core.virtual_workspace.service import (
    EXPIRE_REASON_SUPERSEDED,
    EXPIRE_REASON_TIMEOUT,
)
from flux.enums import VirtualChangeStatus
from flux.errors import (
    ApplyFailedError,
    ConflictError,
    InvalidTransitionError,
    NotFoundError,
    ProposalExpiredError,
    ValidationError,
)
from tests.conftest import python_script

ORIGINAL = "def login(user):\n    return False\n"
PROPOSED = "def login(user):\n    return check_password(user)\n"


def _propose(container: Container, *, file_path: str = "auth/login.py", **kwargs: object):
    return asyncio.run(
        container.workspace.propose(
            file_path=file_path,
            original_content=ORIGINAL,
            proposed_content=PROPOSED,
            agent_source="developer-agent",
            **kwargs,  # type: ignore[arg-type]
        )
    )


def test_proposal_starts_pending_with_hash_and_stats(container: Container) -> None:
    proposal = _propose(container, reason="改成真正的校验", summary="实现登录校验")
    assert proposal.status == VirtualChangeStatus.PENDING.value
    assert proposal.original_hash == content_hash(ORIGINAL)
    assert proposal.diff and proposal.diff.startswith("---")
    assert (proposal.added_lines, proposal.removed_lines, proposal.hunks) == (1, 1, 1)
    assert proposal.agent_source == "developer-agent"
    assert proposal.reason == "改成真正的校验"
    assert proposal.summary == "实现登录校验"


def test_proposal_is_visible_through_api_shape(container: Container) -> None:
    payload = _propose(container).to_dict()
    assert payload["status"] == "pending"
    assert payload["file_path"] == "auth/login.py"
    assert payload["task_id"] is None and payload["project_id"] is None
    assert payload["added_lines"] == 1


def test_unchanged_content_is_rejected(container: Container) -> None:
    with pytest.raises(ValidationError) as excinfo:
        asyncio.run(
            container.workspace.propose(
                file_path="a.py", original_content=ORIGINAL, proposed_content=ORIGINAL
            )
        )
    assert "没有可审阅的改动" in excinfo.value.message


def test_propose_changes_maps_change_set_to_one_proposal_per_file(container: Container) -> None:
    change_set = CodeChangeSet(
        summary="新增健康检查",
        changes=(
            FileChange(path="app/main.py", content=PROPOSED, reason="注册路由"),
            FileChange(path="app/health.py", content="def health():\n    return 'ok'\n"),
        ),
    )
    created = asyncio.run(
        container.workspace.propose_changes(
            change_set,
            agent_source="developer-agent",
            original_files={"app/main.py": ORIGINAL},
        )
    )
    assert [c.file_path for c in created] == ["app/main.py", "app/health.py"]
    # 新文件：原文为空串，hash 为空内容的 hash（Apply 时据此判断"文件本不该存在"）
    new_file = created[1]
    assert new_file.original_content == ""
    assert new_file.original_hash == content_hash("")
    assert created[0].reason == "注册路由"
    assert created[0].summary == "新增健康检查"


def test_propose_changes_skips_files_without_real_change(container: Container) -> None:
    change_set = CodeChangeSet(
        summary="无意义改动",
        changes=(
            FileChange(path="app/main.py", content=PROPOSED),
            FileChange(path="app/other.py", content="same\n"),
        ),
    )
    created = asyncio.run(
        container.workspace.propose_changes(
            change_set,
            original_files={"app/main.py": ORIGINAL, "app/other.py": "same\n"},
        )
    )
    assert [c.file_path for c in created] == ["app/main.py"]
    assert len(asyncio.run(container.workspace.list())) == 1


def test_propose_changes_rejects_empty_effective_change_set(container: Container) -> None:
    change_set = CodeChangeSet(
        summary="空提案", changes=(FileChange(path="app/main.py", content=ORIGINAL),)
    )
    with pytest.raises(ValidationError) as excinfo:
        asyncio.run(
            container.workspace.propose_changes(
                change_set, original_files={"app/main.py": ORIGINAL}
            )
        )
    assert "没有任何有效改动" in excinfo.value.message


def test_accept_then_apply(apply_container: Container, workspace_root) -> None:
    """⑦ 起 apply 会真正落盘：先把被改文件按原文写到工作区里。"""
    (workspace_root / "auth").mkdir()
    (workspace_root / "auth" / "login.py").write_text(ORIGINAL, encoding="utf-8")

    proposal = _propose(apply_container)
    accepted = asyncio.run(apply_container.workspace.accept(str(proposal.id)))
    assert accepted.status == VirtualChangeStatus.ACCEPTED.value
    applied = asyncio.run(apply_container.workspace.apply(str(proposal.id)))
    assert applied.status == VirtualChangeStatus.APPLIED.value


def test_apply_pending_goes_through_accepted(apply_container: Container, workspace_root) -> None:
    """pending → accepted → applied（§7.2 / §7.5 调用方即人工批准）。"""
    (workspace_root / "auth").mkdir()
    (workspace_root / "auth" / "login.py").write_text(ORIGINAL, encoding="utf-8")

    proposal = _propose(apply_container)
    applied = asyncio.run(apply_container.workspace.apply(str(proposal.id)))
    assert applied.status == VirtualChangeStatus.APPLIED.value


def test_apply_failure_records_test_output(apply_settings, db_schema: None, workspace_root) -> None:
    """落盘后测试不过 → failed，且 apply_error 必须带测试输出（§7.6 失败必留痕）。

    只留一句"测试未通过"的话，人看不出是哪个用例挂了、下一轮该改哪里。
    """
    (workspace_root / "auth").mkdir()
    (workspace_root / "auth" / "login.py").write_text(ORIGINAL, encoding="utf-8")
    failing = workspace_root / "failing_check.py"
    failing.write_text("import sys\nprint('E   assert 1 == 2')\nsys.exit(1)\n", encoding="utf-8")
    settings = apply_settings.model_copy(update={"test_command": python_script(failing)})
    container = Container(settings)
    proposal = _propose(container)

    with pytest.raises(ApplyFailedError):
        asyncio.run(container.workspace.apply(str(proposal.id)))

    stored = asyncio.run(container.workspace.get(str(proposal.id)))
    assert stored.status == VirtualChangeStatus.FAILED.value
    assert stored.apply_error is not None
    assert "测试未通过" in stored.apply_error
    assert "assert 1 == 2" in stored.apply_error


def test_apply_without_workspace_root_is_rejected(container: Container) -> None:
    """未配置 FLUX_WORKSPACE_ROOT 时拒绝落盘：绝不默认写进某个"看起来还行"的目录。"""
    proposal = _propose(container)
    with pytest.raises(ValidationError) as excinfo:
        asyncio.run(container.workspace.apply(str(proposal.id)))
    assert "未配置工作区根目录" in excinfo.value.message
    # 状态留在 accepted（已批准但未落盘），不是 failed——这不是改动本身的问题
    assert asyncio.run(container.workspace.get(str(proposal.id))).status == "accepted"


def test_rejected_proposal_cannot_be_applied(container: Container) -> None:
    proposal = _propose(container)
    asyncio.run(container.workspace.reject(str(proposal.id)))
    with pytest.raises(InvalidTransitionError):
        asyncio.run(container.workspace.apply(str(proposal.id)))


# --- 多文件原子 Apply（P0-03）---


def test_apply_many_success_marks_every_proposal_applied(
    apply_container: Container, workspace_root
) -> None:
    (workspace_root / "a.py").write_text(ORIGINAL, encoding="utf-8")
    (workspace_root / "b.py").write_text(ORIGINAL, encoding="utf-8")
    first = _propose(apply_container, file_path="a.py")
    second = _propose(apply_container, file_path="b.py")

    applied = asyncio.run(apply_container.workspace.apply_many([str(first.id), str(second.id)]))

    assert [c.status for c in applied] == ["applied", "applied"]
    assert (workspace_root / "a.py").read_text(encoding="utf-8") == PROPOSED
    assert (workspace_root / "b.py").read_text(encoding="utf-8") == PROPOSED
    assert all(c.backup_path for c in applied)
    # 上下文闭环：整批成功要发一条带批次边界与真实测试结果的 applied 事件，
    # 任务收尾沉淀靠它写"测试到底过没过"，逐条事件带不出这个事实。
    batches = [
        payload
        for event, payload in apply_container.bus.history
        if event == Events.WORKSPACE_APPLIED
    ]
    assert len(batches) == 1
    assert sorted(batches[0]["files"]) == ["a.py", "b.py"]
    # 未配置测试命令时 test 为 None，但字段必须存在：订阅方靠"有没有值"区分跳过与通过
    assert "test" in batches[0]


def test_apply_many_failure_marks_all_failed_and_restores_files(
    apply_settings, db_schema: None, workspace_root
) -> None:
    """任一条失败 → 两条都 failed、两个文件都回原文，apply_error 都带测试输出。"""
    (workspace_root / "a.py").write_text(ORIGINAL, encoding="utf-8")
    (workspace_root / "b.py").write_text(ORIGINAL, encoding="utf-8")
    failing = workspace_root / "failing_check.py"
    failing.write_text("import sys\nprint('E   assert 1 == 2')\nsys.exit(1)\n", encoding="utf-8")
    settings = apply_settings.model_copy(update={"test_command": python_script(failing)})
    container = Container(settings)
    first = _propose(container, file_path="a.py")
    second = _propose(container, file_path="b.py")

    with pytest.raises(ApplyFailedError):
        asyncio.run(container.workspace.apply_many([str(first.id), str(second.id)]))

    for path in ("a.py", "b.py"):
        assert (workspace_root / path).read_text(encoding="utf-8") == ORIGINAL, path
    for proposal in (first, second):
        stored = asyncio.run(container.workspace.get(str(proposal.id)))
        assert stored.status == VirtualChangeStatus.FAILED.value
        assert "测试未通过" in (stored.apply_error or "")
        assert "assert 1 == 2" in (stored.apply_error or "")


def test_apply_many_preflight_keeps_everything_accepted(
    apply_container: Container, workspace_root
) -> None:
    """batch 里有一条已过期 → 整批拒绝：两个文件都不动，状态都停在 accepted。"""
    (workspace_root / "a.py").write_text(ORIGINAL, encoding="utf-8")
    (workspace_root / "b.py").write_text(ORIGINAL, encoding="utf-8")
    first = _propose(apply_container, file_path="a.py")
    second = _propose(apply_container, file_path="b.py")
    (workspace_root / "b.py").write_text("user 自己改的\n", encoding="utf-8")

    with pytest.raises(ConflictError):
        asyncio.run(apply_container.workspace.apply_many([str(first.id), str(second.id)]))

    assert (workspace_root / "a.py").read_text(encoding="utf-8") == ORIGINAL
    assert (workspace_root / "b.py").read_text(encoding="utf-8") == "user 自己改的\n"
    for proposal in (first, second):
        stored = asyncio.run(apply_container.workspace.get(str(proposal.id)))
        assert stored.status == VirtualChangeStatus.ACCEPTED.value
        assert stored.apply_error is None


def test_batch_transitions_are_all_or_nothing(container: Container) -> None:
    """accept_many 里有一条已是终态 → 整批拒绝，另一条必须还停在 pending。"""
    first = _propose(container, file_path="a.py")
    second = _propose(container, file_path="b.py")
    asyncio.run(container.workspace.reject(str(second.id)))

    with pytest.raises(InvalidTransitionError):
        asyncio.run(container.workspace.accept_many([str(first.id), str(second.id)]))

    assert asyncio.run(container.workspace.get(str(first.id))).status == "pending"
    assert asyncio.run(container.workspace.get(str(second.id))).status == "rejected"


def test_batch_transitions_reject_unknown_and_duplicate_ids(container: Container) -> None:
    first = _propose(container, file_path="a.py")

    with pytest.raises(NotFoundError):
        asyncio.run(container.workspace.reject_many([str(first.id), "不存在"]))
    with pytest.raises(ValidationError) as excinfo:
        asyncio.run(container.workspace.accept_many([str(first.id), str(first.id)]))

    assert "重复" in excinfo.value.message
    assert asyncio.run(container.workspace.get(str(first.id))).status == "pending"


def test_applied_proposal_is_terminal(apply_container: Container, workspace_root) -> None:
    (workspace_root / "auth").mkdir()
    (workspace_root / "auth" / "login.py").write_text(ORIGINAL, encoding="utf-8")

    proposal = _propose(apply_container)
    asyncio.run(apply_container.workspace.apply(str(proposal.id)))
    with pytest.raises(InvalidTransitionError):
        asyncio.run(apply_container.workspace.reject(str(proposal.id)))


def test_failed_is_terminal_and_reachable_from_accepted(container: Container) -> None:
    proposal = _propose(container)
    asyncio.run(container.workspace.accept(str(proposal.id)))
    failed = asyncio.run(container.workspace.mark_failed(str(proposal.id)))
    assert failed.status == VirtualChangeStatus.FAILED.value
    with pytest.raises(InvalidTransitionError):
        asyncio.run(container.workspace.accept(str(proposal.id)))


def test_transition_publishes_workspace_event(container: Container) -> None:
    proposal = _propose(container)
    container.bus.clear()
    asyncio.run(container.workspace.accept(str(proposal.id)))

    events = [
        payload for event, payload in container.bus.history if event == Events.WORKSPACE_CHANGED
    ]
    assert events[-1]["to"] == "accepted"
    assert events[-1]["from"] == "pending"
    assert events[-1]["change_id"] == str(proposal.id)


def test_list_filters_by_status(container: Container) -> None:
    first = _propose(container, file_path="a.py")
    _propose(container, file_path="b.py")
    asyncio.run(container.workspace.reject(str(first.id)))

    assert len(asyncio.run(container.workspace.list())) == 2
    assert len(asyncio.run(container.workspace.list(status="rejected"))) == 1
    assert len(asyncio.run(container.workspace.list(status=VirtualChangeStatus.PENDING))) == 1


def test_proposals_survive_process_restart(settings, db_schema) -> None:
    """重启后审核队列不丢：换一个全新的 Container（新引擎）仍能读到同一批提案。"""
    first_container = Container(settings)
    proposal = _propose(first_container, file_path="auth/login.py")
    proposal_id = str(proposal.id)
    asyncio.run(first_container.dispose())

    restarted = Container(settings)
    reloaded = asyncio.run(restarted.workspace.list())
    assert [str(c.id) for c in reloaded] == [proposal_id]
    assert asyncio.run(restarted.workspace.get(proposal_id)).file_path == "auth/login.py"
    asyncio.run(restarted.dispose())


def test_unknown_change_raises_not_found(container: Container) -> None:
    with pytest.raises(NotFoundError):
        asyncio.run(container.workspace.get("0f5b6f4c-0000-0000-0000-000000000000"))
    with pytest.raises(NotFoundError):
        asyncio.run(container.workspace.get("不是-uuid"))
    with pytest.raises(NotFoundError):
        asyncio.run(container.workspace.get("不存在"))


def test_propose_with_unknown_project_raises_not_found(container: Container) -> None:
    with pytest.raises(NotFoundError):
        _propose(container, project_id="0f5b6f4c-0000-0000-0000-000000000000")


def test_propose_with_malformed_owner_id_is_validation_error(container: Container) -> None:
    with pytest.raises(ValidationError):
        _propose(container, task_id="not-a-uuid")


# --- 提案过期 / 失效（P0-02 生命周期）---


def _expire_now(container: Container, change_id: object) -> None:
    """把提案的截止时间改到过去，模拟"已经过期"，不为等 24 小时而 sleep。"""
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    asyncio.run(container.proposal_repo.set_expires_at(str(change_id), past))


def test_proposal_gets_deadline_from_ttl(settings, db_schema: None) -> None:
    container = Container(settings.model_copy(update={"proposal_ttl_seconds": 60}))
    proposal = _propose(container)
    assert proposal.expires_at is not None
    # 库里读出来仍带截止时间（持久化，不是只存在内存）
    stored = asyncio.run(container.workspace.get(str(proposal.id)))
    assert stored.expires_at is not None


def test_ttl_zero_disables_expiry(settings, db_schema: None) -> None:
    container = Container(settings.model_copy(update={"proposal_ttl_seconds": 0}))
    assert _propose(container).expires_at is None


def test_expired_proposal_cannot_be_accepted(container: Container) -> None:
    proposal = _propose(container)
    _expire_now(container, proposal.id)

    with pytest.raises(ProposalExpiredError) as excinfo:
        asyncio.run(container.workspace.accept(str(proposal.id)))

    assert "已过期" in excinfo.value.message
    # 状态必须真的落到 expired 终态，而不是停在 pending
    stored = asyncio.run(container.workspace.get(str(proposal.id)))
    assert stored.status == VirtualChangeStatus.EXPIRED.value
    assert stored.expired_reason == EXPIRE_REASON_TIMEOUT
    # 终态不可再被批准
    with pytest.raises(InvalidTransitionError):
        asyncio.run(container.workspace.accept(str(proposal.id)))


def test_expired_proposal_cannot_be_applied(apply_container: Container, workspace_root) -> None:
    (workspace_root / "auth").mkdir()
    (workspace_root / "auth" / "login.py").write_text(ORIGINAL, encoding="utf-8")
    proposal = _propose(apply_container)
    _expire_now(apply_container, proposal.id)

    with pytest.raises(ProposalExpiredError):
        asyncio.run(apply_container.workspace.apply(str(proposal.id)))

    # 文件一个字节都不能动
    assert (workspace_root / "auth" / "login.py").read_text(encoding="utf-8") == ORIGINAL
    assert (
        asyncio.run(apply_container.workspace.get(str(proposal.id))).status
        == VirtualChangeStatus.EXPIRED.value
    )


def test_expire_stale_sweeps_only_timed_out_and_is_idempotent(container: Container) -> None:
    timed_out = _propose(container, file_path="a.py")
    fresh = _propose(container, file_path="b.py")
    _expire_now(container, timed_out.id)

    expired = asyncio.run(container.workspace.expire_stale())

    assert [str(c.id) for c in expired] == [str(timed_out.id)]
    assert asyncio.run(container.workspace.get(str(fresh.id))).status == "pending"
    # 幂等：再扫一次没有可清理的
    assert asyncio.run(container.workspace.expire_stale()) == []


def test_explicit_expire_publishes_event(container: Container) -> None:
    proposal = _propose(container)
    container.bus.clear()

    expired = asyncio.run(container.workspace.expire(str(proposal.id), reason="人工放弃"))

    assert expired.status == VirtualChangeStatus.EXPIRED.value
    events = [
        payload for event, payload in container.bus.history if event == Events.WORKSPACE_CHANGED
    ]
    assert events[-1]["to"] == "expired"
    assert events[-1]["reason"] == "人工放弃"


def test_new_proposal_supersedes_older_pending_same_file(container: Container) -> None:
    first = _propose(container, file_path="auth/login.py")
    second = asyncio.run(
        container.workspace.propose(
            file_path="auth/login.py",
            original_content=ORIGINAL,
            proposed_content=PROPOSED + "# v2\n",
        )
    )

    stored_first = asyncio.run(container.workspace.get(str(first.id)))
    assert stored_first.status == VirtualChangeStatus.EXPIRED.value
    assert stored_first.expired_reason == EXPIRE_REASON_SUPERSEDED
    assert asyncio.run(container.workspace.get(str(second.id))).status == "pending"


def test_supersede_is_scoped_to_same_task(container: Container) -> None:
    """不同任务各改自己的文件副本时互不干扰（同一个 file_path 但 task 不同）。"""
    first_task = "11111111-1111-1111-1111-111111111111"
    second_task = "22222222-2222-2222-2222-222222222222"
    first = _propose(container, task_id=first_task)
    second = _propose(container, task_id=second_task)

    assert asyncio.run(container.workspace.get(str(first.id))).status == "pending"
    assert asyncio.run(container.workspace.get(str(second.id))).status == "pending"


# --- 整组审核 / 落盘（P0-02）---


def _propose_group(container: Container) -> list:
    change_set = CodeChangeSet(
        summary="整组提交",
        changes=(
            FileChange(path="a.py", content=PROPOSED, reason="改 a"),
            FileChange(path="b.py", content=PROPOSED, reason="改 b"),
        ),
    )
    return asyncio.run(
        container.workspace.propose_changes(
            change_set,
            original_files={"a.py": ORIGINAL, "b.py": ORIGINAL},
        )
    )


def test_propose_changes_shares_one_group_id(container: Container) -> None:
    first, second = _propose_group(container)
    assert first.group_id is not None
    assert first.group_id == second.group_id
    assert len(asyncio.run(container.workspace.list_group(str(first.group_id)))) == 2


def test_accept_group_accepts_every_member(container: Container) -> None:
    first, second = _propose_group(container)

    accepted = asyncio.run(container.workspace.accept_group(str(first.group_id)))

    assert {c.status for c in accepted} == {"accepted"}
    assert len(asyncio.run(container.workspace.list(status="accepted"))) == 2
    assert asyncio.run(container.workspace.get(str(second.id))).status == "accepted"


def test_apply_group_is_atomic(apply_container: Container, workspace_root) -> None:
    (workspace_root / "a.py").write_text(ORIGINAL, encoding="utf-8")
    (workspace_root / "b.py").write_text(ORIGINAL, encoding="utf-8")
    first, second = _propose_group(apply_container)

    applied = asyncio.run(apply_container.workspace.apply_group(str(first.group_id)))

    assert {c.status for c in applied} == {"applied"}
    assert (workspace_root / "a.py").read_text(encoding="utf-8") == PROPOSED
    assert (workspace_root / "b.py").read_text(encoding="utf-8") == PROPOSED
    assert second.group_id == first.group_id


def test_apply_group_conflict_keeps_workspace_untouched(
    apply_container: Container, workspace_root
) -> None:
    (workspace_root / "a.py").write_text(ORIGINAL, encoding="utf-8")
    (workspace_root / "b.py").write_text(ORIGINAL, encoding="utf-8")
    first, _ = _propose_group(apply_container)
    # 用户在外面改了 b.py → 整组的 hash 复验失败，谁都不能落盘
    (workspace_root / "b.py").write_text("user 手改\n", encoding="utf-8")

    with pytest.raises(ConflictError):
        asyncio.run(apply_container.workspace.apply_group(str(first.group_id)))

    assert (workspace_root / "a.py").read_text(encoding="utf-8") == ORIGINAL
    assert (workspace_root / "b.py").read_text(encoding="utf-8") == "user 手改\n"


def test_group_operations_reject_unknown_group(container: Container) -> None:
    with pytest.raises(NotFoundError):
        asyncio.run(container.workspace.accept_group("0f5b6f4c-0000-0000-0000-000000000000"))


def test_list_filters_by_file_path(container: Container) -> None:
    _propose(container, file_path="a.py")
    _propose(container, file_path="b.py")

    assert len(asyncio.run(container.workspace.list(file_path="a.py"))) == 1
