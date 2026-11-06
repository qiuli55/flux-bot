"""测试替身。"""

from __future__ import annotations

from collections.abc import Callable
from threading import Event
from typing import Any

from deepseek_harness import Notification, RunResult

from flux.connectors.base import Connector, ConnectorManifest
from flux.enums import Capability
from flux.errors import PermissionDeniedError


class FakeTerminalConnector(Connector):
    """模拟一个需要 terminal.execute 能力的终端连接器。

    真实终端连接器是 M4 交付物（主规格 §19.1 Issues #042），测试替身只用于验证
    注册表的能力校验、审计日志与事件发布路径。
    """

    manifest = ConnectorManifest(
        name="terminal",
        version="0.1.0",
        type="terminal",
        actions=("run",),
        required_permissions=(Capability.TERMINAL_EXECUTE,),
    )

    def __init__(self) -> None:
        self.initialized = False
        self.closed = False
        self.commands: list[str] = []

    def initialize(self) -> None:
        self.initialized = True

    def validate_permission(self, capability: Capability) -> None:
        if capability is not Capability.TERMINAL_EXECUTE:
            raise PermissionDeniedError(f"终端连接器不支持能力 {capability}")

    async def execute(self, action: str, parameters: dict[str, Any]) -> dict[str, Any]:
        command = str(parameters.get("command", ""))
        self.commands.append(command)
        return {"stdout": f"ran: {command}", "exit_code": 0}

    def health_check(self) -> bool:
        return self.initialized and not self.closed

    def close(self) -> None:
        self.closed = True


#: FakeDshHarness.run 的行为：收到 (harness, instruction, session_id, on_notification)。
DshBehavior = Callable[
    ["FakeDshHarness", str, "str | None", "Callable[[Notification], None] | None"],
    RunResult,
]


def default_dsh_behavior(
    harness: FakeDshHarness,
    instruction: str,
    session_id: str | None,
    on_notification: Callable[[Notification], None] | None,
) -> RunResult:
    """默认行为：回一条助手文本 + 一次 turn/end，模拟正常完成。"""
    sid = session_id or "session"
    message = {
        "type": "assistant/message",
        "data": {"message": {"content": [{"type": "text", "text": f"echo: {instruction}"}]}},
    }
    end = {"type": "turn/end", "data": {"reason": {"kind": "completed"}}}
    if on_notification is not None:
        for event in (message, end):
            on_notification(
                Notification(method="session.event", payload={"sessionId": sid, "event": event})
            )
    return RunResult(
        session_id=sid,
        final_response=f"echo: {instruction}",
        finish_reason="completed",
        events=[message, end],
        notifications=[],
    )


class FakeDshHarnessClient:
    """FakeDshHarness 的 client 面：只实现 notify（interrupt 走它）。"""

    def __init__(self, harness: FakeDshHarness) -> None:
        self._harness = harness

    def notify(self, method: str, payload: dict[str, Any]) -> None:
        self._harness.notifications.append((method, payload))
        if method == "session/cancel":
            # 让阻塞中的 run() 收到 cancel 后返回，等价于 runtime 会话转 idle。
            self._harness.cancel_event.set()


class FakeDshHarness:
    """DeepSeekHarness 测试替身：不启动真实 runtime 子进程，行为由 behavior 决定。"""

    def __init__(self, behavior: DshBehavior, **kwargs: Any) -> None:
        self.behavior = behavior
        self.kwargs = kwargs
        self.closed = False
        self.started = False
        self.notifications: list[tuple[str, dict[str, Any]]] = []
        #: 每次 run() 收到的输入原文（批次①起用于断言 Bootstrap 注入与原文保真）
        self.prompts: list[str] = []
        #: 每次 run() 收到的 session id：同一任务续跑时必须是不同的值
        self.session_ids: list[str] = []
        self.cancel_event = Event()
        self.client = FakeDshHarnessClient(self)

    def start(self) -> None:
        """真实 harness 的初始化入口（runtime 就绪）；默认成功，需要时可被覆盖。"""
        self.started = True

    def run(
        self,
        input: str,
        *,
        session_id: str | None = None,
        on_notification: Callable[[Notification], None] | None = None,
    ) -> RunResult:
        self.prompts.append(input)
        self.session_ids.append(session_id or "")
        return self.behavior(self, input, session_id, on_notification)

    def close(self) -> None:
        self.closed = True


class FakeDshHarnessFactory:
    """按 behavior 产出 FakeDshHarness 的工厂（DeepSeekHarness 的构造替身）。"""

    def __init__(self, behavior: DshBehavior | None = None) -> None:
        self.behavior = behavior or default_dsh_behavior
        self.created: list[FakeDshHarness] = []

    def __call__(self, **kwargs: Any) -> FakeDshHarness:
        harness = FakeDshHarness(self.behavior, **kwargs)
        self.created.append(harness)
        return harness
