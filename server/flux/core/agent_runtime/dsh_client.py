"""Flux × DSH Agent Runtime 客户端（集成方案 §10 / §18 Phase 1）。

本模块把官方 Python SDK（`deepseek-harness-sdk`）包一层，向 Flux 暴露
"起一次 Run / 收流式事件 / 中断 / 查状态" 四个动作。Phase 1 不引入 Node 侧产物，
也不落库：Run 记录只存进程内字典，M1 里程碑再持久化。

P0-01 起这里还负责**把 Flux 的 MCP 能力面注入 DSH**：起 Run 前确保内置 Agent
有一枚可用令牌，并用 DSH 的 loader patch 机制挂上 `@deepseek-ai/dsh-mcp-client`
插件（streamable-http + Bearer）。Agent 因此只能"看项目、提提案"，
改文件仍然必须经过人审 + Apply Engine——它拿不到任何直接落盘的工具。
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from deepseek_harness import DeepSeekHarness, Notification
from deepseek_harness.client import HarnessClient

from flux.config import Settings
from flux.core.agent_runtime import platforms
from flux.core.agent_runtime.dsh_events import to_flux_event
from flux.core.agent_runtime.llm_stream_tap import (
    DEFAULT_UPSTREAM,
    llm_stream_tap,
    tap_base_url,
)
from flux.core.agent_runtime.model_catalog import dsh_provider_for
from flux.core.agent_runtime.protocol import build_runtime_env, compose_instruction
from flux.core.agent_runtime.run_repository import TERMINAL_RUN_STATUSES, AgentRunRepository, as_utc
from flux.core.agent_runtime.runtimes.base import ModelChannel
from flux.core.agent_runtime.supervisor import RunSupervisor
from flux.core.event.bus import EventBus
from flux.core.mcp.auth import AgentTokenService
from flux.enums import Capability, DshRunStatus
from flux.errors import AuthenticationError, ConfigurationError, NotFoundError
from flux.logging import get_logger

logger = get_logger(__name__)

#: 终止态：进入后不再接受 cancel（含超时/重启接管等 P2-15 新增终态）
TERMINAL_STATUSES = frozenset(DshRunStatus(s) for s in TERMINAL_RUN_STATUSES)


#: P2-15：把 DSH 子进程变成"独立会话 + 独立进程组组长"的启动参数。
#: 具体平台实现收敛在 platforms（POSIX 用 setsid 垫片保证 pid == pgid == sid；
#: Windows 用 CREATE_NEW_PROCESS_GROUP），取消时才能精准清理整棵进程树而不误伤 Flux 自身。
def _supervised_launch_args(base: tuple[str, ...]) -> tuple[str, ...]:
    return platforms.launch_argv(base)


class _SupervisedHarnessClient(HarnessClient):
    """HarnessClient 子类：仅覆写启动参数，让 runtime 子进程独占一个进程组。"""

    def _default_launch_args(self, env: dict[str, str]) -> tuple[str, ...]:
        return _supervised_launch_args(super()._default_launch_args(env))


class _SupervisedDeepSeekHarness(DeepSeekHarness):
    """DeepSeekHarness 子类：把内部 client 换成进程组受控的那一个。

    换掉的旧 client 从未 start（无子进程、无读线程），不产生泄漏。
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client = _SupervisedHarnessClient(
            self._client.config, _launch_args=self._client._launch_args
        )


#: 内置 Agent 令牌的能力：读项目 + 提提案 + 经 Flux 终端执行命令（Agent Terminal Console §4）。
#: 命令不是"任意 shell"：它必须走 Flux 的 Terminal Session，命令与输出落成终端事件供人
#: 实时观察并 Stop，输出只回摘要。apply / git / secret 仍不在 MCP 面上。
MCP_TOKEN_SCOPES = (Capability.FILE_READ, Capability.FILE_WRITE, Capability.TERMINAL_EXECUTE)

#: DSH 运行库 finish_reason → Run 终态（P1：Agent 失败不能显示为成功）。
#: `harness.run()` 正常返回只代表"调用结束"，失败结论在 result.finish_reason 里；
#: 只映射明确的失败/中断类取值，completed / stop / None 等其余取值仍按成功处理——
#: 运行库将来新增成功态取值时不会被误判。
_FINISH_REASON_STATUS = {
    "error": DshRunStatus.FAILED,
    "failed": DshRunStatus.FAILED,
    "cancelled": DshRunStatus.CANCELLED,
    "timeout": DshRunStatus.TIMEOUT,
    "interrupted": DshRunStatus.INTERRUPTED,
    "aborted": DshRunStatus.INTERRUPTED,
}

#: 失败类 finish_reason 的中文原因（不对用户透运行库英文原文）
_FINISH_REASON_ERROR = {
    "error": "Agent 执行出错（运行库 finish_reason=error），未产出可用结果",
    "failed": "Agent 执行失败（运行库 finish_reason=failed）",
    "cancelled": "Agent 运行被取消（运行库 finish_reason=cancelled）",
    "timeout": "Agent 运行超时（运行库 finish_reason=timeout）",
    "interrupted": "Agent 运行被中断（运行库 finish_reason=interrupted）",
    "aborted": "Agent 运行被中止（运行库 finish_reason=aborted）",
}

