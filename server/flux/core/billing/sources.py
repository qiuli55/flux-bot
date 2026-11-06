"""官方额度采集器。

两个事实来源（2026-10 实测）：
- **OpenCode Go**：`GET https://opencode.ai/zen/go/v1/usage`，Bearer 鉴权。
  必须带浏览器式 User-Agent，否则 Cloudflare 直接 1010 拒绝（urllib 默认 UA 就会中招）。
  返回 `usage.rolling / weekly / monthly` 的 `{status, percent, resetsAt}`。
- **火山 Agent Plan**：官方 OpenAPI 只认 AK/SK 签名（Bearer 实测 404），本机只有
  `arkcli` 登录态，因此做成「可配置命令 + 解析 JSON」：命令默认
  `arkcli usage plan --product agent-plan`，输出结构变化时落一条 error 快照，
  不影响账本与闸门（闸门会退化为"官方数据不可用"）。

Elective（自定义套餐）复用同一套能力：`SourceSpec(kind="cli")` 跑命令、`kind="http"`
请求一个额度端点。采集器只负责「拿到事实」，不写库、不判定闸门——那些在 service 里。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx

from flux.config import Settings
from flux.core.billing.plans import (
    PLAN_AGENT_PLAN,
    PLAN_CODEX,
    PLAN_OPENCODE_GO,
    WINDOW_DAILY,
    WINDOW_MONTHLY,
    WINDOW_ROLLING_5H,
    WINDOW_WEEKLY,
    PlanDefinition,
    SourceSpec,
    default_plans,
    official_window_key,
)
from flux.logging import get_logger

logger = get_logger(__name__)

#: Go 额度接口；Cloudflare 会按 UA 判定，缺 UA 返回 403/1010
OPENCODE_GO_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
#: 伪装成 opencode 自己的客户端（不要用 python-urllib 默认 UA）
OPENCODE_UA = "opencode/1.0 (flux-billing)"

#: arkcli 默认命令；输出格式由 CLI 决定，解析是尽力而为
DEFAULT_ARKCLI_CMD = "arkcli usage plan --product agent-plan"

#: Codex 订阅额度接口（2026-10 实测）：Bearer 用账号目录 auth.json 里的 access_token
CHATGPT_USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
#: 官方接口按客户端识别，缺 originator 会被拒
CHATGPT_UA = "codex-cli/0.157.1"
CHATGPT_ORIGINATOR = "codex_cli_rs"

#: 官方窗口时长（秒）→ 本地窗口键；Codex 用 primary / secondary 两个窗口
_CODEX_WINDOWS: tuple[tuple[float, str], ...] = (
    (18_000.0, WINDOW_ROLLING_5H),
    (86_400.0, WINDOW_DAILY),
    (604_800.0, WINDOW_WEEKLY),
    (2_592_000.0, WINDOW_MONTHLY),
)


@dataclass(slots=True)
class SnapshotRow:
    """一条待落库的官方窗口快照。"""

    plan_key: str
    window_key: str
    used: float | None = None
    quota: float | None = None
    percent: float | None = None
    unit: str = "percent"
    status: str = "ok"
    resets_at: datetime | None = None


@dataclass(slots=True)
class SourceResult:
    source: str
    rows: list[SnapshotRow] = field(default_factory=list)
    error: str | None = None


class UsageSource(Protocol):
    name: str

    async def fetch(self) -> SourceResult: ...


# --- 通用解析 ---


def _float_of(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().rstrip("%")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _iso_of(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_opencode_config(settings: Settings) -> dict[str, Any] | None:
    """读取 opencode 的全局配置（`FLUX_OPENCODE_CONFIG_FILE`）。"""
    raw = (settings.opencode_config_file or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def opencode_provider_key(settings: Settings, provider: str) -> str | None:
    """从 opencode 配置里取某个 provider 的 apiKey。"""
    config = load_opencode_config(settings)
    providers = config.get("provider") if isinstance(config, dict) else None
    block = providers.get(provider) if isinstance(providers, dict) else None
    options = block.get("options") if isinstance(block, dict) else None
    key = options.get("apiKey") if isinstance(options, dict) else None
    return str(key).strip() if isinstance(key, str) and key.strip() else None


#: Go 订阅 key 的 provider 候选名：opencode 0.11 起把 go 合并进 `opencode`
_OPENCODE_GO_PROVIDERS: tuple[str, ...] = (PLAN_OPENCODE_GO, "opencode")


def opencode_go_key(settings: Settings) -> str | None:
    """取 Go 订阅的 apiKey：优先 `opencode-go`，旧配置合并后回退 `opencode`。"""
    for provider in _OPENCODE_GO_PROVIDERS:
        key = opencode_provider_key(settings, provider)
        if key:
            return key
    return None


def opencode_default_model(settings: Settings) -> str:
    """opencode 配置里的默认模型（顶层 `model`）。"""
    config = load_opencode_config(settings)
    model = config.get("model") if isinstance(config, dict) else None
    return str(model).strip() if isinstance(model, str) else ""


def resolve_default_model(settings: Settings, runtime_id: str) -> str:
    """按 runtime 解析「本轮实际使用的模型」，供额度归因与闸门使用。"""
    if runtime_id == "opencode":
        return opencode_default_model(settings)
    if runtime_id == "codex":
        return (settings.codex_model or "").strip()
    return (settings.dsh_model or "").strip()


# --- OpenCode Go ---


class OpenCodeGoUsageSource:
    """Go 套餐的官方额度（5h / 周 / 月 三个滚动窗口）。"""

    name = "opencode_go"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def fetch(self) -> SourceResult:
        api_key = opencode_go_key(self._settings)
        if not api_key:
            return SourceResult(
                source=self.name,
                error=(
                    "opencode 配置里没有 opencode-go / opencode 的 apiKey："
                    f"{self._settings.opencode_config_file}"
                ),
            )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": OPENCODE_UA,
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(OPENCODE_GO_USAGE_URL, headers=headers)
        except httpx.HTTPError as exc:
            return SourceResult(source=self.name, error=f"请求 Go 额度接口失败：{exc}")
        if response.status_code != 200:
            detail = response.text.strip().replace("\n", " ")[:160]
            return SourceResult(
                source=self.name,
                error=f"Go 额度接口返回 HTTP {response.status_code}：{detail}",
            )
        try:
            payload = response.json()
        except ValueError:
            return SourceResult(source=self.name, error="Go 额度接口返回的不是 JSON")
        usage = payload.get("usage") if isinstance(payload, dict) else None
        block = usage if isinstance(usage, dict) else payload
        rows: list[SnapshotRow] = []
        for raw_key, item in (block or {}).items():
            if not isinstance(item, dict):
                continue
            window_key = official_window_key(str(raw_key))
            percent = _float_of(item.get("percent"))
            if percent is None:
                continue
            rows.append(
                SnapshotRow(
                    plan_key=PLAN_OPENCODE_GO,
                    window_key=window_key,
                    percent=percent,
                    unit="percent",
                    status=str(item.get("status") or "ok"),
                    resets_at=_iso_of(item.get("resetsAt") or item.get("resets_at")),
                )
            )
        if not rows:
            return SourceResult(source=self.name, error="Go 额度接口没有可识别的窗口")
        return SourceResult(source=self.name, rows=rows)


# --- ChatGPT / Codex 订阅 ---


class Fetcher(Protocol):
    """一次 GET：返回 (HTTP 状态码, 原始响应体)。"""

    async def __call__(self, url: str, headers: dict[str, str]) -> tuple[int, bytes]: ...


async def _http_get(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    """chatgpt.com 封 httpx 的握手特征（403），urllib 实测 200，所以在工作线程里用它。"""
    return await asyncio.to_thread(_http_get_sync, url, headers, 15.0)


def _http_get_sync(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class ChatGptCodexUsageSource:
    """ChatGPT 账号的 Codex 额度（plan_type + primary/secondary 两个窗口）。

    凭据来自 Flux 自己的账号目录（`FLUX_CODEX_ACCOUNT_HOME/auth.json`），
    只读 access_token / account_id，不刷新、不落任何 token 副本。
    官方返回 `rate_limit.primary_window / secondary_window`：
    `used_percent`、`limit_window_seconds`、`reset_at`（Unix 秒）。
    """

    name = "chatgpt_codex"

    def __init__(
        self,
        settings: Settings,
        *,
        fetcher: Fetcher | None = None,
    ) -> None:
        self._settings = settings
        #: 测试注入；生产走 urllib（httpx 的握手特征会被 chatgpt.com 判成 403）
        self._fetcher = fetcher

    async def fetch(self) -> SourceResult:
        tokens, error = _codex_tokens(self._settings)
        if error or tokens is None:
            return SourceResult(source=self.name, error=error or "ChatGPT 账号凭据不可用")
        headers = {
            "Authorization": f"Bearer {tokens['access_token']}",
            "chatgpt-account-id": tokens["account_id"],
            "User-Agent": CHATGPT_UA,
            "originator": CHATGPT_ORIGINATOR,
            "Accept": "application/json",
        }
        fetcher = self._fetcher or _http_get
        try:
            status, body = await fetcher(CHATGPT_USAGE_URL, headers)
        except Exception as exc:  # noqa: BLE001 - 网络异常一律记成采集失败
            return SourceResult(source=self.name, error=f"请求 ChatGPT 额度接口失败：{exc}")
        if status == 401:
            return SourceResult(
                source=self.name,
                error="ChatGPT 登录态已失效：请在「设置 → ChatGPT 账号」重新登录",
            )
        if status != 200:
            detail = body.decode("utf-8", "replace").strip().replace("\n", " ")[:160]
            return SourceResult(
                source=self.name,
                error=f"ChatGPT 额度接口返回 HTTP {status}：{detail}",
            )
        try:
            payload = json.loads(body)
        except ValueError:
            return SourceResult(source=self.name, error="ChatGPT 额度接口返回的不是 JSON")
        rows = _rows_from_chatgpt_usage(payload, PLAN_CODEX)
        if not rows:
            return SourceResult(source=self.name, error="ChatGPT 额度接口没有可识别的窗口")
        return SourceResult(source=self.name, rows=rows)


def _codex_tokens(settings: Settings) -> tuple[dict[str, str] | None, str | None]:
    """读账号目录的 auth.json，返回 (access_token/account_id, 错误文案)。"""
    # 延迟导入：codex_account 会导入 billing.plans，顶层互相引用会形成循环
    from flux.core.agent_runtime.codex_account import codex_account_home

    path = codex_account_home(settings) / "auth.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "还没有登录 ChatGPT 账号：登录后会自动采集额度"
    except (OSError, ValueError):
        return None, f"ChatGPT 登录态不可读：{path}"
    tokens = raw.get("tokens") if isinstance(raw, dict) else None
    tokens = tokens if isinstance(tokens, dict) else {}
    access_token = str(tokens.get("access_token") or "").strip()
    account_id = str(tokens.get("account_id") or "").strip()
    if not access_token or not account_id:
        return None, "ChatGPT 登录态缺少 access_token / account_id：请重新登录"
    return {"access_token": access_token, "account_id": account_id}, None


def codex_account_id(settings: Settings) -> str | None:
    """当前 ChatGPT 账号的 account_id（只用于账号指纹，不暴露 token）。"""
    tokens, _ = _codex_tokens(settings)
    return tokens["account_id"] if tokens else None


def _rows_from_chatgpt_usage(payload: Any, plan_key: str) -> list[SnapshotRow]:
    """把 official `rate_limit` 的两个窗口转成快照行。"""
    block = payload.get("rate_limit") if isinstance(payload, dict) else None
    if not isinstance(block, dict):
        return []
    limit_reached = bool(block.get("limit_reached"))
    status = "limit_reached" if limit_reached else "ok"
    rows: list[SnapshotRow] = []
    seen: set[str] = set()
    for slot in ("primary_window", "secondary_window"):
        item = block.get(slot)
        if not isinstance(item, dict):
            continue
        percent = _float_of(item.get("used_percent"))
        if percent is None:
            continue
        window_key = _codex_window_key(_float_of(item.get("limit_window_seconds")))
        if window_key in seen:
            continue
        seen.add(window_key)
        rows.append(
            SnapshotRow(
                plan_key=plan_key,
                window_key=window_key,
                percent=round(percent, 4),
                unit="percent",
                status=status,
                resets_at=_codex_reset_at(item),
            )
        )
    return rows


def _codex_window_key(seconds: float | None) -> str:
    """按窗口时长归到本地窗口；未知时长取最接近的一档，避免前端出现陌生窗口键。"""
    if seconds is None:
        return WINDOW_MONTHLY
    return min(_CODEX_WINDOWS, key=lambda item: abs(item[0] - seconds))[1]


def _codex_reset_at(item: dict[str, Any]) -> datetime | None:
    """官方同时给 `reset_at`（Unix 秒）与 `reset_after_seconds`，优先前者。"""
    reset_at = _float_of(item.get("reset_at"))
    if reset_at is not None:
        try:
            return datetime.fromtimestamp(reset_at, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    after = _float_of(item.get("reset_after_seconds"))
    if after is not None:
        return datetime.now(timezone.utc) + timedelta(seconds=max(after, 0.0))
    return None


# --- 命令采集（火山 arkcli / 自定义 CLI）---


class CommandUsageSource:
    """跑一条本机命令，解析它输出的 JSON 额度。

    火山 Agent Plan（arkcli）与自定义 `kind="cli"` 套餐共用这一份实现：
    `command` 为空时回退到 `FLUX_BILLING_ARKCLI_CMD`。
    """

    name = "arkcli"

    def __init__(
        self,
        settings: Settings,
        *,
        plan_key: str = PLAN_AGENT_PLAN,
        command: str = "",
        name: str = "",
        timeout: float | None = None,
    ) -> None:
        self._settings = settings
        self._plan_key = plan_key
        self._command = command.strip()
        self._timeout = timeout
        if name:
            self.name = name

    async def fetch(self) -> SourceResult:
        command = self._command or (self._settings.billing_arkcli_cmd or "").strip()
        if not command:
            return SourceResult(source=self.name, error="未配置额度采集命令")
        argv = shlex.split(command)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return SourceResult(
                source=self.name,
                error=f"找不到命令 {argv[0]!r}：需要安装对应 CLI 并完成登录",
            )
        except OSError as exc:
            return SourceResult(source=self.name, error=f"启动采集命令失败：{exc}")
        timeout = (
            self._timeout
            if self._timeout is not None
            else self._settings.billing_arkcli_timeout_seconds
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=max(float(timeout), 1.0),
            )
        except asyncio.TimeoutError:
            process.kill()
            await asyncio.gather(process.wait(), return_exceptions=True)
            return SourceResult(source=self.name, error="额度采集命令超时未返回")
        text = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            detail = (err or text or "").replace("\n", " ")[:200]
            return SourceResult(
                source=self.name,
                error=f"采集命令退出码 {process.returncode}：{detail}",
            )
        payload = _json_of(text)
        if payload is None:
            return SourceResult(
                source=self.name,
                error=f"采集命令输出不是 JSON：{text[:200] or '(空)'}",
            )
        rows = _rows_from_payload(payload, self._plan_key)
        if not rows:
            return SourceResult(
                source=self.name,
                error=f"采集命令输出里没有可识别的额度字段：{text[:200]}",
            )
        return SourceResult(source=self.name, rows=rows)


#: 旧名字：arkcli 专用采集器（现在只是 CommandUsageSource 的一个实例参数）
ArkCliUsageSource = CommandUsageSource


def _json_of(text: str) -> Any:
    """arkcli 可能打印日志再输出 JSON：先整体试，失败则逐行试最后一段。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith(("{", "[")):
            continue
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def _rows_from_payload(
    payload: Any,
    plan_key: str,
    *,
    percent_keys: tuple[str, ...] = ("percent", "percent_used", "used_percent", "usage_percent"),
    used_keys: tuple[str, ...] = ("used", "used_amount", "consumed"),
    quota_keys: tuple[str, ...] = ("quota", "total", "limit", "total_amount"),
) -> list[SnapshotRow]:
    """尽力解析额度 JSON：先找窗口容器，再按候选字段名逐个尝试。

    兼容两种形态：
    - `{"rolling": {...}, "weekly": {...}}`（窗口对象）；
    - `{"used": 12, "total": 100}`（没有窗口信息，按整月处理）。
    """
    if not isinstance(payload, dict):
        return []
    container = payload
    for key in ("usage", "data", "result", "plan", "quota"):
        value = payload.get(key)
        if isinstance(value, dict) and value is not payload:
            container = value
            break
    rows: list[SnapshotRow] = []
    for raw_key, item in container.items():
        if not isinstance(item, dict):
            continue
        percent = _first_float(item, percent_keys)
        used = _first_float(item, used_keys)
        quota = _first_float(item, quota_keys)
        if percent is None and used is not None and quota:
            percent = used / quota * 100.0
        if percent is None:
            continue
        rows.append(
            SnapshotRow(
                plan_key=plan_key,
                window_key=official_window_key(str(raw_key)),
                used=used,
                quota=quota,
                percent=round(percent, 4),
                unit=_unit_of(plan_key, used),
                status=str(item.get("status") or "ok"),
                resets_at=_iso_of(
                    item.get("resetsAt")
                    or item.get("reset_at")
                    or item.get("resetAt")
                    or item.get("next_reset_at")
                ),
            )
        )
    if rows:
        return rows
    used = _first_float(container, used_keys)
    quota = _first_float(container, quota_keys)
    if used is not None and quota:
        return [
            SnapshotRow(
                plan_key=plan_key,
                window_key=WINDOW_MONTHLY,
                used=used,
                quota=quota,
                percent=round(used / quota * 100.0, 4),
                unit=_unit_of(plan_key, used),
            )
        ]
    return []


