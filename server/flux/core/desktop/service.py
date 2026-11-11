"""Bot 桌面入口参数：公开隧道地址 + 服务端私有凭据。

App 进「Bot 桌面」前向 `/api/v1/desktop` 要一份配置。数据分两层来源：

- 隧道地址（gateway / bridge）：会随 Cloudflare 快速隧道重启变化，放在公开的
  `desktop.json` 里，由服务端代读并缓存——换地址不需要 App 发版。
- 凭据（token / bridge_key）：只从进程环境变量取，**不下发到客户端源码**。
  仓库公开、APK 被反编译都不会直接暴露，轮换也只需改服务端环境变量。

读不到公开配置时降级到环境变量里的兜底地址；凭据缺失则 `configured=false`，
由 App 提示"桌面未配置"，绝不返回半个连接参数让客户端去猜。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from flux.config import Settings
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

    async def config(self) -> dict[str, Any]:
        public = await self._public()
        gateway = (public.get("gateway") or self._settings.desktop_gateway or "").strip()
        raw_bridge = public.get("bridge") or self._settings.desktop_bridge_url or ""
        bridge = raw_bridge.strip().rstrip("/")
        token = (self._settings.desktop_token or "").strip()
        bridge_key = (self._settings.desktop_bridge_key or "").strip()
        return {
            "configured": bool(gateway and bridge and token and bridge_key),
            "gateway": gateway,
            "bridge": bridge,
            "token": token,
            "bridge_key": bridge_key,
            "config_url": self._settings.desktop_config_url,
            "tunnel_updated_at": public.get("updated", ""),
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
            logger.warning("读取桌面隧道配置失败，回退到环境变量: %s", exc)
            return {}
        if not isinstance(payload, dict):
            return {}
        self._cached = {key: value for key, value in payload.items() if isinstance(value, str)}
        self._cached_at = now
        return self._cached


__all__ = ["DesktopService"]
