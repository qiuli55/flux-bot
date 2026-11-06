"""ChatGPT 账号（Codex 订阅通道）的登录态管理。

背景：Flux 每次 codex Run 都用隔离的 `CODEX_HOME`（`<FLUX_DSH_HOME>/runs/<run_id>/codex-home`），
里面只有 Flux 生成的 config.toml。隔离目录没有 `auth.json`，codex 在 Run 里就判定「未登录」，
ChatGPT 订阅额度因此用不上。本模块把账号登录态收在 Flux 自己的账号目录
（默认 `~/.flux/codex-account`），Run 时只复制 `auth.json` 过去，不碰用户的全局 `~/.codex`。

登录走官方 `codex login --device-auth`（设备码授权），Flux 只解析它打印的授权链接与设备码，
不在本地保存任何 token 副本；`status()` 也只从 id_token 里读展示字段（邮箱 / 套餐 / 过期时间），
绝不下发 access / refresh / id token 本身。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flux.config import Settings
from flux.core.agent_runtime.platforms import executable_argv, popen_kwargs, secure_file
from flux.core.billing.plans import PLAN_CODEX
from flux.errors import ConfigurationError
from flux.logging import get_logger

logger = get_logger(__name__)

#: 设备码形如 XXXX-XXXXX（OpenAI 设备授权页的格式）
_DEVICE_CODE_RE = re.compile(r"\b([A-Z0-9]{4,8}-[A-Z0-9]{4,8})\b")
_URL_RE = re.compile(r"https://[^\s\"'<>]+")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

#: 登录进程最多等这么久才拿到授权链接/设备码（网络正常时应在秒级返回）
_PROMPT_TIMEOUT_SECONDS = 25.0
#: 一次设备码登录的最长寿命：OpenAI 侧 15 分钟过期，这里留余量兜底
_LOGIN_MAX_SECONDS = 20 * 60.0
#: 已结束的登录会话在内存里保留多久（够前端轮询到终态即可）
_LOGIN_KEEP_SECONDS = 10 * 60.0
#: auth.json 上限：正常只有几 KB，防止异常文件被整段读进内存
_MAX_AUTH_BYTES = 1 << 20

_PENDING_STATUSES = ("starting", "pending")


def codex_account_home(settings: Settings) -> Path:
    """账号登录态目录（`FLUX_CODEX_ACCOUNT_HOME`）。"""
    raw = (settings.codex_account_home or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".flux" / "codex-account"


def parse_device_auth_output(text: str) -> tuple[str, str]:
    """从 `codex login --device-auth` 的输出里取 (授权链接, 设备码)；取不到返回空串。

    codex 报错时也会打印 `https://auth.openai.com/api/accounts/deviceauth/...` 这类接口地址，
    所以优先取带 device 的授权页，避免把用户导到 API 端点。
    """
    body = text or ""
    candidates: list[str] = []
    for match in _URL_RE.finditer(body):
        candidate = match.group(0).rstrip(".,;:)]}>")
        if "openai.com" in candidate or "chatgpt.com" in candidate:
            candidates.append(candidate)
    url = next((item for item in candidates if "/codex/device" in item), "")
    if not url:
        url = next((item for item in candidates if item.rstrip("/").endswith("/device")), "")
    if not url and candidates:
        url = candidates[0]
    code_match = _DEVICE_CODE_RE.search(body)
    return url, code_match.group(1) if code_match else ""


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """读 JWT 的 payload（**不验签**）：只用于展示邮箱 / 套餐 / 过期时间。"""
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding)
        claims = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def codex_proxy_env(settings: Settings) -> dict[str, str]:
    """代理环境变量；未配置返回空字典（继承 Flux 进程环境）。"""
    proxy = (settings.codex_proxy or "").strip()
    if not proxy:
        return {}
    return {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "ALL_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "all_proxy": proxy,
    }


def sync_account_auth(source_home: Path | str, target_home: Path | str) -> bool:
    """把账号目录的 `auth.json` 以 0600 原子复制进目标 CODEX_HOME。

    源不存在时返回 False（调用方据此决定是否回退或报错），不创建空文件。
    """
    source = Path(source_home).expanduser() / "auth.json"
    if not source.is_file():
        return False
    try:
        data = source.read_bytes()
    except OSError:
        logger.warning("codex 账号登录态读取失败 path=%s", source)
        return False
    destination_home = Path(target_home).expanduser()
    destination = destination_home / "auth.json"
    tmp = destination_home / ".auth.json.tmp"
    try:
        destination_home.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        secure_file(tmp)
        os.replace(tmp, destination)
        secure_file(destination)
    except OSError:
        logger.warning("codex 账号登录态注入失败 target=%s", destination_home)
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False
    return True


@dataclass
class _LoginSession:
    login_id: str
    process: Any
    created_at: float = field(default_factory=time.monotonic)
    status: str = "starting"
    url: str = ""
    code: str = ""
    message: str = ""
    output: str = ""
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    pump: asyncio.Task[None] | None = None
    watchdog: asyncio.Task[None] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "login_id": self.login_id,
            "status": self.status,
            "url": self.url,
            "code": self.code,
            "message": self.message,
            "expires_in": max(0, int(_LOGIN_MAX_SECONDS - (time.monotonic() - self.created_at))),
        }


class CodexAccountService:
    """账号登录态、设备码登录与退出。登录会话只存内存，进程重启即失效。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._logins: dict[str, _LoginSession] = {}
        #: 计费服务（容器装配后注入）：登录成功立刻补一次 Codex 额度快照
        self._billing: Any = None
        self._billing_jobs: set[asyncio.Task[None]] = set()

    def set_billing(self, billing: Any) -> None:
        """注入计费服务；避免 codex_account ↔ billing.sources 的循环导入，用 Any。"""
        self._billing = billing

    def _schedule_billing_snapshot(self) -> None:
        """登录成功后异步采集一次 ChatGPT 额度，失败不影响登录结果。"""
        billing = self._billing
        if billing is None:
            return

        async def snapshot() -> None:
            try:
                await billing.refresh(PLAN_CODEX)
            except Exception:  # noqa: BLE001 - 采集失败不改变登录态
                logger.warning("登录后采集 Codex 额度失败", exc_info=True)

        try:
            task = asyncio.create_task(snapshot())
        except RuntimeError:  # 没有运行中的事件循环（同步调用场景）时跳过
            logger.warning("没有事件循环，跳过登录后的 Codex 额度采集")
            return
        self._billing_jobs.add(task)
        task.add_done_callback(self._billing_jobs.discard)

    # --- 路径 ---

    @property
    def home(self) -> Path:
        return codex_account_home(self._settings)

    @property
    def auth_path(self) -> Path:
        return self.home / "auth.json"

    def binary_path(self) -> str:
        """codex 可执行文件：`FLUX_CODEX_CLI_BINARY` 覆盖优先，否则查 PATH。"""
        override = (os.environ.get("FLUX_CODEX_CLI_BINARY") or "").strip()
        if override:
            return override
        return shutil.which("codex") or "codex"

    # --- 状态 ---

    def status(self) -> dict[str, Any]:
        binary = self.binary_path()
        payload: dict[str, Any] = {
            "logged_in": False,
            "auth_mode": None,
            "email": None,
            "plan": None,
            "account_id": None,
            "expires_at": None,
            "home": str(self.home),
            "binary": binary,
            "binary_found": shutil.which(binary) is not None,
            "pending_login": None,
        }
        auth = self._read_auth()
        if auth is None:
            payload["pending_login"] = self._latest_pending()
            return payload
        tokens = auth.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else {}
        id_token = str(tokens.get("id_token") or "")
        claims = decode_jwt_claims(id_token)
        auth_claims = claims.get("https://api.openai.com/auth")
        auth_claims = auth_claims if isinstance(auth_claims, dict) else {}
        profile = claims.get("https://api.openai.com/profile")
        profile = profile if isinstance(profile, dict) else {}
        email = str(profile.get("email") or claims.get("email") or "").strip()
        api_key = str(auth.get("OPENAI_API_KEY") or "").strip()
        payload.update(
            {
                "logged_in": True,
                "auth_mode": "chatgpt" if id_token else ("api_key" if api_key else "unknown"),
                "email": email or None,
                "plan": str(auth_claims.get("chatgpt_plan_type") or "").strip() or None,
                "account_id": str(
                    auth_claims.get("chatgpt_account_id") or tokens.get("account_id") or ""
                ).strip()
                or None,
                "expires_at": _iso_or_none(claims.get("exp")),
            }
        )
        payload["pending_login"] = self._latest_pending()
        return payload

    def _read_auth(self) -> dict[str, Any] | None:
        path = self.auth_path
        try:
            if not path.is_file() or path.stat().st_size > _MAX_AUTH_BYTES:
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("codex 账号登录态不可读 path=%s", path)
            return None
        return data if isinstance(data, dict) else None

    # --- 登录 ---

    async def start_login(self) -> dict[str, Any]:
        """启动设备码登录：等到授权链接与设备码出现，或进程提前退出。"""
        self._reap()
        binary = self.binary_path()
        if shutil.which(binary) is None:
            raise ConfigurationError(
                "没有找到 codex 命令行，无法登录 ChatGPT 账号",
                details={
                    "binary": binary,
                    "hint": "安装 @openai/codex 或设置 FLUX_CODEX_CLI_BINARY",
                },
            )
        # 已有进行中的登录：直接复用，避免开出两条设备码让用户困惑
        existing = self._latest_pending()
        if existing is not None:
            return existing

        home = self.home
        try:
            home.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(home, 0o700)
            process = await asyncio.create_subprocess_exec(
                *executable_argv((binary, "login", "--device-auth")),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=self._login_env(),
                cwd=str(home),
                **popen_kwargs(),
            )
        except OSError as exc:
            raise ConfigurationError(
                f"codex 登录进程启动失败：{exc}",
                details={"binary": binary},
            ) from exc

        session = _LoginSession(login_id=uuid.uuid4().hex[:12], process=process)
        self._logins[session.login_id] = session
        logger.info(
            "codex 账号登录开始 login=%s home=%s pid=%s",
            session.login_id,
            home,
            getattr(process, "pid", None),
        )
        session.pump = asyncio.create_task(self._pump(session))
        session.watchdog = asyncio.create_task(self._enforce_deadline(session))
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(session.ready.wait(), timeout=_PROMPT_TIMEOUT_SECONDS)
        if session.status in _PENDING_STATUSES and not session.message:
            session.message = "等待 codex 输出授权信息…"
        return session.public()

    def login_state(self, login_id: str) -> dict[str, Any] | None:
        session = self._logins.get((login_id or "").strip())
        if session is None:
            return None
        # 进程已退出但 pump 还没收尾：以 auth.json 的存在为准补一次终态
        if session.status in _PENDING_STATUSES and session.process.returncode is not None:
            self._settle(session, session.process.returncode)
        return session.public()

    def _latest_pending(self) -> dict[str, Any] | None:
        pending = [s for s in self._logins.values() if s.status in _PENDING_STATUSES]
        if not pending:
            return None
        return max(pending, key=lambda item: item.created_at).public()

    def _login_env(self) -> dict[str, str]:
        env = {**os.environ, "CODEX_HOME": str(self.home), "TERM": "dumb", "NO_COLOR": "1"}
        # 设备码流程不需要本地浏览器；禁掉防止 CLI 调起桌面浏览器
        env.setdefault("BROWSER", "/bin/false")
        env.update(codex_proxy_env(self._settings))
        return env

    async def _pump(self, session: _LoginSession) -> None:
        """读登录进程输出、解析授权信息，并在进程退出后落终态。"""
        process = session.process
        try:
            stream = process.stdout
            while stream is not None:
                raw = await stream.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace")
                session.output = (session.output + _ANSI_RE.sub("", line))[-4000:]
                url, code = parse_device_auth_output(session.output)
                if url:
                    session.url = url
                if code:
                    session.code = code
                if session.url and session.code and session.status == "starting":
                    session.status = "pending"
                    session.message = ""
                    session.ready.set()
            returncode = await process.wait()
            self._settle(session, returncode)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 读流出错也要给会话落终态，不能让前端一直轮询
            logger.exception("codex 账号登录输出读取失败 login=%s", session.login_id)
            session.status = "failed"
            session.message = "登录进程异常退出"
        finally:
            if not session.ready.is_set():
                session.ready.set()
            if session.watchdog is not None:
                session.watchdog.cancel()

    def _settle(self, session: _LoginSession, returncode: int | None) -> None:
        if session.status not in _PENDING_STATUSES:
            return
        if returncode == 0 and self.auth_path.is_file():
            session.status = "success"
            session.message = ""
            logger.info("codex 账号登录成功 login=%s", session.login_id)
            # 登录即采集一次套餐额度，账单系统立刻有 ChatGPT 的官方口径
            self._schedule_billing_snapshot()
            return
        session.status = "failed"
        session.message = _tail(session.output) or f"codex 登录进程退出码 {returncode}"
        logger.warning(
            "codex 账号登录失败 login=%s rc=%s tail=%s",
            session.login_id,
            returncode,
            _tail(session.output),
        )

    async def _enforce_deadline(self, session: _LoginSession) -> None:
        """兜底：OpenAI 侧设备码 15 分钟过期，这里 20 分钟仍未退出就杀掉。"""
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(_LOGIN_MAX_SECONDS)
            if session.status not in _PENDING_STATUSES:
                return
            logger.warning("codex 账号登录超时 login=%s", session.login_id)
            session.message = "登录超时（20 分钟），请重新发起"
            with contextlib.suppress(ProcessLookupError, OSError):
                session.process.terminate()
            with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError, OSError):
                await asyncio.wait_for(session.process.wait(), timeout=5)
            with contextlib.suppress(ProcessLookupError, OSError):
                session.process.kill()
            self._settle(session, -1)

    def _reap(self) -> None:
        now = time.monotonic()
        for login_id, session in list(self._logins.items()):
            finished = session.status not in _PENDING_STATUSES
            if finished and now - session.created_at > _LOGIN_KEEP_SECONDS:
                self._logins.pop(login_id, None)

    # --- 退出 ---

    async def logout(self) -> dict[str, Any]:
        """删除账号目录里的 auth.json；进行中的登录一并终止。"""
        for session in list(self._logins.values()):
            if session.status in _PENDING_STATUSES:
                with contextlib.suppress(ProcessLookupError, OSError):
                    session.process.terminate()
                session.status = "cancelled"
                session.message = "已退出登录"
        removed = False
        path = self.auth_path
        try:
            if path.exists():
                path.unlink()
                removed = True
        except OSError as exc:
            raise ConfigurationError(
                "删除 ChatGPT 登录态失败",
                details={"path": str(path)},
            ) from exc
        logger.info("codex 账号已退出 removed=%s path=%s", removed, path)
        return {"removed": removed, "status": self.status()}


def _iso_or_none(value: Any) -> str | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _tail(text: str, limit: int = 400) -> str:
    """失败原因的短摘要：去掉空行，取最后几行，绝不包含 token（codex 登录输出不含）。"""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return " | ".join(lines[-4:])[-limit:]


__all__ = [
    "CodexAccountService",
    "codex_account_home",
    "codex_proxy_env",
    "decode_jwt_claims",
    "parse_device_auth_output",
    "sync_account_auth",
]