def _unit_of(plan_key: str, used: float | None) -> str:
    """`used` 有值才需要单位：火山按 AFP 记，其它套餐按通用「额度」记。"""
    if used is None:
        return "percent"
    return "afp" if plan_key == PLAN_AGENT_PLAN else "quota"


def _first_float(payload: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key in payload:
            value = _float_of(payload.get(key))
            if value is not None:
                return value
    return None


# --- HTTP 采集（自定义套餐）---

_ENV_REF = re.compile(r"\{env:([A-Za-z0-9_]+)\}")
_FILE_REF = re.compile(r"\{file:([^}]+)\}")


def _dig(payload: Any, path: str) -> Any:
    """按点路径取值（`data.windows`）；路径为空返回原对象。"""
    current = payload
    for segment in [part for part in path.split(".") if part]:
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


def _expand_refs(value: str) -> str:
    """展开 header 里的 `{env:NAME}` / `{file:/path#dot.path}` 引用。

    额度端点通常复用本机 CLI 的登录凭据（Claude / Codex 的 OAuth token 就在
    各自配置文件里），所以支持从文件点路径取；取不到直接报错，不静默发空 token。
    """

    def env_sub(match: re.Match[str]) -> str:
        name = match.group(1)
        current = os.environ.get(name)
        if current is None:
            raise ValueError(f"环境变量 {name} 未设置")
        return current

    def file_sub(match: re.Match[str]) -> str:
        raw = match.group(1)
        path_part, _, dot_path = raw.partition("#")
        path = Path(path_part).expanduser()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"读取凭据文件失败：{path}（{exc}）") from exc
        value = _dig(payload, dot_path)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"凭据文件里没有字符串字段：{raw}")
        return value.strip()

    return _FILE_REF.sub(file_sub, _ENV_REF.sub(env_sub, value))


