"""MCP 鉴权：Agent 接入令牌的签发与校验（目标架构 §3.2；P3-16 统一身份）。

四条硬规则，任何一条被放松都等于开了后门：

1. **明文只出现一次**：`fxt_` + 32 字节随机 hex，签名时返回给调用方，库里只存 sha256；
2. **身份不可自报**：`agent_id` 与 `scopes` 一律来自令牌记录，请求体里同名字段无效
   （写入类工具的 provenance 由服务端盖章）；
3. **现查现判**：每次调用都查库，`revoked_at` 非空立即失效，不做进程内缓存——
   撤销要"下一个请求就生效"，而不是重启后才生效；
4. **令牌只绑定真实 Agent**（P3-16 §4.2）：`agent_id` 必须是 Agent Registry 里的 canonical
   UUID；认证时回查注册表解析 display_name 与 permissions，scopes 取"令牌 ∩ 档案"交集，
   档案不存在即拒绝（fail-closed）。名字只是 slug，不再充当身份。
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.core.agent_runtime.manager import AgentManager, AgentSpec
from flux.enums import AgentRole, Capability
from flux.errors import AuthenticationError, ConfigurationError, NotFoundError, ValidationError
from flux.logging import get_logger
from flux.models.agent_token import AgentToken

logger = get_logger(__name__)

TOKEN_PREFIX = "fxt_"
#: 令牌随机部分的字节数（hex 编码后为 64 个字符）
TOKEN_SECRET_BYTES = 32
#: 永不可授予的能力（目标架构 §3.5）：密钥访问不进任何令牌的 scopes
FORBIDDEN_SCOPES: frozenset[Capability] = frozenset({Capability.SECRET_ACCESS})

#: 由旧自由字符串迁移出来的档案角色（历史 token 多为执行类 Agent）
_LEGACY_AGENT_ROLE = AgentRole.DEVELOPER
_LEGACY_AGENT_DESCRIPTION = "由旧自由字符串令牌迁移生成的 Agent 档案（P3-16）"


def hash_token(raw: str) -> str:
    """令牌的存储形态：sha256 十六进制。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_scopes(values: Iterable[Capability | str]) -> tuple[Capability, ...]:
    """把入参规整成能力集合；未知能力与硬禁用能力一律报错（不静默丢弃）。"""
    parsed: list[Capability] = []
    for value in values:
        try:
            capability = value if isinstance(value, Capability) else Capability(str(value))
        except ValueError as exc:
            raise ValidationError(
                f"未知的能力标识：{value}",
                details={"scope": str(value), "known": sorted(str(c) for c in Capability)},
            ) from exc
        if capability in FORBIDDEN_SCOPES:
            raise ValidationError(
                f"能力 {capability} 不允许授予任何 Agent 令牌（目标架构 §3.5 硬禁令）",
                details={"scope": str(capability)},
            )
        if capability not in parsed:
            parsed.append(capability)
    return tuple(parsed)


def is_canonical_agent_id(value: str | None) -> bool:
    """是否为 canonical UUID 形态的 agent_id。"""
    if not value:
        return False
    try:
        uuid.UUID(str(value).strip())
    except (ValueError, AttributeError, TypeError):
        return False
    return True


@dataclass(frozen=True)
class AgentIdentity:
    """一次 MCP 调用的调用方身份——每个字段都来自令牌 + 注册表，不来自请求体。

    `agent_id` 是 canonical UUID，`name` 是注册表里的 display_name（slug）。
    `run_id` 不属于令牌：它来自 Agent 进程注入的 `X-Flux-Run-Id` 头，由 MCP 层校验该 Run
    归属本身份后才附加（见 `server.mcp_endpoint`）——用于把 Agent 派生的终端会话绑到 Run 上，
    Run 终态时据此回收，避免终端命令进程泄漏。
    """

    agent_id: str
    token_id: str
    scopes: frozenset[Capability]
    name: str = ""
    run_id: str | None = None
    #: 本次 Run 的权限模式（plan / normal / full），由 MCP 层从 agent_runs 快照读取后附加。
    #: 不在令牌里：Agent 用长期令牌，能力位是写死的超集，只能按 Run 判定模式。
    permission_mode: str = "normal"
    #: Bot 级执行白名单（来自 Bot 配置）：None = 不额外限制。
    #: 由 MCP 层按 run → task → bot 解析后附加，仅约束本次 Run。
    allowed_tools: tuple[str, ...] | None = None
    allowed_commands: tuple[str, ...] | None = None
    allowed_paths: tuple[str, ...] | None = None

    def has(self, capability: Capability) -> bool:
        return capability in self.scopes


