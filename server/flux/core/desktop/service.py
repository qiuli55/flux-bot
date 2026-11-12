"""Bot 桌面入口参数：公开隧道地址 + 服务端私有凭据。

App 进「Bot 桌面」前向 `/api/v1/desktop` 要一份配置。数据分两层来源：

- 隧道地址（gateway / bridge）：会随 Cloudflare 快速隧道重启变化，放在公开的
  `desktop.json` 里，由服务端代读并缓存——换地址不需要 App 发版。
- 凭据（token / bridge_key）：只从进程环境变量取，**不下发到客户端源码**。
  仓库公开、APK 被反编译都不会直接暴露，轮换也只需改服务端环境变量。

读不到公开配置时降级到环境变量里的兜底地址；地址或凭据不全则
`configured=false`，由 App 提示"桌面未配置"，绝不返回半个连接参数让客户端去猜。
字段别名（`desktop_host` / `desktop_gateway` 等）的解析规则见
`flux.core.desktop.resolve`，会话票据接口复用同一份解析结果。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from flux.config import Settings
from flux.core.desktop.resolve import DesktopEndpoints, resolve_desktop_endpoints
from flux.logging import get_logger

logger = get_logger(__name__)

#: 公开配置的缓存时长：隧道地址不会秒级变化，60 秒足够且能挡住 App 侧的频繁进入。
_CACHE_SECONDS = 60.0


class DesktopService:
    """组装桌面入口参数；不落库、不持有长期连接。"""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        # 测试注入 MockTransport；生产每次临时建连，避免长期持有连接池
        self._client = client
        self._cached_at = 0.0
        self._cached: dict[str, str] = {}

    async def resolve(self) -> DesktopEndpoints:
        """返回 ``desktop.json`` 优先、env 别名兜底的连接事实（凭据只走 env）。"""
        public = await self._public()
        return resolve_desktop_endpoints(self._settings, public)

    async def config(self) -> dict[str, Any]:
        """GET /api/v1/desktop 的响应体。

        未配置时只返回 `configured=false` 与元数据，所有连接参数留空；配置完整时
        按 `desktop_send_gateway_token` 决定是否下发长期凭据。票据模式下长期凭据
        置为 null，客户端改走 `POST /bots/{id}/desktop/session`。
        """
        endpoints = await self.resolve()
        if not endpoints.configured(self._settings.desktop_ticket_secret):
            logger.warning(
                "desktop.config.unconfigured gateway=%s bridge=%s long_lived_credentials=%s "
                "ticket_secret=%s",
                bool(endpoints.gateway),
                bool(endpoints.bridge),
                endpoints.has_long_lived_credentials,
                bool((self._settings.desktop_ticket_secret or "").strip()),
            )
            return {
                "configured": False,
                "gateway": "",
                "bridge": "",
                "token": None,
                "bridge_key": None,
                "config_url": endpoints.config_url,
                "tunnel_updated_at": endpoints.tunnel_updated_at,
            }
        send_credentials = (
            self._settings.desktop_send_gateway_token and endpoints.has_long_lived_credentials
        )
        return {
            "configured": True,
            "gateway": endpoints.gateway,
            "bridge": endpoints.bridge,
            "token": endpoints.gateway_token if send_credentials else None,
            "bridge_key": endpoints.bridge_key if send_credentials else None,
            "config_url": endpoints.config_url,
            "tunnel_updated_at": endpoints.tunnel_updated_at,
        }

    async def _public(self) -> dict[str, str]:
        """读公开的 `desktop.json`：只取隧道地址，失败时返回空字典走兜底。"""
        url = (self._settings.desktop_config_url or "").strip()
        if not url:
            return {}
        now = time.monotonic()
        if self._cached and now - self._cached_at < _CACHE_SECONDS:
            return self._cached
        try:
            if self._client is not None:
                response = await self._client.get(url)
            else:
                async with httpx.AsyncClient(
                    timeout=self._settings.desktop_timeout_seconds,
                    follow_redirects=True,
                ) as client:
                    response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # 公开配置只是"地址的快捷方式"，读不到不影响鉴权与其它接口。
            logger.warning("desktop.config.public.fallback url=%s error=%s", url, exc)
            return {}
        if not isinstance(payload, dict):
            logger.warning("desktop.config.public.fallback url=%s error=payload-not-object", url)
            return {}
        fetched = {key: value for key, value in payload.items() if isinstance(value, str)}
        if not fetched:
            logger.warning("desktop.config.public.fallback url=%s error=payload-empty", url)
            return {}
        self._cached = fetched
        self._cached_at = now
        logger.info(
            "desktop.config.public.fetched url=%s gateway=%s bridge=%s",
            url,
            bool(fetched.get("gateway")),
            bool(fetched.get("bridge")),
        )
        return self._cached


__all__ = ["DesktopService"]