class HttpUsageSource:
    """GET 一个额度端点，按 `SourceSpec` 里的字段名解析（自定义套餐用）。"""

    name = "http"

    def __init__(self, plan_key: str, spec: SourceSpec) -> None:
        self._plan_key = plan_key
        self._spec = spec

    async def fetch(self) -> SourceResult:
        spec = self._spec
        if not spec.url:
            return SourceResult(source=self.name, error="未配置额度接口 URL")
        try:
            headers = {key: _expand_refs(value) for key, value in spec.headers.items()}
        except ValueError as exc:
            return SourceResult(source=self.name, error=str(exc))
        if spec.user_agent:
            headers.setdefault("User-Agent", spec.user_agent)
        try:
            async with httpx.AsyncClient(timeout=max(float(spec.timeout), 1.0)) as client:
                response = await client.get(spec.url, headers=headers)
        except httpx.HTTPError as exc:
            return SourceResult(source=self.name, error=f"请求额度接口失败：{exc}")
        if response.status_code != 200:
            detail = response.text.strip().replace("\n", " ")[:160]
            return SourceResult(
                source=self.name,
                error=f"额度接口返回 HTTP {response.status_code}：{detail}",
            )
        try:
            payload = response.json()
        except ValueError:
            return SourceResult(source=self.name, error="额度接口返回的不是 JSON")
        block = _dig(payload, spec.usage_path) if spec.usage_path else payload
        if not isinstance(block, dict):
            return SourceResult(
                source=self.name,
                error=f"额度接口的 usage_path 没有取到对象：{spec.usage_path}",
            )
        rows = _rows_from_payload(
            block,
            self._plan_key,
            percent_keys=spec.percent_keys,
            used_keys=spec.used_keys,
            quota_keys=spec.quota_keys,
        )
        if not rows:
            return SourceResult(source=self.name, error="额度接口没有可识别的窗口")
        return SourceResult(source=self.name, rows=rows)


