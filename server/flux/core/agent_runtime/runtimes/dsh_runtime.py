"""DshRuntime：把既有 `FluxDshClient` 包进统一契约（P2-1，设计 §6.2）。

行为完全不变——`prepare` 不做任何 DSH 侧原本不做的事（MCP patch、令牌、进程组隔离
仍由 `FluxDshClient` 自己完成），`start` 直接转调 `start_run`，事件流仍由 DSH 客户端
内部发布到事件总线。本类只提供"统一外观"，是 P2-1 迁移零回归的证明点。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from flux.core.agent_runtime.runtimes.base import (
    EVENT_FINAL,
    RUNTIME_DSH,
    ModelChannel,
    RuntimeEvent,
    RuntimeLaunch,
    RuntimeProcess,
)
from flux.core.permissions.modes import normalize_mode


class DshRuntime:
    """内置 DSH Agent 的 Runtime 适配层。"""

    runtime_id = RUNTIME_DSH
    #: DSH 的 stdio SDK 不给 delta，但 runtime 与模型之间是 SSE——由 llm_stream_tap 旁路
    #: 逐 token 取增量，所以这里也是名副其实的原生流式。
    native_streaming = True

    def __init__(self, client: Any, *, workspace: str = "") -> None:
        #: 由容器惰性解析（测试会替换 container.dsh，故不能在此固化）
        self._client = client
        self._workspace = workspace

    async def prepare(
        self,
        *,
        task: Any,
        agent: Any,
        run_id: str,
        instruction: str,
        channel: ModelChannel | None = None,
    ) -> RuntimeLaunch:
        # 每个 Run 用独立 session：DSH 的 session id 全局唯一，沿用同一个 id 起第二轮
        # 会直接报 "session already exists"，同一对话续聊就永远起不来。
        return RuntimeLaunch(
            runtime_id=self.runtime_id,
            run_id=run_id,
            instruction=instruction,
            cwd=self._workspace,
            session_id=f"flux-task-{getattr(task, 'id', run_id)}-{run_id}",
            task_id=str(getattr(task, "id", "")) or None,
            agent_id=getattr(agent, "id_str", None),
            model=str(getattr(task, "model", "") or ""),
            channel=channel,
            permission_mode=normalize_mode(getattr(task, "permission_mode", None)),
        )

    async def start(self, launch: RuntimeLaunch) -> RuntimeProcess:
        run = await self._client.start_run(
            launch.instruction,
            session_id=launch.session_id,
            run_id=launch.run_id,
            task_id=launch.task_id,
            model=launch.model or None,
            channel=launch.channel,
            permission_mode=launch.permission_mode,
        )
        return RuntimeProcess(
            runtime_id=self.runtime_id,
            run_id=launch.run_id,
            pid=getattr(run, "pid", None),
            pgid=getattr(run, "pgid", None),
            handle=run,
            snapshot=run.to_dict(),
        )

    async def events(self, proc: RuntimeProcess) -> AsyncIterator[RuntimeEvent]:
        # DSH 的事件在客户端内部经事件总线发布（行为不变）；此处不重复产流。
        yield RuntimeEvent(EVENT_FINAL, proc.run_id, {"status": "completed"})

    async def cancel(self, proc: RuntimeProcess) -> None:
        await self._client.cancel(proc.run_id)


__all__ = ["DshRuntime"]
