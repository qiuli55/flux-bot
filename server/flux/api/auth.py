"""REST 面的单用户令牌鉴权（公网暴露前置）。

为什么需要：Flux 的 REST 面有 `terminal.execute` 等价能力（终端会话可执行任意命令）、
`workspace.apply`（落盘）、`git.commit`——把 `/api/v1` 裸暴露到公网等于把服务器 shell 交出去。
MCP 面本来就有 Agent 令牌，REST 面此前完全没有身份校验（只在本机用）。

规则（任一通过即放行）：

0. 带的是**账号云**（B 机 `flux.qiuli55.top`，独立私有仓库）签发的 access token
   （`typ=access`），且与云共享的 HS256 密钥本地验签通过 → 放行。这就是 Flux 本体的
   登录闭环：令牌由唯一的账号权威签发，本机只验签、不每次回源；
1. 请求带 `Authorization: Bearer <token>` 且与 `FLUX_AUTH_TOKEN` **常量时间**比对通过 → 放行；
2. 例外：来源是回环地址**且**没有任何转发头（`X-Forwarded-For` / `X-Real-IP` / `Forwarded`）
   → 放行。这条例外的用途是保护"同机直连"：桌面端（Electron 主进程反代）、
   `make run` + 本机浏览器、本机 CLI 探活都不必带令牌；
3. 其余一律 401（`unauthenticated`，不区分"没带 / 带错 / 格式不对"——区分等于给探测信号）。

`FLUX_AUTH_TOKEN` 留空 = 关掉第 1 条，保持本地开发零配置；账号云令牌（第 0 条）只认
`FLUX_CLOUD_SECRET_KEY` 配上的那一个密钥。

第 2 条是**反向代理配置的硬约束**：反代必须转发 `X-Forwarded-For`（见 `deploy/nginx-flux.conf`
里的 `proxy_set_header`），否则经代理进来的公网请求会以"回环 + 无转发头"的形态被误判为本地直连。
部署脚本用 `curl` 不带令牌复验 401，正是为了盯住这条约束。
"""

from __future__ import annotations

import hmac

from fastapi import Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from flux.api.deps import get_container
from flux.cloud.session import verify_hs256
from flux.config import Settings
from flux.errors import AuthenticationError

#: 视为"同机直连"的来源地址。Unix socket 下 ASGI 的 client 为 None，不算回环。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: 只要出现任一转发头，就说明请求是经代理进来的——不再享有回环例外。
FORWARDED_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")

#: 声明成 OpenAPI 的 securityScheme 是为了 /docs 上有 Authorize 按钮：
#: 公网地址的 /docs 不带令牌调任何接口都是 401，没有这个按钮就没法在浏览器里自测。
#: `auto_error=False` 把"没带令牌"交给下面的依赖统一判定，以便保留回环例外。
bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Flux 访问令牌（服务端 FLUX_AUTH_TOKEN 或账号云签发的 access token）",
)

#: 账号云 access token 的 payload 类型标记（见云服务的 security.py）。
_CLOUD_ACCESS_TYP = "access"


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


def require_rest_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
) -> None:
    """`/api/v1` 的统一鉴权依赖。未配置 `FLUX_AUTH_TOKEN` 时整体跳过。"""
    settings = get_container(request).settings
    provided = (
        credentials.credentials
        if credentials is not None
        else request.cookies.get("flux_auth_token")
    )
    # 第 0 条：账号云登录态。放在最前，让"登录后可用的接口"不依赖 FLUX_AUTH_TOKEN 是否配置。
    if is_cloud_access_token(settings, provided):
        return

    expected = (settings.auth_token or "").strip()
    if not expected:
        return
    if is_trusted_local(request):
        return
    # 常量时间比对：不因"前缀对上了"而早退，不给计时侧信道。
    if not provided or not hmac.compare_digest(provided, expected):
        raise AuthenticationError("缺少或无效的访问令牌")