#: 上游「额度/鉴权」类错误的特征词：命中就换成可操作的说法（与 CLI runtime 同一套口径）
_QUOTA_HINTS: tuple[str, ...] = (
    "insufficient",
    "quota",
    "balance",
    "billing",
    "subscription",
    "unauthorized",
    "invalid api key",
    "authentication",
    "余额",
    "额度",
)


def _failure_error(kind: str, detail: str | None) -> str:
    """失败原因：带上上游原文；额度/鉴权类换成用户能照着处理的说法。

    没有原文时保持原来的通用说法（运行库只给 finish_reason 的情况），
    不臆造原因，也不把 SDK 的英文异常直接甩给用户。
    """
    base = _FINISH_REASON_ERROR[kind]
    if not detail:
        return base
    if any(hint in detail.lower() for hint in _QUOTA_HINTS):
        return f"模型额度或鉴权异常，本轮已立即终止：{detail}。请切换其他模型或补充额度后重新发送。"
    return f"{base}，上游原因：{detail}"


#: DSH MCP 客户端插件（runtime 捆绑的 cordis 插件名）
MCP_PLUGIN_NAME = "@deepseek-ai/dsh-mcp-client"
#: patch 文件落点（dsh_home 下，仓库外）
MCP_PATCH_DIRNAME = "patches"
MCP_PATCH_FILENAME = "flux-mcp.patch.yml"
#: serverName 必须是合法标识符（插件侧正则 ^[A-Za-z0-9_-]{1,32}$）
_SERVER_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,32}")
#: start 期间轮询"子进程是否已出现"的步长（秒）：Popen 很快，initialize 才可能长阻塞
_PROC_REGISTER_POLL = 0.05


@dataclass
class DshRun:
    """一次 DSH Agent Run 的对外快照。

    权威状态由 RunSupervisor 写进 agent_runs 表（P2-15）；这个对象是 API 响应形态，
    并把进程树与心跳信息一并暴露出来（取消后能看出"进程是否真的没了"）。
    """

    session_id: str
    instruction: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: DshRunStatus = DshRunStatus.PENDING
    final_response: str = ""
    finish_reason: str | None = None
    error: str | None = None
    #: 上游失败原文（turn/end 的 reason.error/failure.message），用于把原因讲清楚
    upstream_error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    cancel_requested: bool = False
    pid: int | None = None
    pgid: int | None = None
    timeout_kind: str | None = None
    agent_id: str | None = None
    #: 本次 Run 归属的任务（用于注入 FLUX_TASK_ID；独立起 Run 时为 None）
    task_id: str | None = None
    #: 任务级模型覆盖（移动端切换模型）；None = 用服务端配置的默认模型
    model: str | None = None
    #: 任务级模型通道（密钥库）；含明文 key，repr 已脱敏，不进任何对外快照
    channel: ModelChannel | None = None
    #: 本轮权限模式快照（plan / normal / full），MCP 层据此做白名单与审批闸门
    permission_mode: str = "normal"
    last_state_change_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "instruction": self.instruction,
            "status": str(self.status),
            "final_response": self.final_response,
            "finish_reason": self.finish_reason,
            "error": self.error,
            "events": list(self.events),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_seconds": self.duration_seconds,
            "cancel_requested": self.cancel_requested,
            "pid": self.pid,
            "pgid": self.pgid,
            "timeout_kind": self.timeout_kind,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "permission_mode": self.permission_mode,
            "model": self.model,
            "model_key_id": self.channel.key_id if self.channel is not None else None,
            "last_state_change_at": (
                self.last_state_change_at.isoformat() if self.last_state_change_at else None
            ),
        }


