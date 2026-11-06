"""Agent Manager（主规格 §5.1）：Flux 侧的 Agent 档案注册表。

Flux 本身不执行 Agent——没有 loop、不组装 prompt、不存对话（目标架构 §1）。
本类只维护平台上的 Agent 档案：创建、查询、列出、停止，并在状态跃迁时发布事件；
执行由各 Agent 自己的运行时完成（内置 DSH Agent 或外部 Codex / Claude Code 等）。
档案里的 permissions 是平台侧权限的唯一来源（Connector 按 agent_id 解析）。

P3-16 起档案落库（agents 表），canonical id（UUID）是 Agent 身份的唯一权威：
内存注册表只是它的运行时视图，启动时用 `load_from_db()` 重建，
MCP 令牌、Proposal attribution、任务归属统一引用这个 UUID，不再有"第二套身份"。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from flux.core.agent_runtime.lifecycle import assert_transition
from flux.core.agent_runtime.manifest import AgentManifest, builtin_manifests
from flux.core.agent_runtime.repository import AgentRepository
from flux.core.agent_runtime.runtimes.base import (
    DEFAULT_RUNTIME,
    KNOWN_RUNTIMES,
    RUNTIME_NATIVE_STREAMING,
)
from flux.core.event.bus import EventBus, Events
from flux.enums import AgentRole, AgentState, Capability
from flux.errors import NotFoundError, ValidationError
from flux.logging import get_logger

logger = get_logger(__name__)


@dataclass
class AgentSpec:
    """Agent 档案定义（Identity + Capabilities 两组字段，§5.1）。

    不含模型与 system prompt：模型与对话归 Agent 自己（目标架构 §1）。
    """

    name: str
    role: AgentRole
    description: str = ""
    skills: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    permissions: frozenset[Capability] = frozenset()
    #: 执行该 Agent 的 runtime（P2-1 §6.2）：dsh（内置，默认）/ codex / opencode。
    #: 落库在 agents.config["runtime"]；曾经不进 to_dict，2026-10-06 起随 to_dict 输出
    #: （UI 需要看到"这个 Agent 由谁执行"，否则接入 CLI 后无法确认任务会走哪条路）——
    #: 属向后兼容的加字段，M0 冻结的既有字段语义不变。
    runtime: str = DEFAULT_RUNTIME

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "role": str(self.role),
            "description": self.description,
            "skills": list(self.skills),
            "tools": list(self.tools),
            "permissions": sorted(str(p) for p in self.permissions),
            "runtime": self.runtime,
            # 客户端据此标注「非逐字流式」：这类 Agent 仍可正常绑定与执行，
            # 只是正文会在结尾整段到达（平台按分段回放兜底，不冒充原生逐 token）。
            "native_streaming": bool(RUNTIME_NATIVE_STREAMING.get(self.runtime, False)),
        }


@dataclass
class AgentHandle:
    """注册表里的 Agent 档案句柄。"""

    spec: AgentSpec
    state: AgentState = AgentState.CREATED
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    @property
    def id_str(self) -> str:
        return str(self.id)

    def to_dict(self) -> dict[str, object]:
        return {"id": self.id_str, "state": str(self.state), "spec": self.spec.to_dict()}


class AgentManager:
    def __init__(
        self,
        bus: EventBus | None = None,
        repository: AgentRepository | None = None,
    ) -> None:
        self._bus = bus
        self._repository = repository
        self._agents: dict[uuid.UUID, AgentHandle] = {}

    # --- 接口：create ---

    async def create(self, spec: AgentSpec) -> AgentHandle:
        self._validate_runtime(spec.runtime)
        handle = AgentHandle(spec=spec)
        self._agents[handle.id] = handle
        await self._transition(handle, AgentState.INITIALIZING)
        await self._transition(handle, AgentState.READY)
        await self._persist(handle)
        logger.info("agent.create id=%s name=%s role=%s", handle.id_str, spec.name, spec.role)
        return handle

    async def create_from_manifest(self, manifest: AgentManifest) -> AgentHandle:
        """按 Manifest 声明创建 Agent 档案（§6.2）：声明式配置 → 运行时档案。"""
        return await self.create(
            AgentSpec(
                name=manifest.name,
                role=manifest.role,
                description=manifest.description,
                skills=manifest.skills,
                tools=manifest.tools,
                permissions=manifest.permissions,
            )
        )

    async def create_builtin_agents(self) -> dict[str, AgentHandle]:
        """创建第一批内置档案（Tech Lead / Developer / Reviewer / Tester）。"""
        return {name: await self.create_from_manifest(m) for name, m in builtin_manifests().items()}

    # --- 接口：查询 ---

    def get(self, agent_id: str | uuid.UUID) -> AgentHandle:
        key = self._as_uuid(agent_id)
        handle = self._agents.get(key)
        if handle is None:
            raise NotFoundError(f"Agent {agent_id} 不存在", details={"agent_id": str(agent_id)})
        return handle

    def find_by_name(self, name: str) -> AgentHandle | None:
        """按档案名解析 Agent（供令牌签发 / 内置 Agent 启动器按名字定位 canonical id）。

        名字不唯一时不猜：取最早创建的一个并记警告——令牌与 attribution 必须指向确定身份。
        """
        target = (name or "").strip()
        if not target:
            return None
        matches = [h for h in self._agents.values() if h.spec.name == target]
        if not matches:
            return None
        if len(matches) > 1:
            logger.warning("agent.find_by_name 命中多个同名档案 name=%s，取最早创建的一个", target)
        return min(matches, key=lambda h: h.id.int)

    def list(self) -> list[AgentHandle]:
        return list(self._agents.values())

    def count(self) -> int:
        return len(self._agents)

    async def load_from_db(self) -> list[AgentHandle]:
        """启动时用 agents 表重建内存注册表（canonical id 以库为准）。

        库里没有档案（首次启动）时返回空；调用方再按需创建内置档案。
        """
        if self._repository is None:
            return []
        handles: list[AgentHandle] = []
        for row in await self._repository.list():
            handle = self._handle_from_row(row)
            self._agents[handle.id] = handle
            handles.append(handle)
        if handles:
            logger.info("agent.load_from_db 已从库重建 %d 个档案", len(handles))
        return handles

    # --- 接口：stop ---

    async def stop(self, agent_id: str | uuid.UUID) -> AgentHandle:
        handle = self.get(agent_id)
        if handle.state is AgentState.STOPPED:
            return handle
        await self._transition(handle, AgentState.STOPPED)
        return handle

    # --- 内部 ---

    @staticmethod
    def _validate_runtime(runtime: str) -> None:
        """创建 Agent 时校验 runtime 取值合法（P2-1 §6.2），非法即 422，不静默兜底。

        非原生逐 token 的 runtime 也允许创建：档案会带 `native_streaming=False`
        供客户端标注，执行时正文走分段回放兜底——能力可用，但不能含糊其辞地当作流式。
        未登记在 RUNTIME_NATIVE_STREAMING 的 runtime 视为「不支持原生流式」，由契约
        测试强制要求每个已知 runtime 显式登记取值。
        """
        if runtime not in KNOWN_RUNTIMES:
            raise ValidationError(
                f"未知的 runtime：{runtime}",
                details={"runtime": runtime, "known": list(KNOWN_RUNTIMES)},
            )
        if not RUNTIME_NATIVE_STREAMING.get(runtime, False):
            logger.info(
                "agent.create 使用非逐 token 流式 runtime=%s：档案将标注 native_streaming=False",
                runtime,
            )

    async def _persist(self, handle: AgentHandle) -> None:
        """把档案落库（未装配仓储时跳过——纯内存用例仍可只用 AgentManager）。"""
        if self._repository is None:
            return
        await self._repository.save(
            agent_id=handle.id,
            name=handle.spec.name,
            role=str(handle.spec.role),
            status=str(handle.state),
            description=handle.spec.description,
            skills=tuple(handle.spec.skills),
            tools=tuple(handle.spec.tools),
            permissions=tuple(sorted(str(p) for p in handle.spec.permissions)),
            runtime=handle.spec.runtime,
        )

    @staticmethod
    def _handle_from_row(row: object) -> AgentHandle:
        """把 agents 行还原成运行时句柄（config JSON 回填档案附属字段）。"""
        config = getattr(row, "config", None) or {}
        spec = AgentSpec(
            name=str(getattr(row, "name", "")),
            role=AgentRole(str(getattr(row, "role", AgentRole.DEVELOPER))),
            description=str(config.get("description", "")),
            skills=tuple(config.get("skills") or ()),
            tools=tuple(config.get("tools") or ()),
            permissions=frozenset(Capability(str(p)) for p in (config.get("permissions") or ())),
            runtime=str(config.get("runtime") or DEFAULT_RUNTIME),
        )
        return AgentHandle(
            spec=spec,
            state=AgentState(str(getattr(row, "status", AgentState.CREATED))),
            id=row.id,
        )

    async def _transition(self, handle: AgentHandle, target: AgentState) -> None:
        assert_transition(handle.state, target)
        previous = handle.state
        handle.state = target
        logger.debug("agent.state %s: %s → %s", handle.id_str, previous, target)
        if self._bus is not None:
            await self._bus.publish(
                Events.AGENT_STATE_CHANGED,
                {"agent_id": handle.id_str, "from": str(previous), "to": str(target)},
            )

    @staticmethod
    def _as_uuid(agent_id: str | uuid.UUID) -> uuid.UUID:
        if isinstance(agent_id, uuid.UUID):
            return agent_id
        try:
            return uuid.UUID(str(agent_id))
        except ValueError as exc:
            raise NotFoundError(
                f"Agent 标识非法：{agent_id}", details={"agent_id": str(agent_id)}
            ) from exc
