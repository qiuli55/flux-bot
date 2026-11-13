"""GitHub 连接器：设备码 OAuth 登录 + 加密存储 + 按账号隔离。

登录走 OAuth Device Flow（不需要回调地址，手机上体验最稳）：
1. POST /login/device/code 拿 user_code 与 verification_uri；
2. 用户在浏览器输入 8 位码授权；
3. App 轮询 GET /connectors/github/login/{flow_id}，服务端拿 device_code 换 access_token。

令牌只存在 `github_connections` 表，用密钥库主密钥的 SecretBox 加密；接口只回身份与
scope，不返回任何令牌片段。连接记录按 `account_key`（App 本地账号）唯一隔离。
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.config import Settings
from flux.core.key_vault.service import KeyVaultService
from flux.errors import BadRequestError, NotFoundError, ProviderError
from flux.logging import get_logger
from flux.models.github import GithubConnection

logger = get_logger(__name__)

#: 历史请求头。Phase 2 起只作为兼容常量保留，**不再作为身份来源**：
#: 账号标识统一由登录态（`flux.api.account_scope.account_key_for_request`）推导。
ACCOUNT_HEADER = "X-Flux-Account"
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: Phase 1 之前客户端不带头时写入的历史账号；只对配置的主用户可见。
DEFAULT_ACCOUNT = "default"

#: 设备码流程在内存里保留多久（GitHub 自己也会过期，这里只是兜底清理）
_FLOW_TTL_SECONDS = 900.0

_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


def normalize_account_key(raw: str | None) -> str:
    """规整账号标识：只做格式校验，**不再把空值降级成 `'default'`**。

    Phase 2 起身份来自登录态；本函数仅用于校验/清洗已解析出的用户 id，
    空值返回空串（调用方应拒绝），非法值直接 400，不做静默降级。
    """
    value = (raw or "").strip()
    if not value:
        return ""
    if not _ACCOUNT_RE.fullmatch(value):
        raise BadRequestError(
            "账号标识不合法：只允许字母、数字、下划线与短横线（1~64 位）",
            details={"account": value},
        )
    return value


@dataclass
class _DeviceFlow:
    flow_id: str
    account_key: str
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    interval: int
    expires_at: float
    next_poll_at: float


class GitHubConnectorService:
    """`github_connections` 表与 GitHub REST 的唯一读写入口。"""

    connector_id = "github"
    connector_name = "GitHub"
    description = "代码仓库、Issue、PR 与 Actions"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        key_vault: KeyVaultService,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._key_vault = key_vault
        # 测试注入 MockTransport；生产每次临时建连，避免长期持有连接池
        self._client = client
        self._flows: dict[str, _DeviceFlow] = {}

    # --- 查询 ---

    def _account_candidates(self, account_key: str) -> list[str]:
        """当前账号 + （仅主用户）历史 `'default'` 行。

        老版本不带头时写入 `account_key='default'`；Phase 2 之后它只属于配置的
        主用户，别的用户不能看到、也不能断开这条连接。
        """
        key = normalize_account_key(account_key)
        owner = (self._settings.owner_cloud_user_id or "").strip()
        if key and owner and key == owner:
            return [key, DEFAULT_ACCOUNT]
        return [key]

    async def get_row(self, account_key: str) -> GithubConnection | None:
        candidates = self._account_candidates(account_key)
        if not candidates:
            return None
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(GithubConnection).where(GithubConnection.account_key.in_(candidates))
                )
            )
        # 当前账号优先于历史 default 行
        by_key = {row.account_key: row for row in rows}
        for key in candidates:
            if key in by_key:
                return by_key[key]
        return None

    async def is_connected(self, account_key: str) -> bool:
        return await self.get_row(account_key) is not None

    async def public_state(self, account_key: str) -> dict[str, Any]:
        """连接器列表里的一行：未连接时也是完整形状，App 不用分支解析。"""
        row = await self.get_row(account_key)
        if row is None:
            return {
                "id": self.connector_id,
                "name": self.connector_name,
                "description": self.description,
                "connected": False,
                "account": "",
                "display_name": "",
                "avatar_url": "",
                "scopes": [],
                "connected_at": None,
                "updated_at": None,
            }
        return {**row.to_public(), "description": self.description}

    # --- 设备码登录 ---

    async def start_login(self, account_key: str) -> dict[str, Any]:
        self._purge_flows()
        # 同一账号重开时丢掉旧 flow，避免用户拿着两张码分不清哪张还有效
        for flow_id in [f.flow_id for f in self._flows.values() if f.account_key == account_key]:
            self._flows.pop(flow_id, None)

        data = await self._oauth_json(
            "/login/device/code",
            {"client_id": self._settings.github_client_id, "scope": self._settings.github_scopes},
        )
        if data.get("error"):
            raise BadRequestError(
                _oauth_error_message(data),
                details={"error": data.get("error"), "connector": self.connector_id},
            )
        device_code = str(data.get("device_code") or "")
        user_code = str(data.get("user_code") or "")
        if not device_code or not user_code:
            raise ProviderError("GitHub 设备码响应缺少必要字段", details={"connector": "github"})

        interval = max(5, int(data.get("interval") or 5))
        expires_in = max(60, int(data.get("expires_in") or _FLOW_TTL_SECONDS))
        flow = _DeviceFlow(
            flow_id=uuid.uuid4().hex,
            account_key=account_key,
            device_code=device_code,
            user_code=user_code,
            verification_uri=str(data.get("verification_uri") or "https://github.com/login/device"),
            verification_uri_complete=str(data.get("verification_uri_complete") or ""),
            interval=interval,
            expires_at=time.monotonic() + expires_in,
            next_poll_at=0.0,
        )
        self._flows[flow.flow_id] = flow
        logger.info("github.login.start account=%s flow=%s", account_key, flow.flow_id)
        return {
            "flow_id": flow.flow_id,
            "user_code": flow.user_code,
            "verification_uri": flow.verification_uri,
            "verification_uri_complete": flow.verification_uri_complete,
            "expires_in": expires_in,
            "interval": interval,
        }

    async def poll_login(self, flow_id: str, account_key: str) -> dict[str, Any]:
        self._purge_flows()
        flow = self._flows.get(flow_id)
        if flow is None or flow.account_key != account_key:
            raise NotFoundError(
                "登录流程不存在或已过期，请重新发起连接",
                details={"flow_id": flow_id},
            )
        if time.monotonic() >= flow.expires_at:
            self._flows.pop(flow_id, None)
            return {"status": "expired", "connector": await self.public_state(account_key)}

        now = time.monotonic()
        if now < flow.next_poll_at:
            return {"status": "pending", "connector": await self.public_state(account_key)}

        data = await self._oauth_json(
            "/login/oauth/access_token",
            {
                "client_id": self._settings.github_client_id,
                "device_code": flow.device_code,
                "grant_type": _DEVICE_GRANT,
            },
        )
        flow.next_poll_at = now + flow.interval
        error = str(data.get("error") or "")
        if error == "authorization_pending":
            return {"status": "pending", "connector": await self.public_state(account_key)}
        if error == "slow_down":
            flow.interval += 5
            flow.next_poll_at = now + flow.interval
            return {"status": "pending", "connector": await self.public_state(account_key)}
        if error == "expired_token":
            self._flows.pop(flow_id, None)
            return {"status": "expired", "connector": await self.public_state(account_key)}
        if error:
            self._flows.pop(flow_id, None)
            return {
                "status": "failed",
                "message": _oauth_error_message(data),
                "connector": await self.public_state(account_key),
            }

        token = str(data.get("access_token") or "")
        if not token:
            raise ProviderError("GitHub 授权响应缺少 access_token", details={"connector": "github"})
        scopes = str(data.get("scope") or self._settings.github_scopes)
        user = await self._api_get(token, "/user")
        if not isinstance(user, dict):
            raise ProviderError("GitHub 用户信息格式异常", details={"connector": "github"})

        await self._save(
            account_key,
            login=str(user.get("login") or ""),
            name=str(user.get("name") or ""),
            avatar_url=str(user.get("avatar_url") or ""),
            token=token,
            scopes=scopes,
            token_kind=str(data.get("token_type") or "bearer"),
        )
        self._flows.pop(flow_id, None)
        logger.info(
            "github.login.success account=%s login=%s scopes=%s",
            account_key,
            user.get("login"),
            scopes,
        )
        return {"status": "success", "connector": await self.public_state(account_key)}

    async def disconnect(self, account_key: str) -> dict[str, Any]:
        candidates = self._account_candidates(account_key)
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(GithubConnection).where(GithubConnection.account_key.in_(candidates))
                )
            )
            if rows:
                for row in rows:
                    await session.delete(row)
                await session.commit()
                logger.info("github.disconnect account=%s rows=%s", account_key, len(rows))
        for flow_id in [f.flow_id for f in self._flows.values() if f.account_key == account_key]:
            self._flows.pop(flow_id, None)
        return await self.public_state(account_key)

    async def _save(
        self,
        account_key: str,
        *,
        login: str,
        name: str,
        avatar_url: str,
        token: str,
        scopes: str,
        token_kind: str,
    ) -> None:
        candidates = self._account_candidates(account_key)
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(GithubConnection).where(GithubConnection.account_key.in_(candidates))
                )
            )
            row = next(
                (item for item in rows if item.account_key == account_key),
                rows[0] if rows else None,
            )
            if row is None:
                row = GithubConnection(account_key=account_key)
                session.add(row)
            elif row.account_key != account_key:
                # 历史 default 行在主人重新登录后改挂到真实用户 id 上，
                # 避免同一用户被拆成两行。
                row.account_key = account_key
            row.login = login[:128]
            row.name = name[:256]
            row.avatar_url = avatar_url[:512]
            row.token_ciphertext = self._key_vault.seal_text(token)
            row.scopes = scopes[:512]
            row.token_kind = token_kind[:32]
            await session.commit()

    # --- REST（聊天工具用） ---

    async def token_for(self, account_key: str) -> str | None:
        row = await self.get_row(account_key)
        if row is None:
            return None
        return self._key_vault.open_text(row.token_ciphertext)

    async def api_get(
        self,
        account_key: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        token = await self.token_for(account_key)
        if not token:
            raise BadRequestError(
                "当前账号还没有连接 GitHub，请先到「账户 → 连接器」完成连接",
                details={"connector": self.connector_id},
            )
        response = await self._request(
            "GET",
            f"{self._settings.github_api_base.rstrip('/')}{path}",
            headers={
                **_api_headers(token),
                "Accept": "application/vnd.github+json",
            },
            params=params,
        )
        _raise_for_github(response)
        if not response.content:
            return None
        return response.json()

    async def api_get_text(
        self,
        account_key: str,
        path: str,
        *,
        accept: str,
        params: dict[str, Any] | None = None,
    ) -> str:
        token = await self.token_for(account_key)
        if not token:
            raise BadRequestError(
                "当前账号还没有连接 GitHub，请先到「账户 → 连接器」完成连接",
                details={"connector": self.connector_id},
            )
        response = await self._request(
            "GET",
            f"{self._settings.github_api_base.rstrip('/')}{path}",
            headers={**_api_headers(token), "Accept": accept},
            params=params,
        )
        _raise_for_github(response)
        return response.text

    # --- 内部 HTTP ---

    async def _oauth_json(self, path: str, data: dict[str, str]) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"{self._settings.github_oauth_base.rstrip('/')}{path}",
            data=data,
            headers={"Accept": "application/json", "User-Agent": "Flux-Bot"},
        )
        if response.status_code >= 500:
            raise ProviderError(
                f"GitHub OAuth 服务暂时不可用（HTTP {response.status_code}）",
                details={"connector": "github", "status": response.status_code},
            )
        try:
            parsed = response.json()
        except ValueError as exc:
            raise ProviderError(
                "GitHub OAuth 返回了非 JSON 响应",
                details={"connector": "github", "status": response.status_code},
            ) from exc
        return parsed if isinstance(parsed, dict) else {}

    async def _api_get(self, token: str, path: str) -> Any:
        response = await self._request(
            "GET",
            f"{self._settings.github_api_base.rstrip('/')}{path}",
            headers={**_api_headers(token), "Accept": "application/vnd.github+json"},
        )
        _raise_for_github(response)
        return response.json() if response.content else None

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            if self._client is not None:
                return await self._client.request(method, url, **kwargs)
            async with httpx.AsyncClient(
                timeout=self._settings.github_timeout_seconds,
                follow_redirects=True,
            ) as client:
                return await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"无法连接 GitHub：{exc.__class__.__name__}",
                details={"connector": "github"},
            ) from exc

    def _purge_flows(self) -> None:
        now = time.monotonic()
        for flow_id in [f.flow_id for f in self._flows.values() if f.expires_at <= now]:
            self._flows.pop(flow_id, None)


def _api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Flux-Bot",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _raise_for_github(response: httpx.Response) -> None:
    if response.status_code == 401:
        raise BadRequestError(
            "GitHub 授权已失效，请到「账户 → 连接器」重新连接",
            details={"connector": "github", "status": 401},
        )
    if response.status_code == 404:
        raise BadRequestError(
            "GitHub 上没有找到该资源（仓库名或权限可能不对）",
            details={"connector": "github", "status": 404},
        )
    if response.status_code == 403:
        raise BadRequestError(
            "GitHub 拒绝了这次请求（可能是权限不足或触发了限流）",
            details={"connector": "github", "status": 403},
        )
    if response.status_code >= 400:
        raise ProviderError(
            f"GitHub 接口返回 HTTP {response.status_code}",
            details={"connector": "github", "status": response.status_code},
        )


def _oauth_error_message(data: dict[str, Any]) -> str:
    error = str(data.get("error") or "")
    if error == "device_flow_disabled":
        return (
            "GitHub OAuth App 未开启 Device Flow：请在 GitHub → Settings → Developer settings → "
            "OAuth Apps 里勾选 Enable Device Flow 并保存，再重试"
        )
    if error == "access_denied":
        return "你取消了 GitHub 授权"
    if error == "incorrect_client_credentials":
        return "GitHub OAuth App 的 Client ID 配置有误"
    if error == "expired_token":
        return "授权码已过期，请重新发起连接"
    return str(data.get("error_description") or error or "GitHub 授权失败")


__all__ = ["ACCOUNT_HEADER", "DEFAULT_ACCOUNT", "GitHubConnectorService", "normalize_account_key"]