def build_sources(
    settings: Settings,
    plans: dict[str, PlanDefinition] | None = None,
) -> dict[str, UsageSource]:
    """按套餐装配采集器（key = plan_key；没有采集方式的套餐不出现在这里）。"""
    registry = plans if plans is not None else default_plans()
    sources: dict[str, UsageSource] = {}
    for key, plan in registry.items():
        spec = plan.source
        if spec is not None:
            if spec.kind == "none":
                continue
            if spec.kind == "cli" and spec.command:
                sources[key] = CommandUsageSource(
                    settings,
                    plan_key=key,
                    command=spec.command,
                    name="cli",
                    timeout=spec.timeout,
                )
            elif spec.kind == "http" and spec.url:
                sources[key] = HttpUsageSource(key, spec)
            continue
        if key == PLAN_OPENCODE_GO:
            sources[key] = OpenCodeGoUsageSource(settings)
        elif key == PLAN_AGENT_PLAN:
            sources[key] = CommandUsageSource(settings, plan_key=key, name="arkcli")
        elif key == PLAN_CODEX and plan.builtin:
            # 账号未登录时采集器返回明确错误，登录成功后登录流程会立刻补一次采集
            sources[key] = ChatGptCodexUsageSource(settings)
    return sources


__all__ = [
    "CHATGPT_USAGE_URL",
    "DEFAULT_ARKCLI_CMD",
    "OPENCODE_GO_USAGE_URL",
    "ArkCliUsageSource",
    "ChatGptCodexUsageSource",
    "CommandUsageSource",
    "HttpUsageSource",
    "OpenCodeGoUsageSource",
    "SnapshotRow",
    "SourceResult",
    "UsageSource",
    "build_sources",
    "codex_account_id",
    "load_opencode_config",
    "opencode_default_model",
    "opencode_go_key",
    "opencode_provider_key",
    "resolve_default_model",
]