class AgentTokenService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        agents: AgentManager | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._agents = agents

    async def issue(
        self,
        *,
        agent_id: str,
        scopes: Iterable[Capability | str],
        label: str = "",
    ) -> tuple[AgentToken, str]:
        """签发一枚令牌，返回 (记录, 明文)。明文只在这里出现一次，调用方必须立即交出去。

        `agent_id` 必须是注册表里真实存在的 canonical UUID：不存在就拒绝，
        绝不为一个自由字符串建身份（P3-16 §4.2 / TC-16B）。
        """
        canonical = self._require_canonical(agent_id)
        handle = self._require_registered(canonical)
        granted = parse_scopes(scopes)
        raw = f"{TOKEN_PREFIX}{secrets.token_hex(TOKEN_SECRET_BYTES)}"
        token = AgentToken(
            agent_id=canonical,
            label=label.strip(),
            token_hash=hash_token(raw),
            scopes=[str(scope) for scope in granted],
        )
        async with self._session_factory() as session:
            session.add(token)
            await session.commit()
        # 日志只记身份与能力，不记明文也不记哈希
        logger.info(
            "agent_token.issue agent=%s name=%s scopes=%s",
            canonical,
            handle.spec.name,
            [str(s) for s in granted],
        )
        return token, raw

    async def ensure_agent(self, *, name: str, permissions: Iterable[Capability | str]) -> object:
        """按名字取档案；不存在则建一个（供内置 Agent 启动器与旧令牌迁移使用）。

        返回 AgentHandle。这么做的意义是：调用方（如 `_ensure_mcp_token`）只需要一个
        名字，就能拿到稳定的 canonical UUID，从而给令牌一个真实身份。
        """
        if self._agents is None:
            raise ConfigurationError(
                "未装配 AgentManager，无法按名字解析/创建 Agent 档案",
                details={"hint": "由容器把 container.agents 传给 AgentTokenService"},
            )
        target = (name or "").strip()
        if not target:
            raise ValidationError("Agent 名称不能为空", details={"name": name})
        existing = self._agents.find_by_name(target)
        if existing is not None:
            return existing
        return await self._agents.create(
            AgentSpec(
                name=target,
                role=_LEGACY_AGENT_ROLE,
                description=_LEGACY_AGENT_DESCRIPTION,
                permissions=frozenset(parse_scopes(permissions)),
            )
        )

    async def list(self, *, agent_id: str | None = None) -> list[AgentToken]:
        statement = select(AgentToken).order_by(AgentToken.created_at, AgentToken.id)
        if agent_id:
            statement = statement.where(AgentToken.agent_id == agent_id)
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def revoke(self, token_id: str | uuid.UUID) -> AgentToken:
        key = self._as_uuid(token_id)
        async with self._session_factory() as session:
            token = await session.get(AgentToken, key)
            if token is None:
                raise NotFoundError(f"令牌 {token_id} 不存在", details={"token_id": str(token_id)})
            if token.revoked_at is None:
                token.revoked_at = datetime.now(timezone.utc)
            await session.commit()
        logger.info("agent_token.revoke id=%s agent=%s", token_id, token.agent_id)
        return token

    async def authenticate(self, raw: str | None) -> AgentIdentity:
        """校验明文令牌；任何不通过都抛 AuthenticationError（401，fail-closed）。"""
        if not raw or not raw.strip():
            raise AuthenticationError("缺少 Agent 接入令牌")
        candidate = raw.strip()
        if not candidate.startswith(TOKEN_PREFIX):
            raise AuthenticationError("Agent 接入令牌格式非法")
        digest = hash_token(candidate)
        async with self._session_factory() as session:
            token = await session.scalar(select(AgentToken).where(AgentToken.token_hash == digest))
        if token is None or token.revoked_at is not None:
            raise AuthenticationError("Agent 接入令牌无效或已撤销")
        return self._resolve_identity(token)

    async def identity_for(self, agent_id: str) -> AgentIdentity | None:
        """按 canonical agent_id 取一枚未撤销令牌的身份，供进程内组件复用。

        找不到时返回 None，由调用方决定报错方式——这里不兜底造一份身份。
        """
        if not is_canonical_agent_id(agent_id):
            return None
        async with self._session_factory() as session:
            token = await session.scalar(
                select(AgentToken)
                .where(AgentToken.agent_id == agent_id, AgentToken.revoked_at.is_(None))
                .order_by(AgentToken.created_at)
            )
        if token is None:
            return None
        return self._resolve_identity(token)

    async def migrate_legacy_tokens(self) -> int:
        """把历史"自由字符串 agent_id"的令牌迁移到 canonical UUID（P3-16 Step 8）。

        能按名字在注册表里找到档案的直接改写；找不到的先补建档案再改写——
        旧 token 里那个名字就是它的 display_name，不能因为迁移把在用的接入点废掉。
        返回被迁移的令牌数。
        """
        if self._agents is None:
            return 0
        async with self._session_factory() as session:
            rows = list(await session.scalars(select(AgentToken)))
        migrated = 0
        for row in rows:
            if is_canonical_agent_id(row.agent_id):
                continue
            name = (row.agent_id or "").strip()
            if not name:
                continue
            handle = await self.ensure_agent(name=name, permissions=_known_scopes(row.scopes))
            canonical = handle.id_str  # type: ignore[attr-defined]
            async with self._session_factory() as session:
                token = await session.get(AgentToken, row.id)
                if token is None or token.agent_id == canonical:
                    continue
                token.agent_id = canonical
                await session.commit()
            migrated += 1
        if migrated:
            logger.info("agent_token.migrate_legacy 已迁移 %d 枚旧令牌到 canonical UUID", migrated)
        return migrated

    # --- 内部 ---

    def _resolve_identity(self, token: AgentToken) -> AgentIdentity:
        """令牌 → 注册表 → 能力交集。注册表未装配或解析不到档案即拒绝（fail-closed）。

        纯仓储单测（未注入 AgentManager）时退化为"仅按令牌声明"，生产路径永远注入注册表。
        """
        granted = frozenset(_known_scopes(token.scopes))
        if self._agents is None:
            return AgentIdentity(agent_id=token.agent_id, token_id=str(token.id), scopes=granted)
        try:
            handle = self._agents.get(token.agent_id)
        except NotFoundError as exc:
            raise AuthenticationError(
                "令牌绑定的 Agent 不在注册表中，已拒绝（canonical 身份无法解析）",
                details={"agent_id": token.agent_id},
            ) from exc
        return AgentIdentity(
            agent_id=handle.id_str,
            name=handle.spec.name,
            token_id=str(token.id),
            # 令牌能力与档案权限取交集：档案被收权，令牌立即可用面同步收窄
            scopes=granted & handle.spec.permissions,
        )

    @staticmethod
    def _require_canonical(agent_id: str) -> str:
        candidate = (agent_id or "").strip()
        if not is_canonical_agent_id(candidate):
            raise ValidationError(
                "agent_id 必须是 Agent Registry 里的 canonical UUID（P3-16：身份只有一套）",
                details={"agent_id": str(agent_id)},
            )
        return str(uuid.UUID(candidate))

    def _require_registered(self, canonical: str):  # noqa: ANN202 - 返回 AgentHandle
        if self._agents is None:
            raise ConfigurationError(
                "未装配 AgentManager，无法校验 agent_id 是否真实存在",
                details={"hint": "由容器把 container.agents 传给 AgentTokenService"},
            )
        # get 找不到会抛 NotFoundError（API 层映射为 404）
        return self._agents.get(canonical)

    @staticmethod
    def _as_uuid(value: str | uuid.UUID) -> uuid.UUID:
        if isinstance(value, uuid.UUID):
            return value
        try:
            return uuid.UUID(str(value))
        except ValueError as exc:
            raise NotFoundError(f"令牌标识非法：{value}", details={"token_id": str(value)}) from exc


def _known_scopes(values: Iterable[object]) -> list[Capability]:
    """把库里存的字符串还原成能力集合。

    遇到已废弃/不认识的能力标识只跳过——它代表的权限本就不存在，
    跳过等于不给权限，符合 fail-closed；报错反而会让整枚令牌不可用。
    """
    known: list[Capability] = []
    for value in values or []:
        try:
            capability = value if isinstance(value, Capability) else Capability(str(value))
        except ValueError:
            logger.warning("令牌里存在未知能力标识，已忽略：%s", value)
            continue
        if capability in FORBIDDEN_SCOPES:
            logger.error("令牌里出现硬禁用能力，已忽略：%s", capability)
            continue
        known.append(capability)
    return known
