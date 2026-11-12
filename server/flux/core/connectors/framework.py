"""用户账号连接器框架：可安装定义目录 + 按账号隔离的安装状态。

注意与 `flux.connectors.base.ConnectorRegistry` 的区别：
- 后者是 Agent 执行侧的连接器（execute / health / permissions），由 Agent 工具调用；
- 本模块是用户账号侧的连接器（catalog / install / uninstall / 凭据），对应 App
  「账户 → 连接器」页面。两者不合并，避免把"用户授权"与"Agent 能力"混为一谈。

GitHub 继续复用 `GitHubConnectorService` 与 `github_connections` 表（设备码 OAuth、
令牌加密、账号隔离都不变）。本框架只登记它的 catalog 定义，并在列表 / 卸载时委托；
通用连接器（api_key 等）的安装元数据与凭据落在 `connector_installations` 表。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.config import Settings
from flux.core.connectors.calendar import CalendarConnectorService
from flux.core.github.service import GitHubConnectorService
from flux.core.key_vault.service import KeyVaultService
from flux.errors import BadRequestError, ConflictError, NotFoundError
from flux.logging import get_logger
from flux.models.connector_installation import ConnectorInstallation

logger = get_logger(__name__)

AUTH_OAUTH = "oauth"
AUTH_API_KEY = "api_key"
_AUTH_TYPES = frozenset({AUTH_OAUTH, AUTH_API_KEY})
_CONNECTOR_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SCOPE_MAX_CHARS = 512


@dataclass(frozen=True)
class ConnectorDefinition:
    """一个可安装连接器的静态定义（不含任何账号数据）。"""

    id: str
    name: str
    description: str
    auth_types: tuple[str, ...]
    scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _CONNECTOR_ID_RE.fullmatch(self.id):
            raise ValueError(f"连接器 id 不合法：{self.id!r}")
        if not self.auth_types:
            raise ValueError(f"连接器 {self.id} 至少需要一种鉴权方式")
        unknown = set(self.auth_types) - _AUTH_TYPES
        if unknown:
            raise ValueError(f"连接器 {self.id} 声明了不支持的鉴权方式：{sorted(unknown)}")

    def to_catalog(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "auth_types": list(self.auth_types),
            "scopes": list(self.scopes),
        }


class ConnectorCatalog:
    """可安装连接器定义的注册表（进程内、只读目录）。"""

    def __init__(self) -> None:
        self._definitions: dict[str, ConnectorDefinition] = {}

    def register(self, definition: ConnectorDefinition) -> None:
        if definition.id in self._definitions:
            raise ValueError(f"连接器 {definition.id} 已经注册")
        self._definitions[definition.id] = definition
        logger.info(
            "connector.catalog.register id=%s auth_types=%s",
            definition.id,
            ",".join(definition.auth_types),
        )

    def get(self, connector_id: str) -> ConnectorDefinition:
        definition = self._definitions.get(connector_id)
        if definition is None:
            raise NotFoundError(
                "连接器不存在",
                details={"connector": connector_id, "catalog": sorted(self._definitions)},
            )
        return definition

    def all(self) -> list[ConnectorDefinition]:
        return list(self._definitions.values())

    def catalog(self) -> list[dict[str, Any]]:
        return [definition.to_catalog() for definition in self._definitions.values()]


def build_default_catalog(settings: Settings) -> ConnectorCatalog:
    """生产目录：GitHub（OAuth 设备码）+ Calendar（CalDAV / ICS，api_key）。

    GitHub 的 scope 取自 `settings.github_scopes`，与设备码登录流程实际请求的范围一致。
    Calendar 的 api_key 指 CalDAV 密码或 ICS 订阅密码；公开 ICS 订阅可以留空。
    """
    catalog = ConnectorCatalog()
    catalog.register(
        ConnectorDefinition(
            id=GitHubConnectorService.connector_id,
            name=GitHubConnectorService.connector_name,
            description=GitHubConnectorService.description,
            auth_types=(AUTH_OAUTH,),
            scopes=tuple(part for part in settings.github_scopes.split() if part),
        )
    )
    catalog.register(
        ConnectorDefinition(
            id=CalendarConnectorService.connector_id,
            name=CalendarConnectorService.connector_name,
            description=CalendarConnectorService.description,
            auth_types=(AUTH_API_KEY,),
            scopes=("calendar.read", "calendar.write"),
        )
    )
    return catalog


class ConnectorFrameworkService:
    """连接器安装状态的唯一读写入口，所有查询都带 `account_key` 约束。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        key_vault: KeyVaultService,
        catalog: ConnectorCatalog,
        *,
        github: GitHubConnectorService | None = None,
        calendar: CalendarConnectorService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._key_vault = key_vault
        self._catalog = catalog
        self._github = github
        self._calendar = calendar

    # --- 目录 ---

    def register(self, definition: ConnectorDefinition) -> None:
        """运行时注册定义（测试 / 后续内置连接器共用同一入口）。"""
        self._catalog.register(definition)

    async def list_catalog(self) -> list[dict[str, Any]]:
        return self._catalog.catalog()

    # --- 安装状态 ---

    async def installation(
        self, account_key: str, connector_id: str
    ) -> ConnectorInstallation | None:
        async with self._session_factory() as session:
            return (
                await session.scalars(
                    select(ConnectorInstallation).where(
                        ConnectorInstallation.account_key == account_key,
                        ConnectorInstallation.connector_id == connector_id,
                    )
                )
            ).first()

    async def state(self, account_key: str, connector_id: str) -> dict[str, Any]:
        """一个连接器的对外状态；未安装时也返回完整形状，客户端无需分支解析。"""
        definition = self._catalog.get(connector_id)
        if self._is_github(connector_id):
            assert self._github is not None
            return await self._github.public_state(account_key)
        if self._is_calendar(connector_id):
            assert self._calendar is not None
            return await self._calendar.public_state(account_key)

        row = await self.installation(account_key, connector_id)
        if row is not None:
            return row.to_public(name=definition.name, description=definition.description)
        return {
            "id": definition.id,
            "name": definition.name,
            "description": definition.description,
            "connected": False,
            "account": "",
            "display_name": "",
            "avatar_url": "",
            "scopes": [],
            "connected_at": None,
            "updated_at": None,
        }

    async def list_states(self, account_key: str) -> list[dict[str, Any]]:
        return [await self.state(account_key, definition.id) for definition in self._catalog.all()]

    # --- 安装 / 卸载 ---

    async def install(
        self,
        account_key: str,
        connector_id: str,
        *,
        auth_type: str | None = None,
        api_key: str | None = None,
        scopes: list[str] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        definition = self._catalog.get(connector_id)
        requested = (auth_type or "").strip()
        if not requested:
            if len(definition.auth_types) != 1:
                raise BadRequestError(
                    "请指定 auth_type",
                    details={"connector": connector_id, "auth_types": list(definition.auth_types)},
                )
            requested = definition.auth_types[0]
        if requested not in definition.auth_types:
            raise BadRequestError(
                "该连接器不支持这种鉴权方式",
                details={"connector": connector_id, "auth_types": list(definition.auth_types)},
            )

        if requested == AUTH_OAUTH:
            # OAuth 类连接器必须走各自的授权流程（GitHub 是设备码登录）；
            # 通用 install 接口只负责 api_key，避免出现第二个半成品 OAuth 实现。
            raise BadRequestError(
                "OAuth 连接器请使用对应的登录流程",
                details={
                    "connector": connector_id,
                    "auth_type": AUTH_OAUTH,
                    "login_path": f"/api/v1/connectors/{connector_id}/login/start",
                },
            )

        # 日历连接器把 mode / url / username / password 打包进同一份加密凭据；
        # ICS 订阅允许空密码，因此不能走下面 api_key 必填的通用分支。
        if self._is_calendar(connector_id):
            assert self._calendar is not None
            scope_value = self._normalize_scopes(definition, scopes)
            return await self._calendar.install(
                account_key,
                api_key=api_key,
                settings=settings,
                scopes=scope_value,
            )

        secret = (api_key or "").strip()
        if not secret:
            raise BadRequestError(
                "安装 API Key 类连接器需要提供 api_key",
                details={"connector": connector_id, "auth_type": AUTH_API_KEY},
            )
        if await self.installation(account_key, connector_id) is not None:
            raise ConflictError(
                "该连接器已安装；如需更换凭据请先卸载",
                details={"connector": connector_id},
            )

        scope_value = self._normalize_scopes(definition, scopes)
        async with self._session_factory() as session:
            session.add(
                ConnectorInstallation(
                    account_key=account_key,
                    connector_id=connector_id,
                    auth_type=AUTH_API_KEY,
                    scopes=scope_value,
                    credential_ciphertext=self._key_vault.seal_text(secret),
                )
            )
            await session.commit()
        logger.info(
            "connector.install account=%s connector=%s auth=%s scopes=%s",
            account_key,
            connector_id,
            AUTH_API_KEY,
            scope_value,
        )
        return await self.state(account_key, connector_id)

    async def uninstall(self, account_key: str, connector_id: str) -> dict[str, Any]:
        self._catalog.get(connector_id)
        if self._is_github(connector_id):
            assert self._github is not None
            return await self._github.disconnect(account_key)

        async with self._session_factory() as session:
            row = (
                await session.scalars(
                    select(ConnectorInstallation).where(
                        ConnectorInstallation.account_key == account_key,
                        ConnectorInstallation.connector_id == connector_id,
                    )
                )
            ).first()
            if row is not None:
                await session.delete(row)
                await session.commit()
                logger.info(
                    "connector.uninstall account=%s connector=%s auth=%s",
                    account_key,
                    connector_id,
                    row.auth_type,
                )
        return await self.state(account_key, connector_id)

    async def credential_for(self, account_key: str, connector_id: str) -> str | None:
        """读取安装时写入的凭据（供未来的通用执行路径使用，绝不进 API 响应）。"""
        self._catalog.get(connector_id)
        if self._is_github(connector_id):
            assert self._github is not None
            return await self._github.token_for(account_key)
        row = await self.installation(account_key, connector_id)
        if row is None or row.credential_ciphertext is None:
            return None
        return self._key_vault.open_text(row.credential_ciphertext)

    # --- 内部 ---

    def _is_github(self, connector_id: str) -> bool:
        return self._github is not None and connector_id == self._github.connector_id

    def _is_calendar(self, connector_id: str) -> bool:
        return self._calendar is not None and connector_id == self._calendar.connector_id

    @staticmethod
    def _normalize_scopes(definition: ConnectorDefinition, scopes: list[str] | None) -> str:
        requested = definition.scopes if scopes is None else tuple(scopes)
        cleaned = [scope.strip() for scope in requested if scope and scope.strip()]
        value = " ".join(cleaned)
        if len(value) > _SCOPE_MAX_CHARS:
            raise BadRequestError(
                "scopes 过长",
                details={"connector": definition.id, "max_chars": _SCOPE_MAX_CHARS},
            )
        return value


__all__ = [
    "AUTH_API_KEY",
    "AUTH_OAUTH",
    "ConnectorCatalog",
    "ConnectorDefinition",
    "ConnectorFrameworkService",
    "build_default_catalog",
]
