"""Supervisor 的出站通道：把报告投递回来源平台（当前只有飞书 + 空实现）。

通道只做一件事：把一段纯文本发到平台上的某个会话。它不认识工单、不读数据库，
因此可以独立替换/测试。凭据只从环境变量读取（`FLUX_SUPERVISOR_FEISHU_*`），
绝不落库、不进日志。

没有配置凭据时装配 `NullChannel`：投递被记为"已处理"，不会让 outbox 反复重试
到 dead —— 本地开发与测试环境本来就没有可用的平台出口。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Protocol

import httpx

from flux.logging import get_logger

logger = get_logger(__name__)

__all__ = ["FeishuChannel", "NullChannel", "SupervisorChannel", "WechatChannel"]

#: 平台单条文本上限的安全值（飞书文本消息上限远大于此，留足余量）
MAX_TEXT_CHARS = 4000


class SupervisorChannel(Protocol):
    """出站通道协议：按平台会话 id 发一段纯文本。"""

    name: str

    async def send_text(self, external_id: str, text: str) -> None:
        """发送文本；失败必须抛异常，交给 outbox 退避重试。"""
        ...

    async def aclose(self) -> None:
        """释放连接池。"""
        ...


class NullChannel:
    """没有配置凭据时的空实现：只记日志，不产生网络请求。"""

    def __init__(self, name: str) -> None:
        self.name = name

    async def send_text(self, external_id: str, text: str) -> None:
        logger.info(
            "Supervisor 通道 %s 未配置凭据，跳过投递 external_id=%s chars=%s",
            self.name,
            external_id,
            len(text),
        )

    async def aclose(self) -> None:
        return None


class FeishuChannel:
    """飞书自建应用：tenant_access_token + 发文本消息。"""

    name = "feishu"

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        api_base: str = "https://open.feishu.cn",
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._api_base = api_base.rstrip("/")
        self._timeout = max(1.0, float(timeout_seconds))
        self._client = client
        self._owns_client = client is None
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._lock = asyncio.Lock()

    async def send_text(self, external_id: str, text: str) -> None:
        token = await self._tenant_token()
        response = await self._request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": external_id,
                "msg_type": "text",
                "content": json.dumps({"text": (text or "")[:MAX_TEXT_CHARS]}, ensure_ascii=False),
            },
        )
        _raise_for_feishu(response, "发送飞书消息")

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    # --- 内部 ---

    async def _tenant_token(self) -> str:
        """缓存 tenant_access_token，提前 60 秒过期，避免边界上刚好失效。"""
        if self._token is not None and time.monotonic() < self._token_expires_at:
            return self._token
        async with self._lock:
            if self._token is not None and time.monotonic() < self._token_expires_at:
                return self._token
            response = await self._request(
                "POST",
                "/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
            data = _payload(response, "获取飞书 tenant_access_token")
            token = str(data.get("tenant_access_token") or "")
            if not token:
                raise RuntimeError("飞书 tenant_access_token 响应缺少令牌")
            expire = int(data.get("expire") or 7200)
            self._token = token
            self._token_expires_at = time.monotonic() + max(60, expire - 60)
            return token

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
            self._client = client
            self._owns_client = True
        return await client.request(method, f"{self._api_base}{path}", **kwargs)


class WechatChannel:
    """微信主动出站：把文本回推给 OpenClaw 网关（入站被动回复不走这里）。"""

    name = "wechat"

    def __init__(
        self,
        *,
        send_url: str,
        token: str,
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._send_url = send_url
        self._token = token
        self._timeout = max(1.0, float(timeout_seconds))
        self._client = client
        self._owns_client = client is None

    async def send_text(self, external_id: str, text: str) -> None:
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
            self._client = client
            self._owns_client = True
        response = await client.post(
            self._send_url,
            headers={"Authorization": f"Bearer {self._token}"},
            json={"session_key": external_id, "text": (text or "")[:MAX_TEXT_CHARS]},
        )
        if response.status_code >= 400:
            detail = response.text[:300]
            raise RuntimeError(f"发送微信消息失败：HTTP {response.status_code} {detail}")

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None


def _payload(response: httpx.Response, label: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{label}失败：响应不是 JSON（HTTP {response.status_code}）") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{label}失败：响应结构异常（HTTP {response.status_code}）")
    code = data.get("code")
    if response.status_code >= 400 or code not in (0, None):
        message = data.get("msg") or data.get("message") or ""
        raise RuntimeError(f"{label}失败：HTTP {response.status_code} code={code} {message}"[:500])
    return data


def _raise_for_feishu(response: httpx.Response, label: str) -> None:
    _payload(response, label)
