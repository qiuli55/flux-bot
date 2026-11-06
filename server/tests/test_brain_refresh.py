"""Apply 成功后项目 Brain 自动刷新测试（上下文闭环）。

验证三条边界：只看 applied、同项目节流、失败不阻塞且不把失败记成"刚扫过"。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from flux.core.event.bus import EventBus, Events
from flux.core.project_brain.refresh import BrainRefresher


class _Clock:
    """可控单调时钟。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Brain:
    """记录扫描与写入；`fail` 打开后模拟扫描异常。"""

    def __init__(self) -> None:
        self.scans: list[str] = []
        self.writes: list[tuple[str, object, str, dict]] = []
        self.notes: dict[str, list[dict]] = {}
        self.fail = False

    async def scan(self, project_id, *, record: bool = True):
        if self.fail:
            raise RuntimeError("scan boom")
        self.scans.append(str(project_id))
        return SimpleNamespace(profile=SimpleNamespace(files_scanned=1))

    async def write(self, project_id, *, section, content: str, meta=None):
        key = getattr(section, "value", str(section))
        metadata = dict(meta or {})
        self.writes.append((str(project_id), key, content, metadata))
        self.notes.setdefault(key, []).append({"metadata": metadata, "content": content})

    async def sections(self, project_id):
        return dict(self.notes)


class _Workspace:
    def __init__(self, *, project_id, task_id=None) -> None:
        self._change = SimpleNamespace(project_id=project_id, task_id=task_id)

    async def get(self, change_id: str):
        return self._change


class _Tasks:
    def __init__(self, project_id) -> None:
        self._project_id = project_id

    async def get(self, task_id):
        return SimpleNamespace(project_id=self._project_id)


def _build(*, brain: _Brain, project_id, workspace: _Workspace, clock: _Clock):
    bus = EventBus()
    refresher = BrainRefresher(
        brain=brain,
        tasks=_Tasks(project_id),
        workspace=workspace,
        bus=bus,
        interval_seconds=300,
        clock=clock,
    )
    refresher.attach()
    return bus, refresher


async def test_applied_change_triggers_one_scan_per_project_window() -> None:
    """applied 触发扫描；同项目 300 秒内再 applied 只算一次。"""
    clock, brain, project = _Clock(), _Brain(), uuid.uuid4()
    bus, _ = _build(
        brain=brain, project_id=project, workspace=_Workspace(project_id=project), clock=clock
    )

    await bus.publish(Events.WORKSPACE_CHANGED, {"change_id": "c1", "to": "applied"})
    assert brain.scans == [str(project)]

    clock.now += 10
    await bus.publish(Events.WORKSPACE_CHANGED, {"change_id": "c2", "to": "applied"})
    assert len(brain.scans) == 1

    clock.now += 400
    await bus.publish(Events.WORKSPACE_CHANGED, {"change_id": "c3", "to": "applied"})
    assert len(brain.scans) == 2


async def test_non_applied_change_is_ignored() -> None:
    """只有落盘的提案才刷新；accepted / pending 等状态不触发扫描。"""
    clock, brain, project = _Clock(), _Brain(), uuid.uuid4()
    bus, _ = _build(
        brain=brain, project_id=project, workspace=_Workspace(project_id=project), clock=clock
    )

    await bus.publish(Events.WORKSPACE_CHANGED, {"change_id": "c1", "to": "accepted"})
    await bus.publish(Events.WORKSPACE_CHANGED, {"change_id": "c2", "to": "pending"})
    assert brain.scans == []


async def test_change_without_project_is_skipped() -> None:
    """提案与任务都没有项目归属时跳过：Brain 按项目存，猜一个项目比不刷新更糟。"""
    clock, brain = _Clock(), _Brain()
    bus, refresher = _build(
        brain=brain,
        project_id=None,
        workspace=_Workspace(project_id=None, task_id=uuid.uuid4()),
        clock=clock,
    )

    await refresher.on_change(Events.WORKSPACE_CHANGED, {"change_id": "c1", "to": "applied"})
    assert brain.scans == []
    assert bus.history == []


async def test_scan_failure_is_swallowed_and_retried_on_the_next_change() -> None:
    """扫描失败只记日志：不抛给 Apply，也不占用节流窗口（下一次仍会重试）。"""
    clock, brain, project = _Clock(), _Brain(), uuid.uuid4()
    bus, _ = _build(
        brain=brain, project_id=project, workspace=_Workspace(project_id=project), clock=clock
    )

    brain.fail = True
    await bus.publish(Events.WORKSPACE_CHANGED, {"change_id": "c1", "to": "applied"})
    assert brain.scans == []

    brain.fail = False
    clock.now += 1
    await bus.publish(Events.WORKSPACE_CHANGED, {"change_id": "c2", "to": "applied"})
    assert brain.scans == [str(project)]


def _applied_payload(project_id, *, batch_id: str = "batch-1") -> dict:
    return {
        "batch_id": batch_id,
        "change_ids": ["c1", "c2"],
        "files": ["a.py", "b.py"],
        "task_id": str(uuid.uuid4()),
        "project_id": str(project_id),
        "added_lines": 7,
        "removed_lines": 2,
        "test": {"passed": True, "command": "pytest -q", "exit_code": 0},
    }


async def test_applied_batch_is_sedimented_once_with_real_test_result() -> None:
    """整批落盘写一条沉淀：含文件清单与真实测试结果；同一 batch_id 重投只写一次。"""
    clock, brain, project = _Clock(), _Brain(), uuid.uuid4()
    bus, _ = _build(
        brain=brain, project_id=project, workspace=_Workspace(project_id=project), clock=clock
    )

    payload = _applied_payload(project)
    await bus.publish(Events.WORKSPACE_APPLIED, payload)
    await bus.publish(Events.WORKSPACE_APPLIED, payload)

    assert len(brain.writes) == 1
    written_project, section, text, meta = brain.writes[0]
    assert written_project == str(project)
    assert section == "agent_notes"
    assert "a.py" in text and "b.py" in text
    assert "pytest -q" in text and "通过" in text
    assert meta["batch_id"] == "batch-1"


async def test_applied_batch_without_project_falls_back_to_its_task() -> None:
    """批次事件只带 task_id 时，沿任务找回项目归属，不能因为缺 project_id 就丢沉淀。"""
    clock, brain, project = _Clock(), _Brain(), uuid.uuid4()
    bus, _ = _build(
        brain=brain, project_id=project, workspace=_Workspace(project_id=None), clock=clock
    )

    payload = _applied_payload(project)
    payload["project_id"] = None
    await bus.publish(Events.WORKSPACE_APPLIED, payload)

    assert [item[0] for item in brain.writes] == [str(project)]
