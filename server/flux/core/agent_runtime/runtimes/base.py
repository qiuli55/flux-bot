"""Agent Runtime 统一契约（P2-1，设计 §6）。

`Task → AgentRuntime → {Claude Code, Codex, OpenCode}`：平台只认这一份契约，启动、
身份注入、MCP 接入、事件流、取消各运行时各管各的实现细节，编排层（`TaskRunService`）
不出现任何 runtime 专属分支。

统一事件词表：`status / message / tool_call / tool_result / thinking / final / error`。
终态由既有 `RunSupervisor` 裁决（CLI 退出码 0 → completed、非 0 → failed、
cancel/timeout 由看护器升级链路判定），本契约不新增第二套生命周期。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

#: 支持的 runtime 标识（也是 Agent 档案里 `config["runtime"]` 的合法取值）
RUNTIME_CLAUDE = "claude-code"
RUNTIME_CODEX = "codex"
RUNTIME_OPENCODE = "opencode"
DEFAULT_RUNTIME = RUNTIME_CLAUDE
KNOWN_RUNTIMES: tuple[str, ...] = (RUNTIME_CLAUDE, RUNTIME_CODEX, RUNTIME_OPENCODE)
#: 需要 Flux 主动拉起本机 CLI 的 runtime（安装状态必须是 READY）
CLI_RUNTIMES: tuple[str, ...] = (RUNTIME_CLAUDE, RUNTIME_CODEX, RUNTIME_OPENCODE)

#: 每个 runtime 是否原生逐 token 流式。
#: True = 增量由 runtime 自己产出（claude-code 走 stream_event delta / codex 走
#: app-server delta / opencode 走 serve 的 message.part.delta），平台不做「整段生成再切块回放」。
#: False = 该 runtime 只能整段返回：Agent 仍可正常创建与执行，档案带
#: `native_streaming=False` 供客户端标注，正文由平台按分段回放兜底。
#: 新增 runtime 必须在上面显式登记取值，契约测试会拦住漏登记的 runtime。
RUNTIME_NATIVE_STREAMING: dict[str, bool] = {
    RUNTIME_CLAUDE: True,
    RUNTIME_CODEX: True,
    RUNTIME_OPENCODE: True,
}


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    """一个 runtime 的执行时限与能力开关（新 runtime 必须登记，契约测试会拦住漏登记）。

    默认值面向「提交后一直阻塞到跑完」的通用 runtime；支持异步回合的 runtime
    （提交立即返回 + 事件/轮询收口 + 主动拉终态）把 `async_turn` 置 True，
    客户端据此避免长连接读写超时把整轮产出作废。进程复用同理：只在 `reusable_process`
    为 True 时才进入进程池，编排层不出现任何 runtime 专属分支。
    """

    #: 提交一轮的上限（HTTP 请求 / JSON-RPC 调用）；超时视为提交失败，可重试
    submit_timeout_seconds: float = 30.0
    #: 无任何进展的容忍时长（无输出、无 MCP 活动、状态不变）
    idle_timeout_seconds: float = 600.0
    #: 无论有无进展都不放行的绝对上限
    hard_timeout_seconds: float = 1800.0
    #: 兜底查询 / 轮询的步长
    poll_interval_seconds: float = 2.0
    #: 是否支持「提交立即返回 + 事件收口」的异步回合
    async_turn: bool = False
    #: 进程能否跨 Run 复用（复用粒度见 ProcessPool 的 key）
    reusable_process: bool = False
    #: 上下文预算：续聊会话上一轮的输入 token 超过该值时，由 runtime 自行压缩 / 重开；
    #: 0 = 不限制。这只是能力声明——没实现上下文治理的 runtime 直接忽略，不影响接入；
    #: 声明支持的 runtime 负责在 runtimes 层消费它（编排层不出现 runtime 专属分支）。
    context_compact_tokens: int = 0


#: 未登记 runtime 的兜底时限（未来接入的 runtime 不登记也能跑，只是拿不到专属口径）
DEFAULT_RUNTIME_LIMITS = RuntimeLimits()

#: 各 runtime 的时限默认值；被 Settings 的 `agent_*` 覆盖项进一步收敛
RUNTIME_LIMITS: dict[str, RuntimeLimits] = {
    #: `claude -p` 是一次性进程（靠 `--resume <session_id>` 续聊），不进进程池
    RUNTIME_CLAUDE: RuntimeLimits(submit_timeout_seconds=60.0, async_turn=True),
    RUNTIME_CODEX: RuntimeLimits(submit_timeout_seconds=60.0, reusable_process=True),
    RUNTIME_OPENCODE: RuntimeLimits(
        submit_timeout_seconds=30.0,
        poll_interval_seconds=2.0,
        async_turn=True,
        reusable_process=True,
    ),
}


def runtime_limits(runtime_id: str) -> RuntimeLimits:
    """取 runtime 的时限；未登记的 runtime 用通用默认（不阻断未来接入）。"""
    return RUNTIME_LIMITS.get(runtime_id, DEFAULT_RUNTIME_LIMITS)


#: 统一事件词表
EVENT_STATUS = "status"
EVENT_MESSAGE = "message"
EVENT_TOOL_CALL = "tool_call"
EVENT_TOOL_RESULT = "tool_result"
EVENT_THINKING = "thinking"
EVENT_FINAL = "final"
EVENT_ERROR = "error"
RUNTIME_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EVENT_STATUS,
        EVENT_MESSAGE,
        EVENT_TOOL_CALL,
        EVENT_TOOL_RESULT,
        EVENT_THINKING,
        EVENT_FINAL,
        EVENT_ERROR,
    }
)


@dataclass(slots=True)
class ModelChannel:
    """密钥库解析出的一条模型通道（含明文 key；只在内存里流转，绝不落盘/进日志）。"""

    provider: str
    base_url: str
    api_key: str
    wire_api: str = ""
    #: key 库条目的名称，用于 codex provider 块的展示名
    label: str = ""
    key_id: str = ""

    def __repr__(self) -> str:  # 防止误把密钥打进日志/异常
        return (
            f"ModelChannel(provider={self.provider!r}, base_url={self.base_url!r}, "
            f"key_id={self.key_id!r})"
        )


@dataclass(slots=True)
class RuntimeLaunch:
    """一次 Run 启动所需的全部事实（由 `AgentRuntime.prepare` 组装）。"""

    runtime_id: str
    run_id: str
    instruction: str
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    #: 任务自带的执行目录（tasks.workspace 快照）；None = 使用 runtime 的默认工作区。
    #: 与 cwd 分开：cwd 已包含默认回退，而这里只表示「任务确实有自己的目录」。
    workspace: str | None = None
    session_id: str = ""
    task_id: str | None = None
    agent_id: str | None = None
    #: Agent 档案里登记的模型名（仅用于 Run 用量归因，不参与启动）
    model: str = ""
    #: CLI runtime 专用：run 私有目录（含该 CLI 的隔离配置）
    run_dir: Path | None = None
    #: CLI runtime 专用：完整启动 argv
    argv: tuple[str, ...] = ()
    #: Run 结束即撤销的一次性令牌（明文不落任何日志/证据）
    token_id: str | None = None
    #: 任务级模型通道（来自密钥库）；None = 用 runtime 原生模型配置
    channel: ModelChannel | None = None
    #: 本轮权限模式快照（plan / normal / full）：Run 启动时从任务取值，切换模式下一轮才生效
    permission_mode: str = "normal"
    #: 本 Run 的执行时限（来自 runtime 能力表 + Settings 覆盖）；None = 看护器用全局默认
    limits: RuntimeLimits | None = None


@dataclass(slots=True)
class RuntimeProcess:
    """一个已登记的 Run 进程（pid/pgid 由 runtimes 回填给看护器）。"""

    runtime_id: str
    run_id: str
    pid: int | None = None
    pgid: int | None = None
    #: 运行时自己的句柄（CLI 是 CliProcess）；编排层不解释它
    handle: Any = None
    #: 对外 Run 快照（字段与 Run 快照 dict 对齐，供 API 响应）
    snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RuntimeEvent:
    """统一事件（词表见模块 docstring）。"""

    type: str
    run_id: str
    data: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class AgentRuntime(Protocol):
    """所有 Agent Runtime 的实现契约（设计 §6.2）。"""

    runtime_id: str
    #: 是否原生逐 token 流式（True 必须名副其实：增量由 runtime 直接产出）
    native_streaming: bool

    async def prepare(
        self,
        *,
        task: Any,
        agent: Any,
        run_id: str,
        instruction: str,
        channel: ModelChannel | None = None,
    ) -> RuntimeLaunch:
        """准备一次 Run：指令已由编排层拼好，这里补齐 env / cwd / run 目录 / MCP 注入 / 令牌。

        `channel` 是任务级模型通道（密钥库）；None = 用 runtime 原生模型配置。
        """

    async def start(self, launch: RuntimeLaunch) -> RuntimeProcess:
        """拉起 Run 并返回进程句柄；进程组隔离与看护登记在此完成。"""

    def events(self, proc: RuntimeProcess) -> AsyncIterator[RuntimeEvent]:
        """产出统一事件流（status / message / tool_call / tool_result / final / error）。"""

    async def cancel(self, proc: RuntimeProcess) -> None:
        """取消一次 Run（实际升级链路复用既有 RunSupervisor）。"""


__all__ = [
    "CLI_RUNTIMES",
    "DEFAULT_RUNTIME",
    "DEFAULT_RUNTIME_LIMITS",
    "EVENT_ERROR",
    "EVENT_FINAL",
    "EVENT_MESSAGE",
    "EVENT_STATUS",
    "EVENT_TOOL_CALL",
    "EVENT_TOOL_RESULT",
    "KNOWN_RUNTIMES",
    "ModelChannel",
    "RUNTIME_CLAUDE",
    "RUNTIME_CODEX",
    "RUNTIME_EVENT_TYPES",
    "RUNTIME_LIMITS",
    "RUNTIME_NATIVE_STREAMING",
    "RUNTIME_OPENCODE",
    "AgentRuntime",
    "RuntimeEvent",
    "RuntimeLaunch",
    "RuntimeLimits",
    "RuntimeProcess",
    "runtime_limits",
]
