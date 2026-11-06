"""与 Flux 账号云（B 机 `flux.qiuli55.top`）的会话交互。

设计要点：

- **本地验签**：云端用共享密钥签发 HS256 access token，本机用标准库校验签名与有效期，
  不每次回源账号云——公网抖动不该让 Flux 本体一起不可用；
- **不复制用户表**：账号的权威全在云端，本机只存一份会话（access / refresh token）到
  `~/.flux/cloud.json`（0600）；
- 只依赖标准库 + `httpx`，不引入 PyJWT（`backend/requirements.txt` 里没有它）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from flux.logging import get_logger

logger = get_logger(__name__)

#: 云端接口的超时（秒）。上传大日志时可以更宽，但这里保持统一、简单。
DEFAULT_TIMEOUT = 30.0

#: 会话到期前多久开始续期（秒），避免"刚好在手边过期"。
_EXPIRY_SKEW_SECONDS = 30.0

_ALG = "HS256"
_ACCESS_TYP = "access"


class CloudError(RuntimeError):
    """账号云交互失败（网络、状态码或响应格式）。"""


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def verify_hs256(token: str, secret: str) -> dict[str, Any] | None:
    """校验 HS256 签名的 JWT：签名与 `exp` 都通过才返回 payload，否则 None。"""
    if not token or not secret:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header_segment, payload_segment, signature_segment = parts
    try:
        header = json.loads(_b64url_decode(header_segment))
        payload = json.loads(_b64url_decode(payload_segment))
        signature = _b64url_decode(signature_segment)
    except (ValueError, TypeError):
        return None
    if not isinstance(header, dict) or not isinstance(payload, dict):
        return None
    if header.get("alg") != _ALG:
        return None
    signing_input = f"{header_segment}.{payload_segment}".encode()
    expected = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, signature):
        return None
    exp = payload.get("exp")
    if isinstance(exp, (int, float)) and time.time() >= float(exp):
        return None
    return payload


@dataclass
class CloudSession:
    """本机保存的账号云会话。"""

    access_token: str
    refresh_token: str
    expires_at: float
    user_id: str = ""
    username: str = ""

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at - _EXPIRY_SKEW_SECONDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "user_id": self.user_id,
            "username": self.username,
        }

    @classmethod
    def from_dict(cls, data: Any) -> CloudSession | None:
        if not isinstance(data, dict):
            return None
        access = str(data.get("access_token") or "")
        refresh = str(data.get("refresh_token") or "")
        expires_at = data.get("expires_at")
        if not access or not refresh or not isinstance(expires_at, (int, float)):
            return None
        return cls(
            access_token=access,
            refresh_token=refresh,
            expires_at=float(expires_at),
            user_id=str(data.get("user_id") or ""),
            username=str(data.get("username") or ""),
        )


def load_session(path: Path) -> CloudSession | None:
    """读取本机会话；不存在或损坏都返回 None（不抛异常）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("读取账号云会话失败 %s: %s", path, exc)
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("账号云会话文件不是合法 JSON：%s", path)
        return None
    return CloudSession.from_dict(data)


def save_session(path: Path, session: CloudSession) -> None:
    """写入本机会话，权限 0600（先写临时文件再替换，避免半截文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(session.to_dict(), ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def clear_session(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:  # pragma: no cover - 权限异常等，记账即可
        logger.warning("删除账号云会话失败 %s: %s", path, exc)


class CloudClient:
    """账号云的 HTTP 客户端（只用得到三个接口）。"""

    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout

    async def exchange_code(self, code: str) -> CloudSession:
        """用一次性登录码换取令牌（登录回跳闭环的最后一步）。"""
        payload = await self._post("/api/auth/exchange", {"code": code})
        return self._session_from_tokens(payload)

    async def refresh(self, refresh_token: str) -> CloudSession:
        """用 refresh token 换新的 access token（服务端续期，不需要浏览器）。"""
        payload = await self._post("/api/auth/token", {"refresh_token": refresh_token})
        return self._session_from_tokens(payload)

    async def upload_log(
        self,
        session: CloudSession,
        *,
        path: Path,
        run_started_at: str = "",
        prev_run_started_at: str = "",
        client_version: str = "",
        os_info: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        """上传一份日志文件。"""
        headers = {"Authorization": f"Bearer {session.access_token}"}
        form = {
            "run_started_at": run_started_at,
            "prev_run_started_at": prev_run_started_at,
            "client_version": client_version,
            "os_info": os_info,
            "note": note,
        }
        files = {"file": (path.name, path.read_bytes(), "application/octet-stream")}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/api/logs/upload",
                headers=headers,
                data=form,
                files=files,
            )
        return self._json_or_raise(response)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.base_url}{path}", json=payload)
        return self._json_or_raise(response)

    @staticmethod
    def _json_or_raise(response: httpx.Response) -> dict[str, Any]:
        body = response.text
        if response.status_code >= 400:
            raise CloudError(f"账号云返回 {response.status_code}：{body[:200]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise CloudError("账号云返回了非 JSON 响应") from exc
        if not isinstance(payload, dict):
            raise CloudError("账号云响应格式不正确")
        return payload

    @staticmethod
    def _session_from_tokens(payload: dict[str, Any]) -> CloudSession:
        access = str(payload.get("access_token") or "")
        refresh = str(payload.get("refresh_token") or "")
        if not access or not refresh:
            raise CloudError("账号云未返回完整令牌")
        expires_in = payload.get("expires_in")
        ttl = float(expires_in) if isinstance(expires_in, (int, float)) else 1800.0
        user = payload.get("user")
        user = user if isinstance(user, dict) else {}
        return CloudSession(
            access_token=access,
            refresh_token=refresh,
            expires_at=time.time() + ttl,
            user_id=str(user.get("id") or ""),
            username=str(user.get("username") or ""),
        )
