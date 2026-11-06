"""套餐与额度窗口定义（纯函数 + 一个可选的自定义套餐文件）。

归因规则（按优先级）：
1. **模型前缀**：模型 id 的第一段就是供应商/套餐前缀（`opencode-go/gemini-4-argon`），
   这也是 opencode 自己的写法。命中 `model_prefixes` 才参与额度统计与闸门；
2. **runtime**：模型 id 不带前缀时（codex 的 `gpt-5.5`），按当前 Agent 的 runtime 归因；
3. 都没命中 → 只记账、不限额（`plan_key=None`）。

套餐不再是写死的三张：内置套餐 + `FLUX_BILLING_PLANS_FILE` 指向的 JSON 合并，
新增套餐（Codex / Claude / 任何订阅）只需加一条配置，不用改代码。文件格式：

```json
{
  "plans": [
    {
      "key": "claude-max",
      "label": "Claude Max",
      "vendor": "Anthropic",
      "model_prefixes": ["claude", "anthropic"],
      "runtime_ids": ["claude-code"],
      "windows": ["5h", "weekly", "monthly"],
      "fallback_model": "deepseek/deepseek-flash",
      "quota_usd": 100,
      "source": {"kind": "cli", "command": "claude usage --json", "timeout": 20}
    }
  ]
}
```

`source.kind` 支持 `cli`（跑命令、解析 JSON）与 `http`（GET 一个额度接口）；
省略 `source` 就是"只记账，不限额"。同 key 的自定义套餐会覆盖内置定义。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: 套餐标识（与 usage_records.plan_key / usage_snapshots.plan_key 同源）
PLAN_OPENCODE_GO = "opencode-go"
PLAN_AGENT_PLAN = "agent-plan"
PLAN_OPENCODE_ZEN = "opencode"
PLAN_CODEX = "codex"
PLAN_CLAUDE = "claude"

#: 窗口标识（与 usage_snapshots.window_key 同源）
WINDOW_ROLLING_5H = "rolling_5h"
WINDOW_DAILY = "daily"
WINDOW_WEEKLY = "weekly"
WINDOW_MONTHLY = "monthly"


@dataclass(frozen=True, slots=True)
class WindowSpec:
    """一个额度窗口。

    `rolling=True` 表示「以当前时刻为终点回溯固定时长」（5 小时 / 7 天滚动窗）；
    `False` 表示按 UTC 自然边界（自然日 / 自然月）。
    """

    key: str
    label: str
    rolling: bool = True
    duration: timedelta | None = None

    def start(self, now: datetime | None = None) -> datetime:
        """窗口起点（UTC）。"""
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if self.key == WINDOW_MONTHLY:
            return current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if self.key == WINDOW_DAILY:
            return current.replace(hour=0, minute=0, second=0, microsecond=0)
        if self.rolling and self.duration is not None:
            return current - self.duration
        return current.replace(hour=0, minute=0, second=0, microsecond=0)


WINDOW_5H = WindowSpec(WINDOW_ROLLING_5H, "5 小时", rolling=True, duration=timedelta(hours=5))
WINDOW_1D = WindowSpec(WINDOW_DAILY, "自然日", rolling=False)
WINDOW_7D = WindowSpec(WINDOW_WEEKLY, "7 天", rolling=True, duration=timedelta(days=7))
WINDOW_1M = WindowSpec(WINDOW_MONTHLY, "自然月", rolling=False)

#: 官方窗口键 → 本地窗口定义
OFFICIAL_WINDOW_ALIASES: dict[str, str] = {
    # OpenCode Go /zen/go/v1/usage
    "rolling": WINDOW_ROLLING_5H,
    "rolling_5h": WINDOW_ROLLING_5H,
    "5h": WINDOW_ROLLING_5H,
    "five_hour": WINDOW_ROLLING_5H,
    "fivehour": WINDOW_ROLLING_5H,
    # 日
    "daily": WINDOW_DAILY,
    "day": WINDOW_DAILY,
    "1d": WINDOW_DAILY,
    # 周
    "weekly": WINDOW_WEEKLY,
    "week": WINDOW_WEEKLY,
    "7d": WINDOW_WEEKLY,
    # 月
    "monthly": WINDOW_MONTHLY,
    "month": WINDOW_MONTHLY,
}

#: 配置里可用的窗口别名 → WindowSpec（自定义套餐的 "windows": [...] 用）
_WINDOW_SPECS: dict[str, WindowSpec] = {
    WINDOW_ROLLING_5H: WINDOW_5H,
    "5h": WINDOW_5H,
    "five_hour": WINDOW_5H,
    "rolling": WINDOW_5H,
    WINDOW_DAILY: WINDOW_1D,
    "day": WINDOW_1D,
    "1d": WINDOW_1D,
    WINDOW_WEEKLY: WINDOW_7D,
    "week": WINDOW_7D,
    "7d": WINDOW_7D,
    WINDOW_MONTHLY: WINDOW_1M,
    "month": WINDOW_1M,
}


def official_window_key(raw: str) -> str:
    """把官方返回的窗口名收敛成本地窗口键；未知窗口原样小写返回。"""
    key = (raw or "").strip().lower()
    return OFFICIAL_WINDOW_ALIASES.get(key, key)


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """自定义套餐的官方额度采集方式（内置套餐走代码里的专用采集器）。"""

    kind: str = "cli"
    command: str = ""
    url: str = ""
    timeout: float = 20.0
    #: 从返回 JSON 里取窗口对象的点路径（`usage` / `data.windows`）；空 = 顶层
    usage_path: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    user_agent: str = ""
    percent_keys: tuple[str, ...] = ("percent", "percent_used", "used_percent", "usage_percent")
    used_keys: tuple[str, ...] = ("used", "used_amount", "consumed")
    quota_keys: tuple[str, ...] = ("quota", "total", "limit", "total_amount")


@dataclass(frozen=True, slots=True)
class PlanDefinition:
    """一个套餐/计费通道的定义。"""

    key: str
    label: str
    vendor: str
    model_prefixes: tuple[str, ...]
    windows: tuple[WindowSpec, ...]
    #: 官方额度采集器标识（展示用；内置套餐对应代码里的专用采集器）
    official_source: str = ""
    #: 额度用尽后的降级目标（写进闸门提示，不自动切换）
    fallback_model: str = ""
    #: 套餐总额（美元）；只有官方不给百分比时才用它把本地账本折算成占比
    quota_usd: float | None = None
    #: 模型 id 不带前缀时按 runtime 归因（如 codex → ChatGPT 订阅）。
    #: 注意：用 API Key 跑同一个 runtime 也会归到这里；不想要这种归因时，
    #: 在同 key 的自定义套餐里写 `"runtime_ids": []` 把它关掉。
    runtime_ids: tuple[str, ...] = ()
    #: 自定义套餐的采集方式；None = 只在代码里装配（内置）或只记账
    source: SourceSpec | None = None
    #: 是否内置套餐（自定义文件同 key 覆盖后会被标记 False）
    builtin: bool = False

    def window(self, key: str) -> WindowSpec | None:
        for item in self.windows:
            if item.key == key:
                return item
        return None

    @property
    def model_prefix(self) -> str:
        """展示用：多个前缀用 ` / ` 连接，前端仍是单个字符串。"""
        return " / ".join(self.model_prefixes)


def default_plans() -> dict[str, PlanDefinition]:
    """内置套餐（顺序即前端展示顺序）。

    Codex / Claude 默认只记账、不限额：它们的官方额度没有稳定公开接口，
    需要时用自定义套餐文件补 `source`（HTTP 端点或本机 CLI 命令）。
    """
    return {
        PLAN_OPENCODE_GO: PlanDefinition(
            key=PLAN_OPENCODE_GO,
            label="OpenCode Go",
            vendor="OpenCode",
            model_prefixes=(PLAN_OPENCODE_GO,),
            windows=(WINDOW_5H, WINDOW_7D, WINDOW_1M),
            official_source="opencode_go",
            fallback_model="opencode/deepseek-v4.1-flash-free",
            builtin=True,
        ),
        PLAN_AGENT_PLAN: PlanDefinition(
            key=PLAN_AGENT_PLAN,
            label="火山 Agent Plan",
            vendor="Volcengine",
            model_prefixes=(PLAN_AGENT_PLAN,),
            windows=(WINDOW_5H, WINDOW_1D, WINDOW_7D, WINDOW_1M),
            official_source="arkcli",
            fallback_model="deepseek/deepseek-flash",
            builtin=True,
        ),
        PLAN_OPENCODE_ZEN: PlanDefinition(
            key=PLAN_OPENCODE_ZEN,
            label="OpenCode Zen",
            vendor="OpenCode",
            model_prefixes=(PLAN_OPENCODE_ZEN,),
            windows=(WINDOW_1D, WINDOW_1M),
            builtin=True,
        ),
        PLAN_CODEX: PlanDefinition(
            key=PLAN_CODEX,
            label="Codex 订阅",
            vendor="OpenAI",
            model_prefixes=(PLAN_CODEX,),
            windows=(WINDOW_5H, WINDOW_7D, WINDOW_1M),
            runtime_ids=(PLAN_CODEX,),
            # ChatGPT 账号的 Codex 额度：登录后自动采集 plan_type + 官方窗口百分比
            official_source="chatgpt_codex",
            builtin=True,
        ),
        PLAN_CLAUDE: PlanDefinition(
            key=PLAN_CLAUDE,
            label="Claude 订阅",
            vendor="Anthropic",
            model_prefixes=(PLAN_CLAUDE, "anthropic"),
            windows=(WINDOW_5H, WINDOW_7D, WINDOW_1M),
            runtime_ids=("claude-code", PLAN_CLAUDE),
            builtin=True,
        ),
    }


#: 内置套餐（保持旧名字；服务实际使用的注册表可由自定义文件扩展）
PLANS: dict[str, PlanDefinition] = default_plans()


def _windows_of(raw: Any) -> tuple[WindowSpec, ...]:
    if not isinstance(raw, list) or not raw:
        return (WINDOW_5H, WINDOW_7D, WINDOW_1M)
    windows: list[WindowSpec] = []
    for item in raw:
        spec = _WINDOW_SPECS.get(str(item).strip().lower())
        if spec is not None and spec not in windows:
            windows.append(spec)
    return tuple(windows) or (WINDOW_5H, WINDOW_7D, WINDOW_1M)


def _strings_of(raw: Any) -> tuple[str, ...]:
    """前缀 / runtime 列表：统一小写，便于和模型 id、runtime 标识比较。"""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return ()
    values = [str(item).strip().lower() for item in raw]
    return tuple(item for item in values if item)


def _keys_of(raw: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    """JSON 字段名列表：保留大小写（`percentUsed` 不能变成 `percentused`）。"""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return default
    values = [str(item).strip() for item in raw]
    kept = tuple(item for item in values if item)
    return kept or default


def _source_of(raw: Any) -> SourceSpec | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "cli").strip().lower()
    if kind not in ("cli", "http", "none"):
        raise ValueError(f"未知的 source.kind：{kind}")
    headers = raw.get("headers")
    defaults = SourceSpec()
    return SourceSpec(
        kind=kind,
        command=str(raw.get("command") or "").strip(),
        url=str(raw.get("url") or "").strip(),
        timeout=float(raw.get("timeout") or 20.0),
        usage_path=str(raw.get("usage_path") or "").strip(),
        headers={str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {},
        user_agent=str(raw.get("user_agent") or "").strip(),
        percent_keys=_keys_of(raw.get("percent_keys"), defaults.percent_keys),
        used_keys=_keys_of(raw.get("used_keys"), defaults.used_keys),
        quota_keys=_keys_of(raw.get("quota_keys"), defaults.quota_keys),
    )


def plan_from_dict(raw: dict[str, Any]) -> PlanDefinition:
    """把配置文件里的一条套餐定义转成 `PlanDefinition`（校验失败抛 ValueError）。"""
    key = str(raw.get("key") or "").strip().lower()
    if not key:
        raise ValueError("套餐缺少 key")
    prefixes = _strings_of(raw.get("model_prefixes") or raw.get("model_prefix"))
    runtime_ids = _strings_of(raw.get("runtime_ids"))
    if not prefixes and not runtime_ids:
        raise ValueError(f"套餐 {key} 至少要给 model_prefixes 或 runtime_ids")
    source = _source_of(raw.get("source"))
    quota = raw.get("quota_usd")
    fallback_source = "" if source is None or source.kind == "none" else source.kind
    return PlanDefinition(
        key=key,
        label=str(raw.get("label") or key).strip(),
        vendor=str(raw.get("vendor") or "").strip(),
        model_prefixes=prefixes,
        windows=_windows_of(raw.get("windows")),
        official_source=str(raw.get("official_source") or fallback_source).strip(),
        fallback_model=str(raw.get("fallback_model") or "").strip(),
        quota_usd=(
            float(quota)
            if isinstance(quota, (int, float)) and not isinstance(quota, bool)
            else None
        ),
        runtime_ids=runtime_ids,
        source=source,
        builtin=False,
    )


def load_plan_registry(path: str | None) -> dict[str, PlanDefinition]:
    """内置套餐 + 自定义文件合并；文件不存在 = 只用内置。

    单条写错只跳过那一条（配置文件是手写的，不能让一个拼写错误拖垮整套计费）。
    """
    plans = default_plans()
    raw_path = (path or "").strip()
    if not raw_path:
        return plans
    file_path = Path(raw_path).expanduser()
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return plans
    except (OSError, ValueError):
        return plans
    items = payload.get("plans") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return plans
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            plan = plan_from_dict(item)
        except (TypeError, ValueError):
            continue
        plans[plan.key] = plan
    return plans


def model_prefix(model: str) -> str:
    """模型 id 的供应商段（`opencode-go/gemini-4-argon` → `opencode-go`）。"""
    value = (model or "").strip()
    if not value:
        return ""
    return value.split("/", 1)[0].strip().lower()


def resolve_plan(
    model: str,
    runtime_id: str = "",
    plans: dict[str, PlanDefinition] | None = None,
) -> PlanDefinition | None:
    """按「模型前缀 → runtime」解析套餐；未登记返回 None（只记账、不限额）。"""
    registry = plans if plans is not None else PLANS
    prefix = model_prefix(model)
    if prefix:
        for plan in registry.values():
            if prefix in plan.model_prefixes:
                return plan
    runtime = (runtime_id or "").strip().lower()
    if runtime:
        for plan in registry.values():
            if runtime in plan.runtime_ids:
                return plan
    return None


__all__ = [
    "OFFICIAL_WINDOW_ALIASES",
    "PLANS",
    "PLAN_AGENT_PLAN",
    "PLAN_CLAUDE",
    "PLAN_CODEX",
    "PLAN_OPENCODE_GO",
    "PLAN_OPENCODE_ZEN",
    "WINDOW_1D",
    "WINDOW_1M",
    "WINDOW_5H",
    "WINDOW_7D",
    "WINDOW_DAILY",
    "WINDOW_MONTHLY",
    "WINDOW_ROLLING_5H",
    "WINDOW_WEEKLY",
    "PlanDefinition",
    "SourceSpec",
    "WindowSpec",
    "default_plans",
    "load_plan_registry",
    "model_prefix",
    "official_window_key",
    "plan_from_dict",
    "resolve_plan",
]