class FluxDshClient:
    """DSH Agent Runtime 的客户端（集成方案 §10；P2-15 起生命周期交给 RunSupervisor）。

    Run 权威状态在 agent_runs 表（RunSupervisor 写），本类只负责"起一次 Run / 收事件流"。
    `harness_factory` 默认是 SDK 的 `DeepSeekHarness`（进程组受控子类）；测试注入替身即可
    绕开真实 runtime。`token_service` 用于给内置 Agent 签发 MCP 接入令牌（P0-01）；未注入
    且 MCP 注入已启用时，起 Run 会显式报错，而不是让 Agent 在"没有平台能力"的状态下空跑。
    `supervisor` 由容器装配（带数据库仓储）；未注入时退化成纯内存看护，单测可直接使用。
    """

    def __init__(
        self,
        settings: Settings,
        bus: EventBus | None = None,
        harness_factory: Callable[..., DeepSeekHarness] | None = None,
        token_service: AgentTokenService | None = None,
        supervisor: RunSupervisor | None = None,
        run_repository: AgentRunRepository | None = None,
    ) -> None:
        self._settings = settings
        self._bus = bus
        self._harness_factory = harness_factory or _SupervisedDeepSeekHarness
        self._token_service = token_service
        self._runs: dict[str, DshRun] = {}
        self._harnesses: dict[str, DeepSeekHarness] = {}
        self._supervisor = supervisor or RunSupervisor(
            settings=settings, repository=run_repository, bus=bus
        )
        #: 进程内缓存的内置 Agent 令牌明文（明文只在签发时出现一次）
        self._mcp_token: str | None = None
        #: 按 canonical agent UUID 缓存令牌明文（「单独跟某一个 agent 干活」用）
        self._agent_tokens: dict[str, str] = {}
        #: 内置 Agent 的 canonical UUID（P3-16：令牌与 Run 归属都用它）
        self._mcp_agent_id: str | None = None

    @property
    def supervisor(self) -> RunSupervisor:
        return self._supervisor

    # --- 就绪 ---

    def ensure_ready(self) -> None:
        """确认能力已启用，并确保 DSH_HOME 与工作区目录存在。"""
        if not self._settings.dsh_enabled:
            raise ConfigurationError("DSH Agent Runtime 未启用（FLUX_DSH_ENABLED=false）")
        for path in (self._settings.dsh_home, self._settings.dsh_workspace):
            Path(path).mkdir(parents=True, exist_ok=True)

    # --- MCP 注入（P0-01）---

    async def prepare_mcp_patch(
        self, agent_id: str | None = None, *, run_id: str | None = None
    ) -> Path | None:
        """确保目标 Agent 有一条可用的 MCP 通道，返回 DSH loader patch 文件路径。

        `agent_id` 留空 = 平台配置的内置 Agent（patch 落在固定文件名，行为与既有链路一致）；
        指定时按该 Agent 的 canonical UUID 签发令牌，并写到**该 Agent 专属**的 patch 文件名——
        否则不同 Agent 的 Run 并发起跑会互相覆盖同一个 patch 文件。

        `run_id` 非空时，会在 patch 的 HTTP headers 里追加 `X-Flux-Run-Id`，并把它并进 patch
        文件名：MCP 端点据此把头绑到本次 Run，Agent 经 `terminal.execute` 派生的终端会话也随之
        绑到 Run，Run 终态时回收。文件名带 run_id 是为了**同一 Agent 的并发 Run 各用各的 patch**，
        不互相覆盖对方的 run_id 归属（否则会把 A 的会话错记到 B 名下）。

        未启用 MCP 注入时返回 None（DSH 退化成裸 agent，仅用于排查）。
        patch 里含 Bearer 令牌明文，因此写在 dsh_home（仓库外）并收紧到 0600——
        DSH 只能从配置文件读 header，这是它拿到令牌的唯一途径。
        """
        if not self._settings.dsh_mcp_enabled:
            logger.warning("DSH MCP 注入已关闭：本次 Run 的 Agent 将看不到项目、也提不了提案")
            return None
        if self._token_service is None:
            raise ConfigurationError(
                "已启用 DSH MCP 注入，但未装配 AgentTokenService",
                details={"hint": "由容器把 container.agent_tokens 传给 FluxDshClient"},
            )
        server_name = self._settings.dsh_mcp_server_name.strip()
        if not _SERVER_NAME_PATTERN.fullmatch(server_name):
            raise ConfigurationError(
                f"MCP serverName 非法（须匹配 ^[A-Za-z0-9_-]{{1,32}}$）：{server_name!r}",
                details={"server_name": server_name},
            )
        url = self._settings.dsh_mcp_url.strip()
        if not url.startswith(("http://", "https://")):
            raise ConfigurationError(f"MCP 端点必须是 http(s) URL：{url!r}", details={"url": url})

        if agent_id is None:
            token = await self._ensure_mcp_token()
            stem = "flux-mcp"
        else:
            token = await self._ensure_token_for_agent(agent_id)
            stem = f"flux-mcp-{agent_id}"
        # 文件名按 (agent, run_id) 唯一：同一 Agent 的并发 Run 各用各的 patch，
        # 不互相覆盖对方的 X-Flux-Run-Id 归属。run_id 为空时退回既有的按 Agent 命名。
        patch_name = MCP_PATCH_FILENAME if run_id is None else f"{stem}-{run_id}.patch.yml"
        headers = {"Authorization": f"Bearer {token}"}
        if run_id is not None:
            # MCP 端点据此把本次调用绑到该 Run，终端会话随之绑 Run、Run 终态时回收。
            headers["X-Flux-Run-Id"] = run_id
        entries = [
            {
                "id": f"flux-mcp-{server_name}",
                "name": MCP_PLUGIN_NAME,
                "config": {
                    "transport": "streamable-http",
                    "serverName": server_name,
                    "url": url,
                    "headers": headers,
                    "toolCallTimeoutMs": self._settings.dsh_mcp_tool_timeout_ms,
                    # Flux 不可达时宁可起不来：起得来的裸 agent 会假装自己什么都能干
                    "failOnStartupError": True,
                },
            }
        ]
        path = (Path(self._settings.dsh_home) / MCP_PATCH_DIRNAME / patch_name).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._render_patch(entries), encoding="utf-8")
        # 平台原语：POSIX chmod 0600 / Windows icacls 收权；失败如实记 warning，不静默降级
        if not platforms.secure_file(path):
            logger.warning("dsh.mcp patch 文件权限弱化（未能收紧到仅属主可访问）path=%s", path)
        logger.info(
            "dsh.mcp patch 已就绪 path=%s server=%s agent=%s",
            path,
            server_name,
            agent_id or self._settings.dsh_mcp_agent_id,
        )
        return path

    async def _ensure_mcp_token(self) -> str:
        """返回一枚可用的内置 Agent 令牌明文；该 Agent 名下旧令牌一律撤销后重签。

        明文只在 `issue` 时出现一次，进程重启后无法从库里取回——这正是设计：
        与其在库里存一份可还原的密文，不如重签一枚，并把旧的撤销掉。

        P3-16：令牌的 `agent_id` 必须是 Agent Registry 的 canonical UUID。配置里的
        `dsh_mcp_agent_id`（如 `dsh-builtin`）只是 display_name，先按名字解析档案、
        没有就补建，再拿 UUID 去签令牌——身份只有一套。
        """
        assert self._token_service is not None  # 调用方已确保
        if self._mcp_token and self._mcp_agent_id:
            try:
                identity = await self._token_service.authenticate(self._mcp_token)
            except AuthenticationError:
                logger.warning("缓存的 MCP 令牌已失效（被撤销？），将重签一枚")
                self._mcp_token = None
            else:
                self._mcp_agent_id = identity.agent_id
                return self._mcp_token

        handle = await self._token_service.ensure_agent(
            name=self._settings.dsh_mcp_agent_id, permissions=MCP_TOKEN_SCOPES
        )
        agent_id = handle.id_str  # type: ignore[attr-defined]
        for token in await self._token_service.list(agent_id=agent_id):
            if token.revoked_at is None:
                await self._token_service.revoke(token.id)
        _, raw = await self._token_service.issue(
            agent_id=agent_id,
            scopes=MCP_TOKEN_SCOPES,
            label="DSH 内置 Agent（Flux 自动签发）",
        )
        self._mcp_token = raw
        self._mcp_agent_id = agent_id
        return raw

    async def _ensure_token_for_agent(self, agent_id: str) -> str:
        """按 canonical agent UUID 返回一枚可用的 MCP 令牌明文。

        令牌的 agent_id 决定这次 Run 里 Flux MCP 调用算谁的身份，所以必须是 Agent
        Registry 的 canonical UUID——传 display_name 会签出身份错乱的令牌，宁可直接报错。
        与内置 Agent 同理：明文只在 issue 时出现一次，进程重启取不回，所以按 agent 缓存、
        失效则重签，并在重签前撤销该 agent 名下的旧令牌。
        """
        assert self._token_service is not None  # 调用方已确保
        try:
            uuid.UUID(agent_id)
        except (ValueError, AttributeError, TypeError) as exc:
            raise ConfigurationError(
                f"agent_id 必须是 Agent Registry 的 canonical UUID：{agent_id!r}",
                details={"agent_id": agent_id},
            ) from exc
        cached = self._agent_tokens.get(agent_id)
        if cached:
            try:
                await self._token_service.authenticate(cached)
            except AuthenticationError:
                logger.warning("缓存的 MCP 令牌已失效（被撤销？），将重签一枚 agent=%s", agent_id)
                self._agent_tokens.pop(agent_id, None)
            else:
                return cached

        for token in await self._token_service.list(agent_id=agent_id):
            if token.revoked_at is None:
                await self._token_service.revoke(token.id)
        _, raw = await self._token_service.issue(
            agent_id=agent_id,
            scopes=MCP_TOKEN_SCOPES,
            label=f"DSH Agent {agent_id[:8]}（Flux 自动签发）",
        )
        self._agent_tokens[agent_id] = raw
        return raw

    @staticmethod
    def _render_patch(entries: list[dict[str, Any]]) -> str:
        """渲染 loader patch：**必须包在 `insert` 里**。

        DSH 的 patch 数组有两种条目语义：带 `id` 的是"覆盖已存在条目"，带 `insert` 的才是
        "新增条目"。直接把 `{id, name, config}` 放进顶层数组会被当成前者，DSH 只打印一句
        `patch: entry "flux-mcp-flux" not found` 就跳过——Run 照常跑完，但 Agent 的工具集里
        没有任何 Flux MCP 工具（P0-01 静默失效）。
        """
        header = (
            "# Flux 自动生成：把平台 MCP 能力面注入 DSH runtime。\n"
            "# 文件含 Agent 接入令牌（权限 0600），勿提交版本库；Flux 起 Run 时会按需覆盖。\n"
            "# 结构：insert 列表——顶层数组的 {id: ...} 语义是覆盖已有条目，新插件必须 insert。\n"
        )
        return header + yaml.safe_dump([{"insert": entries}], allow_unicode=True, sort_keys=False)

    # --- 起 Run ---

    async def start_run(
        self,
        instruction: str,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        task_id: object | None = None,
        agent_id: str | None = None,
        model: str | None = None,
        channel: ModelChannel | None = None,
        permission_mode: str = "normal",
    ) -> DshRun:
        """登记一条 Run（PENDING，落库）并把实际执行交给后台任务。

        权威状态由 RunSupervisor 写进 agent_runs 表；本方法只负责"建记录 + 起协程"。
        MCP patch 在起 Run 前准备好：拿不到令牌 / 写不出 patch 就直接报错，
        不把"Agent 摸不到项目"这种残废状态放进运行队列。

        `run_id` 可由调用方预先指定：任务链路要先把它登记到任务上再起 Run，
        否则 Run 结束时事件可能先到、任务还查不到自己的 Run（P0-05 的竞态）。

        `agent_id` 指定则由该 Agent 执行这次 Run（canonical UUID）；留空走内置 Agent，
        与既有链路完全一致。
        """
        self.ensure_ready()
        # run_id 必须在准备 MCP patch 之前定下来：patch 的 X-Flux-Run-Id 头要写上它。
        run_id = run_id or uuid.uuid4().hex
        patch_path = await self.prepare_mcp_patch(agent_id, run_id=run_id)
        run = DshRun(
            run_id=run_id,
            session_id=session_id or f"flux-{run_id}",
            instruction=instruction,
            status=DshRunStatus.PENDING,
            started_at=datetime.now(timezone.utc),
            agent_id=agent_id or self._mcp_agent_id,
            task_id=str(task_id) if task_id is not None else None,
            model=(model or "").strip() or None,
            channel=channel,
            permission_mode=permission_mode,
        )
        self._runs[run.run_id] = run
        await self._supervisor.create_run(
            run_id=run.run_id,
            session_id=run.session_id,
            instruction=instruction,
            agent_id=run.agent_id,
            task_id=task_id,
            permission_mode=permission_mode,
        )
        asyncio.create_task(self._execute(run, patch_path))
        return run

    async def _execute(self, run: DshRun, patch_path: Path | None = None) -> None:
        """在线程里跑一次 DSH 会话，并把通知转成 Flux 事件。

        SDK 是同步阻塞的（§10.3），必须用 `asyncio.to_thread` 卸载；每次 Run **新建一个**
        harness，因为一个 SDK 实例独占一个 runtime 子进程且只能串行使用，不能共享单实例并发 run。

        生命周期动作全部经 RunSupervisor（P2-15）：STARTING → RUNNING（记 pid/pgid）→ 终态；
        取消/超时若已先落定终态，这里的结果不再覆盖（is_settled）。
        """
        loop = asyncio.get_running_loop()
        run_id = run.run_id
        # 旁路 token：token 级增量由本进程的 SSE 代理直接发总线，Run 结束注销
        tap_token: str | None = None

        def _on_notification(notification: Notification) -> None:
            # 回调由 SDK 读线程同步调用，不能直接 await；把动作交回事件循环线程。
            event, body = to_flux_event(notification)
            record = {**body, "run_id": run_id}
            run.events.append(record)
            detail = body.get("error_detail")
            if isinstance(detail, str) and detail:
                # 失败原因随 turn/end 一起到达，先存下来给终态消息用（见 _failure_error）
                run.upstream_error = detail
            # 旁路已经逐 token 推过正文时，SDK 收尾那条整段 assistant/message 只留档不外发，
            # 否则手机端会把同一段答复再整段贴一遍。
            duplicate = (
                tap_token is not None
                and body.get("event_type") == "assistant/message"
                and llm_stream_tap.has_streamed(tap_token)
            )
            if self._bus is not None and not duplicate:
                asyncio.run_coroutine_threadsafe(self._bus.publish(event, record), loop)
            # 收到事件即算一次有效进展（P2-15 §2.4：不能只看 stdout 有没有字节）
            asyncio.run_coroutine_threadsafe(
                self._supervisor.note_output(run_id, _progress_snapshot(record)), loop
            )

        status = DshRunStatus.COMPLETED
        error: str | None = None
        timeout_kind: str | None = None
        harness: DeepSeekHarness | None = None
        await self._supervisor.mark_starting(run_id)
        provider = self._settings.dsh_provider
        base_url: str | None = None
        api_key: str | None = None
        if run.channel is not None:
            provider = dsh_provider_for(run.channel.provider) or provider
            base_url = run.channel.base_url or None
            api_key = run.channel.api_key or None
        # token 级流式：把 DSH 的模型 base_url 临时指到本进程的 SSE 旁路。
        # 上游就是本来要用的模型 API；旁路只转发 + 解析增量，不改请求/响应内容。
        if self._bus is not None:
            upstream = (
                base_url
                or os.environ.get("DEEPSEEK_BASE_URL")
                or self._settings.deepseek_base_url
                or DEFAULT_UPSTREAM
            )
            tap_token = llm_stream_tap.register(run_id=run_id, upstream=upstream, bus=self._bus)
            base_url = f"{tap_base_url(self._settings.dsh_mcp_url).rstrip('/')}/{tap_token}"
        try:
            harness = self._harness_factory(
                dsh_home=self._settings.dsh_home,
                cwd=self._settings.dsh_workspace,
                provider=provider,
                model=run.model or self._settings.dsh_model,
                max_tokens=self._settings.dsh_max_tokens,
                patches=(str(patch_path),) if patch_path is not None else (),
                initialize_timeout_seconds=self._settings.dsh_init_timeout_seconds,
                request_timeout_seconds=self._settings.dsh_run_timeout_seconds or None,
                env=self._agent_env(run),
                base_url=base_url,
                api_key=api_key,
            )
            self._harnesses[run.run_id] = harness
            if not await self._mark_process_started(run_id, harness):
                # 取消/超时抢先把 Run 落成了终态：不要再跑，直接收摊
                return
            try:
                # 初始化与执行分开调：这一步超时说明 Agent 根本没启动起来，
                # 归成启动超时（中文原因），而不是把 SDK 的英文异常原文透给用户。
                # start 内先 spawn 再 initialize；spawn 一出现就补登 pid/pgid，
                # 这样 initialize 卡死（provider 无响应）时超时/取消也能按 pgid 清理进程树。
                start_task = asyncio.create_task(asyncio.to_thread(harness.start))
                await self._register_process_group_later(run_id, harness, start_task)
                await start_task
            except TimeoutError as exc:
                logger.warning("dsh.run 启动初始化超时 run=%s error=%s", run.run_id, exc)
                status = DshRunStatus.TIMEOUT
                timeout_kind = "startup"
                error = (
                    "启动超时（startup）：DSH 运行时在 "
                    f"{self._settings.dsh_init_timeout_seconds:g} 秒内未完成初始化"
                )
            else:
                # start 返回后再核对一遍（幂等）：轮询没赶上或进程信息变更都以这里为准；
                # 返回 False 表示取消/超时抢先把 Run 落成终态：不要再跑指令，finally 会清理进程。
                if not await self._register_process_group(run_id, harness):
                    return
                # 单一收口点（批次①实现点 1）：任务路径（tasks.py）与 DSH API 路径（dsh.py）
                # 都从这里进 harness——统一在交给 harness 前拼上 Runtime Bootstrap，作为 Agent
                # 的第一份强制上下文；run.instruction 存库 / API / UI 仍保持用户原文。
                result = await asyncio.to_thread(
                    harness.run,
                    compose_instruction(run.instruction, run.permission_mode),
                    session_id=run.session_id,
                    on_notification=_on_notification,
                )
                run.final_response = result.final_response
                run.finish_reason = result.finish_reason
                # P1：run() 正常返回 ≠ 成功——运行库以失败类 finish_reason 收尾时
                # 必须落非成功终态，否则任务在 UI 上显示"已完成"，用户以为改动已产出。
                kind = (result.finish_reason or "").strip().lower()
                mapped = _FINISH_REASON_STATUS.get(kind)
                if mapped is not None:
                    logger.warning("dsh.run 非成功结束 run=%s finish_reason=%s", run.run_id, kind)
                    status = mapped
                    error = _failure_error(kind, run.upstream_error)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 上游任何异常都归到失败终态
            logger.exception("dsh.run 执行失败 run=%s", run.run_id)
            status = DshRunStatus.FAILED
            error = str(exc)
        finally:
            # 旁路只在本 Run 内有效：无论成败都注销，避免 token 泄漏成长期入口
            llm_stream_tap.unregister(tap_token)
            # 构造也可能失败（无合适 runtime 载体等），关闭同上：单点失败不得吞掉终态写入。
            if harness is not None:
                try:
                    harness.close()
                except Exception:  # noqa: BLE001 - 关闭失败不能改变本次 Run 的结果
                    logger.exception("dsh.harness 关闭失败 run=%s", run.run_id)
            self._harnesses.pop(run.run_id, None)

        final = await self._supervisor.finish(
            run_id,
            status,
            final_response=run.final_response,
            finish_reason=run.finish_reason,
            error=error,
            timeout_kind=timeout_kind,
        )
        if final is not status:
            # 看护器改写了终态（取消语义覆盖迟到的异常/结果）：本线程算出的失败原因
            # 不该串进"已取消"的结果里（502r 曾出现 cancelled 却带 stdout closed 报错）。
            error = None
            timeout_kind = None
            run.finish_reason = None
        run.error = error
        run.timeout_kind = timeout_kind
        self._sync_snapshot(run, final)
        self._supervisor.release(run_id)

    async def _mark_process_started(self, run_id: str, harness: DeepSeekHarness) -> bool:
        """起进程前的登记：进入 RUNNING 并注册优雅中断；返回 False 表示已被取消/超时落定。

        此刻子进程尚未创建（SDK 在 `harness.start()` 里才 Popen），pid/pgid 必然拿不到，
        真实进程信息由 `_register_process_group` 在 start 成功后补登。
        """
        if self._supervisor.is_settled(run_id):
            return False
        await self._supervisor.mark_running(run_id, pid=None, pgid=None)
        self._supervisor.register_interrupt(
            run_id,
            lambda: _notify_cancel(harness, session_id=self._runs[run_id].session_id),
        )
        return not self._supervisor.is_settled(run_id)

    async def _register_process_group_later(
        self, run_id: str, harness: DeepSeekHarness, start_task: asyncio.Task[None]
    ) -> None:
        """start 在线程里跑期间轮询：Popen 一出现就把 pid/pgid 补登给看护器。

        `client.start()` 同步 Popen 后立刻做 initialize，后者可能长时间阻塞
        （provider 无字节返回等）。如果等到 start 返回才登记，超时落库时 pid/pgid 仍是 null，
        看护器判"无进程可清理"，残留进程只能等 SDK 自己超时（D-01 的 504 现场）。
        """
        while not start_task.done():
            if self._supervisor.is_settled(run_id):
                return
            pid, pgid = _process_group_of(harness)
            if pid is not None:
                await self._supervisor.mark_running(run_id, pid=pid, pgid=pgid)
                return
            await asyncio.sleep(_PROC_REGISTER_POLL)

    async def _register_process_group(self, run_id: str, harness: DeepSeekHarness) -> bool:
        """`harness.start()` 成功后补登真实 pid/pgid，取消/超时才能按 pgid 清理整棵进程树。

        D-01 根因：原实现在 start 之前登记，`_proc` 尚为 None，agent_runs 里 pid/pgid
        永远为 null，killpg 不可用、进程树清理退化成等它自退。setsid 垫片保证
        pid == pgid == sid；测试替身没有 `_proc`，按"无进程"继续（看护器退化为状态机）。
        返回 False 表示 Run 已被抢落终态，不应再执行指令。
        """
        if self._supervisor.is_settled(run_id):
            return False
        pid, pgid = _process_group_of(harness)
        if pid is None:
            logger.warning("dsh.run 未取到子进程 pid（按无进程看护）run=%s", run_id)
            return True
        await self._supervisor.mark_running(run_id, pid=pid, pgid=pgid)
        return not self._supervisor.is_settled(run_id)

    def _agent_env(self, run: DshRun) -> dict[str, str]:
        """启动时注入机器可读的 Runtime Identity（最终方案 §5）。

        真源在 `agent_runtime.protocol.build_runtime_env`，与 CLI Adapter 共用同一份，
        避免两套 `FLUX_*` 注入各写各的（收口方案 §8.2）。
        """
        return build_runtime_env(
            run_id=run.run_id,
            workspace=self._settings.workspace_root or self._settings.dsh_workspace,
            task_id=run.task_id,
            agent_id=run.agent_id,
            mcp_endpoint=self._settings.dsh_mcp_url if self._settings.dsh_mcp_enabled else None,
            permission_mode=run.permission_mode,
        )

    async def cancel(self, run_id: str) -> DshRun:
        """取消一次 Run：CANCELLING → 优雅通知 → 清理整个进程树 → CANCELLED。

        进程树确认清理干净才算 CANCELLED；清理不掉落 FAILED 并在 error 里说明
        （P2-15 §2.5：绝不把残留进程伪装成取消成功）。
        """
        run = self._runs.get(run_id)
        status = await self._supervisor.cancel(run_id)
        if run is None:
            return await self._snapshot_from_repo(run_id, status)
        self._sync_snapshot(run, status)
        await self._refresh_from_row(run)
        return run

    # --- 查询 ---

    async def get_run_async(self, run_id: str) -> DshRun:
        """查一次 Run：内存快照优先，没有就回落到库（Flux 重启后遗留的 Run 仍可查）。"""
        run = self._runs.get(run_id)
        if run is None:
            run = await self._snapshot_from_repo(run_id, None)
        else:
            await self._refresh_from_row(run)
        return run

    def get_run(self, run_id: str) -> DshRun:
        run = self._runs.get(run_id)
        if run is None:
            raise NotFoundError(f"DSH Run {run_id} 不存在", details={"run_id": run_id})
        return run

    def list_runs(self) -> list[DshRun]:
        return list(self._runs.values())

    def status(self) -> dict[str, Any]:
        """对外暴露的 DSH 配置快照（不含任何密钥）。"""
        return {
            "enabled": self._settings.dsh_enabled,
            "home": self._settings.dsh_home,
            "workspace": self._settings.dsh_workspace,
            "provider": self._settings.dsh_provider,
            "model": self._settings.dsh_model,
            # MCP 注入状态（P0-01）：agent 能否通过平台拿项目上下文、提提案就看这里
            "mcp": {
                "enabled": self._settings.dsh_mcp_enabled,
                "url": self._settings.dsh_mcp_url,
                "server_name": self._settings.dsh_mcp_server_name,
                "agent_name": self._settings.dsh_mcp_agent_id,
                "agent_id": self._mcp_agent_id,
            },
        }

    def close(self) -> None:
        """尽力关闭所有活跃 harness；单个失败只记日志，不影响其它实例。"""
        for run_id, harness in list(self._harnesses.items()):
            try:
                harness.close()
            except Exception:  # noqa: BLE001 - 关闭失败不能阻塞应用退出
                logger.exception("dsh.harness 关闭失败 run=%s", run_id)
            self._harnesses.pop(run_id, None)

    async def shutdown(self) -> None:
        """应用退出：停掉看护循环、清理仍在跑的进程树、关闭 harness（P2-15 §2.5）。"""
        await self._supervisor.stop_background()
        await self._supervisor.kill_active_processes()
        self.close()

    # --- 内部 ---

    @staticmethod
    def _sync_snapshot(run: DshRun, status: DshRunStatus) -> None:
        """把看护器落定的终态同步进内存快照。"""
        run.status = status
        run.cancel_requested = run.cancel_requested or status is DshRunStatus.CANCELLED
        if status in TERMINAL_STATUSES:
            run.finished_at = run.finished_at or datetime.now(timezone.utc)
            if run.started_at is not None:
                run.duration_seconds = (run.finished_at - run.started_at).total_seconds()

    async def _refresh_from_row(self, run: DshRun) -> None:
        """用库里的权威状态刷新内存快照（错误信息、超时类型、进程信息都以库为准）。"""
        row = await self._supervisor.repository.get(run.run_id)
        if row is None:
            return
        try:
            row_status = DshRunStatus(row.status)
            if run.status in TERMINAL_STATUSES and row_status not in TERMINAL_STATUSES:
                # 本进程看护器已落定终态、库里这一行只是还没写完（取消与异常收尾并发的
                # 窗口里，胜者要先确认进程树清理才落库）。不能拿慢一拍的旧状态覆盖，
                # 否则取消接口会把刚取消的 Run 读成 cancelling，任务被误判成失败（502r）。
                logger.info(
                    "agent_runs 行 %s 尚在 %s，内存快照已落定 %s：以看护器为准",
                    run.run_id,
                    row_status,
                    run.status,
                )
            else:
                run.status = row_status
        except ValueError:
            logger.warning("agent_runs 行 %s 的状态无法识别：%s", run.run_id, row.status)
        run.error = row.error
        run.finish_reason = row.finish_reason or run.finish_reason
        run.timeout_kind = row.timeout_kind
        run.cancel_requested = bool(row.cancel_requested)
        run.pid = row.pid
        run.pgid = row.pgid
        run.agent_id = row.agent_id or run.agent_id
        run.task_id = str(row.task_id) if row.task_id else run.task_id
        run.last_state_change_at = row.last_state_change_at
        if row.finished_at is not None:
            # SQLite 不存时区：库里读出来的时间是 naive，直接与 aware 的 started_at
            # 相减会抛 TypeError（取消接口 500 的根因，实测复现）。归一到 aware UTC 再算。
            run.finished_at = as_utc(row.finished_at)
            if run.started_at is not None:
                run.duration_seconds = (run.finished_at - run.started_at).total_seconds()

    async def _snapshot_from_repo(self, run_id: str, status: DshRunStatus | None) -> DshRun:
        """从库里重建一条 Run 快照（Flux 重启后内存里没有、库里还在的场景）。"""
        row = await self._supervisor.repository.get(run_id)
        if row is None:
            raise NotFoundError(f"DSH Run {run_id} 不存在", details={"run_id": run_id})
        run = DshRun(
            run_id=row.id,
            session_id=row.session_id,
            instruction=row.instruction,
            status=DshRunStatus(row.status),
            agent_id=row.agent_id,
            task_id=str(row.task_id) if row.task_id else None,
            permission_mode=getattr(row, "permission_mode", "normal") or "normal",
        )
        await self._refresh_from_row(run)
        if status is not None:
            run.status = status
        return run

    async def _publish(self, event: str, run: DshRun) -> None:
        if self._bus is not None:
            await self._bus.publish(event, run.to_dict())


def _process_group_of(harness: DeepSeekHarness) -> tuple[int | None, int | None]:
    """取 harness 子进程的 (pid, pgid)。

    setid 垫片保证 pid == pgid；即便拿不到 pgid 也返回 pid，看护器会退化成单进程清理。
    测试替身没有 `_proc`，返回 (None, None)——看护器按"无进程"处理。
    """
    proc = getattr(getattr(harness, "client", None), "_proc", None)
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return None, None
    return pid, pid


def _notify_cancel(harness: DeepSeekHarness, *, session_id: str) -> None:
    """请 DSH runtime 自己收尾（会话转 idle）；子进程退出由看护器确认。"""
    harness.client.notify("session/cancel", {"sessionId": session_id})


def _progress_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    """落库用的最后事件摘要：不存文本正文，避免把整段回复塞进 JSON 列。"""
    return {
        "event_type": record.get("event_type"),
        "session_id": record.get("session_id"),
        "finish_reason": record.get("finish_reason"),
    }


__all__ = ["FluxDshClient", "DshRun", "TERMINAL_STATUSES"]
