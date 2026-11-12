"""REST 面的令牌鉴权（公网暴露前置）。

为什么需要：Flux 的 REST 面有 `terminal.execute` 等价能力（终端会话可执行任意命令）、
`workspace.apply`（落盘）、`git.commit`——把 `/api/v1` 裸暴露到公网等于把服务器 shell 交出去。
MCP 面本来就有 Agent 令牌，REST 面此前完全没有身份校验（只在本机用）。

规则（任一通过即放行）：

0. 带的是**账号云**（B 机 `flux.qiuli55.top`，独立私有仓库）签发的 access token
   （`typ=access`），且与云共享的 HS256 密钥本地验签通过 → 放行。这就是 Flux 本体的
   登录闭环：令牌由唯一的账号权威签发，本机只验签、不每次回源；
1. 请求带 `Authorization: Bearer <token>` 且与 `FLUX_AUTH_TOKEN` **常量时间**比对通过 → 放行；
2. 设备令牌（`fbd_` 前缀）：查库校验 sha256、撤销与过期状态；命中后把设备身份放到
   `request.state.principal`。为降低每请求打库压力，带 30 秒进程内缓存；撤销会立即
   清除本进程缓存，其他进程最迟在 TTL 后生效（见 `flux.core.devices.service`）；
3. 例外：来源是回环地址**且**没有任何转发头（`X-Forwarded-For` / `X-Real-IP` / `Forwarded`）
   → 放行。这条例外的用途是保护"同机直连"：桌面端（Electron 主进程反代）、
   `make run` + 本机浏览器、本机 CLI 探活都不必带令牌；
4. 其余一律 401（`unauthenticated`，不区分"没带 / 带错 / 格式不对"——区分等于给探测信号）。

`FLUX_AUTH_TOKEN` 留空时由 `FLUX_ALLOW_NO_AUTH` 决定兜底策略：
- 未显式设置（None）= 旧行为，整体开放（仅 flux.main 兼容，Bot 节点会把它收敛为 False）；
- False = 只允许第 3 条的同机直连，其余请求 401（Bot 节点默认，fail-closed）；
- True = 显式开放（仅限完全可信的内网，启动时告警）。

免鉴权白名单只有设备配对兑换 `POST /api/v1/devices/pair`：它本身就是"用一次性配对码
换令牌"的入口，不能要求手机先持有令牌。其余设备管理接口仍由全局依赖把关。

第 3 条是**反向代理配置的硬约束**：反代必须转发 `X-Forwarded-For`（见 `deploy/nginx-flux.conf`
里的 `proxy_set_header`），否则经代理进来的公网请求会以"回环 + 无转发头"的形态被误判为本地直连。
部署脚本用 `curl` 不带令牌复验 401，正是为了盯住这条约束。
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from flux.api.deps import get_container
from flux.cloud.session import verify_hs256
from flux.config import Settings
from flux.errors import AuthenticationError, PermissionDeniedError

#: 视为"同机直连"的来源地址。Unix socket 下 ASGI 的 client 为 None，不算回环。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: 只要出现任一转发头，就说明请求是经代理进来的——不再享有回环例外。
FORWARDED_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")

#: 不需要任何身份即可调用的 REST 路径（相对 api_v1_prefix）。
PUBLIC_REST_PATHS = frozenset({"/devices/pair"})

#: 声明成 OpenAPI 的 securityScheme 是为了 /docs 上有 Authorize 按钮：
#: 公网地址的 /docs 不带令牌调任何接口都是 401，没有这个按钮就没法在浏览器里自测。
#: `auto_error=False` 把"没带令牌"交给下面的依赖统一判定，以便保留回环例外。
bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Flux 访问令牌（服务端 FLUX_AUTH_TOKEN / 设备令牌 / 账号云 access token）",
)

#: 账号云 access token 的 payload 类型标记（见云服务的 security.py）。
_CLOUD_ACCESS_TYP = "access"

#: 设备令牌前缀（配对签发）。
DEVICE_TOKEN_PREFIX = "fbd_"

#: 可视为管理员的 principal 类型：cloud 是已登录的人，local 是同机直连。
_ADMIN_KINDS = frozenset({"admin", "local", "cloud"})


def _has_forwarded_headers(request: Request) -> bool:
    return any(request.headers.get(name) for name in FORWARDED_HEADERS)


def is_trusted_local(request: Request) -> bool:
    """同机直连（回环 + 无转发头）。"""
    client = request.client
    if client is None or client.host not in LOOPBACK_HOSTS:
        return False
    return not _has_forwarded_headers(request)


def is_cloud_access_token(settings: Settings, token: str | None) -> bool:
    """账号云签发的 access token？本地验签，不每次回源。"""
    secret = (settings.cloud_secret_key or "").strip()
    if not secret or not token:
        return False
    payload = verify_hs256(token, secret)
    return payload is not None and payload.get("typ") == _CLOUD_ACCESS_TYP


def _is_public_rest_path(request: Request, settings: Settings) -> bool:
    prefix = settings.api_v1_prefix.rstrip("/")
    path = request.url.path.rstrip("/")
    return any(path == f"{prefix}{item}" for item in PUBLIC_REST_PATHS)


def _set_principal(request: Request, kind: str, **extra: Any) -> dict[str, Any]:
    principal = {"kind": kind, **extra}
    request.state.principal = principal
    return principal


async def require_rest_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
) -> None:
    """`/api/v1` 的统一鉴权依赖（未配置令牌时的兜底策略见模块文档）。"""
    container = get_container(request)
    settings = container.settings
    # 配对兑换是唯一的免鉴权路径：手机此时还没有任何令牌。
    if _is_public_rest_path(request, settings):
        return

    provided = (
        credentials.credentials
        if credentials is not None
        else request.cookies.get("flux_auth_token")
    )
    # 第 0 条：账号云登录态。放在最前，让"登录后可用的接口"不依赖 FLUX_AUTH_TOKEN 是否配置。
    if is_cloud_access_token(settings, provided):
        _set_principal(request, "cloud")
        return

    expected = (settings.auth_token or "").strip()
    # 第 1 条：管理员令牌。与具体来源无关，常量时间比对。
    if expected and provided and hmac.compare_digest(provided, expected):
        _set_principal(request, "admin")
        return

    # 第 2 条：设备令牌。前缀命中后必须验真；无效不能被"开放模式"或回环例外兜住。
    if provided and provided.startswith(DEVICE_TOKEN_PREFIX):
        device = await container.devices.resolve_token(provided)
        if device is None:
            raise AuthenticationError("缺少或无效的访问令牌")
        _set_principal(request, "device", device_id=str(device.id), device_name=device.name)
        return

    if not expected:
        # None = 旧 flux.main 行为（开放）；True = 显式开放；False = Bot 节点的 fail-closed。
        if settings.allow_no_auth is not False:
            _set_principal(request, "local")
            return
        if is_trusted_local(request):
            _set_principal(request, "local")
            return
        raise AuthenticationError(
            "未配置 FLUX_AUTH_TOKEN：Bot 节点只接受本机回环直连，其他请求一律拒绝。"
            "请在 .env 设置 FLUX_AUTH_TOKEN；若部署在完全可信的内网，"
            "可显式设置 FLUX_ALLOW_NO_AUTH=true 启用旧的无鉴权模式。"
        )
    if is_trusted_local(request):
        _set_principal(request, "local")
        return
    # 常量时间比对：不因"前缀对上了"而早退，不给计时侧信道。
    if not provided or not hmac.compare_digest(provided, expected):
        raise AuthenticationError("缺少或无效的访问令牌")


async def require_admin(
    request: Request,
    _: None = Depends(require_rest_auth),
) -> dict[str, Any]:
    """管理员专用路由：设备令牌不能管理设备，也不能签发配对码。"""
    principal = getattr(request.state, "principal", None) or {}
    if principal.get("kind") not in _ADMIN_KINDS:
        raise PermissionDeniedError(
            "该操作需要管理员权限（设备令牌不可用）",
            details={"principal": principal.get("kind")},
        )
    return principal


async def require_device(
    request: Request,
    _: None = Depends(require_rest_auth),
) -> dict[str, Any]:
    """设备自身才能调用的路由（如手机主动登出）。"""
    principal = getattr(request.state, "principal", None) or {}
    if principal.get("kind") != "device" or not principal.get("device_id"):
        raise PermissionDeniedError(
            "该操作需要设备令牌",
            details={"principal": principal.get("kind")},
        )
    return principal
