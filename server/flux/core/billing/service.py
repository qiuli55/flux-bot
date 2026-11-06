"""计费编排：落账、窗口聚合、官方对账、闸门判定。

三条口径（决定了这里为什么这么写）：
1. **本地账本**是唯一能做任务级归因的来源——每一步模型调用落一条 `usage_records`；
2. **官方快照**只给总量（Go 的 `/usage`、火山的 arkcli），用于对账与闸门；
3. **闸门**默认只告警（`FLUX_BILLING_ENFORCE=false`），开启后才在启动时拒绝新 Run。

因此 `record_step` 永不抛错（账本问题不能拖垮 Run），而 `ensure_can_start`
只有在 `enforce=True` 且明确超额时才抛 `ConflictError`。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.cloud.billing_sync import SYNC_INTERVAL_SECONDS, CloudBillingSync
from flux.config import Settings
from flux.core.billing.accounts import (
    account_label,
    resolve_plan_accounts,
)
from flux.core.billing.plans import (
    PLAN_OPENCODE_GO,
    WINDOW_MONTHLY,
    PlanDefinition,
    WindowSpec,
    default_plans,
    load_plan_registry,
    model_prefix,
    resolve_plan,
)
from flux.core.billing.pricing import PriceBook, load_price_book
from flux.core.billing.repository import BillingRepository
from flux.core.billing.sources import build_sources, resolve_default_model
from flux.errors import ConflictError
from flux.logging import get_logger

logger = get_logger(__name__)

#: 闸门等级
LEVEL_OK = "ok"
LEVEL_WARNING = "warning"
LEVEL_EXCEEDED = "exceeded"
#: 官方数据不可用且无本地兜底口径，无法判定
LEVEL_UNKNOWN = "unknown"
#: 计费关闭
LEVEL_DISABLED = "disabled"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """SQLite 读回会丢 tzinfo，比较前统一按 UTC 归一。"""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _row_time(value: Any) -> datetime | None:
    """快照行的 fetched_at 可能已是 datetime，也可能是 to_dict 后的 ISO 字符串。"""
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, str) and value:
        try:
            return _as_utc(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


@dataclass(frozen=True, slots=True)
class GateDecision:
    """一次额度闸门判定。"""

    plan_key: str
    level: str
    percent: float | None = None
    window_key: str = ""
    message: str = ""
    enforce: bool = False
    fallback_model: str = ""
    #: 判定所依据的账号（空 = 本机没能识别账号）
    account_key: str = ""

    @property
    def blocked(self) -> bool:
        """只有「开启拦截 + 明确超额」才真的拒绝新 Run。"""
        return self.enforce and self.level == LEVEL_EXCEEDED

    @property
    def alerting(self) -> bool:
        return self.level in (LEVEL_WARNING, LEVEL_EXCEEDED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_key": self.plan_key,
            "level": self.level,
            "percent": self.percent,
            "window_key": self.window_key,
            "message": self.message,
            "enforce": self.enforce,
            "fallback_model": self.fallback_model,
            "account_key": self.account_key,
            "blocked": self.blocked,
        }


class BillingService:
    """容器级单例：run 之间共享缓存与锁（CliRuntime 实例是每 Run 新建的）。"""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._repo = BillingRepository(session_factory)
        #: 内置套餐 + 自定义套餐文件（同 key 覆盖）；文件写坏时回退内置，计费不中断
        try:
            self._plans = load_plan_registry(settings.billing_plans_file)
        except Exception:  # noqa: BLE001 - 配置问题只影响扩展套餐，不影响主流程
            logger.warning("自定义套餐文件读取失败，回退内置套餐", exc_info=True)
            self._plans = default_plans()
        self._sources = build_sources(settings, self._plans)
        #: plan_key → 本机账号指纹（同一套餐多账号必须分开聚合；解析不到为空）
        self._accounts = resolve_plan_accounts(settings, self._plans)
        #: 官方价目表（每百万 token，USD 落账）；runtime 没上报 cost 时按它估算
        self._prices: PriceBook = load_price_book(
            settings.billing_prices_file,
            cny_per_usd=settings.billing_cny_per_usd,
        )
        #: plan_key → 官方快照行（最近一次采集结果）
        self._cache: dict[str, list[dict[str, Any]]] = {}
        #: plan_key → 最近一次采集尝试的时刻（失败也算，避免反复打官方接口）
        self._cache_at: dict[str, datetime] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        #: 跨设备归集：本机账本增量上传 + 云端「全部设备」合计（关闭时整体不发起请求）
        self._cloud_sync = CloudBillingSync(settings, self._repo)
        #: 汇总键（plan|account|window）→ 云端合计
        self._cloud: dict[str, dict[str, Any]] = {}
        self._cloud_at: datetime | None = None
        self._cloud_error: str = ""
        #: plan_key → 云端出现过的账号指纹（补齐本机没见过的账号）
        self._cloud_accounts: dict[str, list[str]] = {}

    # --- 配置 ---

    @property
    def enabled(self) -> bool:
        return bool(self._settings.billing_enabled)

    @property
    def enforce(self) -> bool:
        return bool(self._settings.billing_enforce)

    @property
    def _refresh_seconds(self) -> int:
        return max(int(self._settings.billing_refresh_seconds or 0), 0)

    @property
    def _cloud_enabled(self) -> bool:
        return bool(self._settings.billing_cloud_sync)

    # --- 落账 ---

    async def record_step(
        self,
        *,
        run_id: str | None,
        model: str,
        provider: str = "",
        runtime_id: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        cost: float | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        """记录一次模型调用的增量用量；任何异常只记日志，不影响 Run。"""
        if not self.enabled:
            return
        if not (input_tokens or output_tokens or cache_read_tokens or cache_write_tokens):
            return
        plan = resolve_plan(model, runtime_id, self._plans)
        # runtime 自带 cost（opencode 会报）优先；没有就用官方价目表估算，再没有就留空。
        if cost is None:
            cost = self._prices.estimate_cost(
                model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                occurred_at=occurred_at,
            )
        try:
            await self._repo.add_record(
                provider=provider or model_prefix(model) or "unknown",
                model=model,
                plan_key=plan.key if plan else None,
                # 套餐已识别时打上本机账号指纹；未识别的模型只记账
                account_key=self._accounts.get(plan.key, "") if plan else "",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                cost=cost,
                run_id=run_id,
                task_id=task_id,
                agent_id=agent_id,
                occurred_at=occurred_at,
            )
        except Exception:  # 账本故障不能拖垮正在跑的 Run
            logger.warning("计费落账失败 run=%s model=%s", run_id, model, exc_info=True)

    # --- 查询 ---

    async def tokens_by_bot_since(self, since: datetime) -> dict[uuid.UUID, int]:
        """按 Bot 聚合 `since` 之后的 token 用量（Bot 每日预算的数据来源）。"""
        return await self._repo.tokens_by_bot_since(since)

    async def overview(self, *, refresh: bool = False) -> dict[str, Any]:
        """所有套餐的额度概览（前端成本中心的主数据）。"""
        plans: list[dict[str, Any]] = []
        for plan in self._plans.values():
            rows = await self._snapshot_rows(plan, refresh=refresh)
            plans.append(await self._plan_view(plan, rows))
        return {
            "enabled": self.enabled,
            "enforce": self.enforce,
            "alert_percent": float(self._settings.billing_alert_percent),
            "block_percent": float(self._settings.billing_block_percent),
            #: 官方快照的刷新周期（前端据此判断数据是否过期）
            "refresh_seconds": self._refresh_seconds,
            "plans_file": str(self._settings.billing_plans_file or ""),
            #: 跨设备归集状态：synced_at / error 由后台同步与「立即对账」共同维护
            "cloud": {
                "enabled": self._cloud_enabled,
                "synced_at": self._cloud_at.isoformat() if self._cloud_at else None,
                "error": self._cloud_error,
            },
            "plans": plans,
        }

    async def plan_view(self, plan_key: str, *, refresh: bool = False) -> dict[str, Any]:
        plan = self._plans.get(plan_key)
        if plan is None:
            raise ConflictError(f"未登记的套餐：{plan_key}", code="unknown_plan")
        rows = await self._snapshot_rows(plan, refresh=refresh)
        return await self._plan_view(plan, rows)

    def prices(self) -> dict[str, Any]:
        """官方价目表（前端只读展示，USD / 1M tokens）。"""
        return self._prices.view()

    async def run_usage(self, run_id: str) -> dict[str, Any]:
        """按 Run 回放明细并汇总。"""
        records = await self._repo.records_by_run(run_id)
        totals = {
            "records": len(records),
            "input_tokens": sum(r.input_tokens for r in records),
            "output_tokens": sum(r.output_tokens for r in records),
            "cache_read_tokens": sum(r.cache_read_tokens for r in records),
            "cache_write_tokens": sum(r.cache_write_tokens for r in records),
            "total_tokens": sum(r.total_tokens for r in records),
            "cost": round(sum(r.cost or 0.0 for r in records), 6),
        }
        return {"run_id": run_id, "totals": totals, "records": [r.to_dict() for r in records]}

    async def refresh(self, plan_key: str | None = None) -> dict[str, Any]:
        """立即拉取官方额度（页面上的「刷新」按钮）。"""
        results: list[dict[str, Any]] = []
        targets = [self._plans[plan_key]] if plan_key in self._plans else list(self._plans.values())
        for plan in targets:
            if plan.key not in self._sources:
                results.append(
                    {
                        "plan_key": plan.key,
                        "account_key": self._accounts.get(plan.key, ""),
                        "source": "",
                        "refreshed": False,
                        "rows": 0,
                        "error": "该套餐没有官方额度来源",
                    }
                )
                continue
            rows, error = await self._fetch_locked(plan)
            results.append(
                {
                    "plan_key": plan.key,
                    "account_key": self._accounts.get(plan.key, ""),
                    "source": plan.official_source,
                    "refreshed": error is None,
                    "rows": len(rows),
                    "error": error,
                }
            )
        # 顺手把本机新增账本推上云端，并刷新「全部设备」合计（失败不影响官方对账结果）
        cloud = await self.sync_cloud()
        return {"results": results, "cloud": cloud}

    # --- 跨设备归集 ---

    async def sync_cloud(self) -> dict[str, Any]:
        """增量上传本机账本 + 拉取云端合计；任何失败只记状态，不抛错。"""
        sync = self._cloud_sync
        if not sync.enabled:
            return {"ok": False, "error": "未启用套餐云归集"}
        pushed = await sync.push()
        accounts = await sync.accounts()
        if accounts is not None:
            self._cloud_accounts = accounts
        queries = await self._cloud_queries()
        items = await sync.summary(queries) if queries else {}
        if items is None:
            self._cloud_error = sync.last_error or "云端汇总拉取失败"
            return {
                "ok": False,
                "pushed": pushed,
                "queries": len(queries),
                "error": self._cloud_error,
            }
        self._cloud = items
        self._cloud_at = _utcnow()
        self._cloud_error = ""
        return {"ok": True, "pushed": pushed, "queries": len(queries), "error": ""}

    async def start_cloud_sync(self, interval: float = SYNC_INTERVAL_SECONDS) -> None:
        """后台循环：启动先跑一轮，之后按周期归集；取消时安静退出。"""
        while True:
            try:
                await self.sync_cloud()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 同步失败只记账，等下一轮
                logger.warning("套餐用量云归集失败", exc_info=True)
            await asyncio.sleep(interval)

    async def _cloud_queries(self) -> list[dict[str, Any]]:
        """把「套餐 × 账号 × 窗口」展开成云端汇总请求。"""
        queries: list[dict[str, Any]] = []
        for plan in self._plans.values():
            for account_key in await self._account_keys(plan):
                for window in plan.windows:
                    queries.append(
                        {
                            "key": self._cloud_key(plan.key, account_key, window.key),
                            "plan_key": plan.key,
                            "account_key": account_key,
                            "since": window.start().isoformat(),
                        }
                    )
        return queries

    @staticmethod
    def _cloud_key(plan_key: str, account_key: str, window_key: str) -> str:
        return f"{plan_key}|{account_key}|{window_key}"

    # --- 闸门 ---

    async def gate_for_model(
        self, model: str, *, runtime_id: str = "", refresh: bool = False
    ) -> GateDecision:
        """按模型前缀（其次 runtime）归因套餐并判定额度。未登记套餐一律放行。"""
        plan = resolve_plan(model, runtime_id, self._plans)
        if plan is None:
            return GateDecision(plan_key="", level=LEVEL_OK)
        rows = await self._snapshot_rows(plan, refresh=refresh)
        return await self._gate_for_plan(plan, rows, account_key=self._accounts.get(plan.key, ""))

    async def ensure_can_start(self, *, model: str, runtime_id: str) -> GateDecision:
        """Run 创建前的闸门：超额且开启拦截时抛 ConflictError。"""
        if not self.enabled:
            return GateDecision(plan_key="", level=LEVEL_DISABLED)
        effective = (model or "").strip() or resolve_default_model(self._settings, runtime_id)
        if not effective:
            return GateDecision(
                plan_key="", level=LEVEL_UNKNOWN, message="无法确定模型，跳过额度检查"
            )
        decision = await self.gate_for_model(effective, runtime_id=runtime_id, refresh=True)
        if decision.blocked:
            raise ConflictError(decision.message, code="billing_quota_exceeded")
        if decision.alerting:
            logger.warning(
                "额度告警 model=%s plan=%s level=%s percent=%s",
                effective,
                decision.plan_key,
                decision.level,
                decision.percent,
            )
        return decision

    # --- 内部 ---

    async def _plan_view(self, plan: PlanDefinition, rows: list[dict[str, Any]]) -> dict[str, Any]:
        current = self._accounts.get(plan.key, "")
        earliest = min((window.start() for window in plan.windows), default=None)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row.get("account_key") or ""), []).append(row)
        # 展示顺序：本机当前账号 → 账本里出现过的账号 → 云端其它设备用过的账号 →
        # 只出现在官方快照里的账号
        order = await self._account_keys(plan)
        for key in grouped:
            if key not in order:
                order.append(key)
        accounts = [
            await self._account_view(
                plan,
                key,
                grouped.get(key, []),
                earliest,
                is_current=(key == current or (not current and key == order[0])),
            )
            for key in order
        ]
        primary = next((item for item in accounts if item["is_current"]), accounts[0])
        return {
            "key": plan.key,
            "label": plan.label,
            "vendor": plan.vendor,
            "model_prefix": plan.model_prefix,
            "model_prefixes": list(plan.model_prefixes),
            "runtime_ids": list(plan.runtime_ids),
            "official_source": plan.official_source,
            "fallback_model": plan.fallback_model,
            "account_key": primary["key"],
            "gate": primary["gate"],
            "accounts": accounts,
            # 向后兼容：顶层 windows / gate 都是「本机当前账号」的口径
            "windows": primary["windows"],
        }

    async def _account_keys(self, plan: PlanDefinition) -> list[str]:
        """账号展示顺序：本机当前账号 → 本地账本 → 云端其它设备用过的账号。"""
        current = self._accounts.get(plan.key, "")
        earliest = min((window.start() for window in plan.windows), default=None)
        ledger = (
            await self._repo.record_accounts(plan_key=plan.key, since=earliest)
            if earliest is not None
            else []
        )
        order: list[str] = []
        for key in [current, *ledger, *self._cloud_accounts.get(plan.key, [])]:
            if key not in order:
                order.append(key)
        return order or [""]

    async def _account_view(
        self,
        plan: PlanDefinition,
        account_key: str,
        rows: list[dict[str, Any]],
        earliest: datetime | None,
        *,
        is_current: bool,
    ) -> dict[str, Any]:
        """单个账号的窗口视图；`rows` 只包含该账号的官方快照。"""
        official = {str(row.get("window_key")): row for row in rows}
        # 同一账号只要有一次采集成功，比它更早的错误快照就不再代表当前状态：
        # 例如 Free 版 Codex 只有月度窗口，旧版本写下的 5h/周 报错不应一直挂着。
        latest_ok = max(
            (
                moment
                for row in rows
                if not row.get("error") and row.get("percent") is not None
                for moment in (_row_time(row.get("fetched_at")),)
                if moment is not None
            ),
            default=None,
        )
        flux_times = (
            await self._repo.record_times(
                plan_key=plan.key, since=earliest, account_key=account_key
            )
            if earliest is not None
            else []
        )
        windows: list[dict[str, Any]] = []
        for window in plan.windows:
            official_row = official.get(window.key)
            if official_row is not None and official_row.get("error") and latest_ok is not None:
                fetched = _row_time(official_row.get("fetched_at"))
                if fetched is not None and fetched < latest_ok:
                    official_row = None
            windows.append(
                {
                    "key": window.key,
                    "label": window.label,
                    "since": window.start().isoformat(),
                    "local": await self._repo.totals(
                        plan_key=plan.key, since=window.start(), account_key=account_key
                    ),
                    "official": official_row,
                    #: 云端「全部设备」合计（未启用归集或还没同步时为 None）
                    "cloud": self._cloud.get(self._cloud_key(plan.key, account_key, window.key)),
                    "external": await self._external_view(
                        plan.key,
                        window.key,
                        window.start(),
                        flux_times,
                        account_key=account_key,
                    ),
                }
            )
        decision = await self._gate_for_plan(plan, rows, account_key=account_key)
        return {
            "key": account_key,
            "label": account_label(account_key),
            "is_current": is_current,
            "gate": decision.to_dict(),
            "windows": windows,
        }

    async def _external_view(
        self,
        plan_key: str,
        window_key: str,
        since: datetime,
        flux_times: list[datetime],
        *,
        account_key: str = "",
    ) -> dict[str, Any]:
        """官方百分比差分：估算 Flux 之外的客户端消耗（如 Trae）。

        官方接口只给整数百分比、没有任务归因，所以这里只能给出「至少有多少个
        百分点不是 Flux 花掉的」：区间内 Flux 没有任何调用时，官方百分比的增长
        全部归给外部客户端；Flux 同时在跑的区间无法拆分，单独记为 mixed。
        窗口重置（resets_at 变化或百分比回落）时清空重算，不把重置当用量。
        """
        snaps = await self._repo.snapshots_since(
            plan_key=plan_key,
            window_key=window_key,
            since=since,
            account_key=account_key,
        )
        points = mixed = increase = 0.0
        if len(snaps) >= 2:
            times = [_as_utc(value) for value in flux_times]
            previous = snaps[0]
            for current in snaps[1:]:
                baseline = previous["percent"]
                delta = current["percent"] - baseline
                reset = current["resets_at"] != previous["resets_at"] or delta < 0
                if reset:
                    points = mixed = increase = 0.0
                elif delta > 0:
                    increase += delta
                    start = _as_utc(previous["fetched_at"])
                    end = _as_utc(current["fetched_at"])
                    if any(start < moment <= end for moment in times):
                        mixed += delta
                    else:
                        points += delta
                previous = current
        first = snaps[0]["fetched_at"] if snaps else None
        latest = snaps[-1]["fetched_at"] if snaps else None
        return {
            "snapshots": len(snaps),
            "since": _as_utc(first).isoformat() if first else None,
            "latest_at": _as_utc(latest).isoformat() if latest else None,
            "official_increase": round(increase, 1),
            "points": round(points, 1),
            "mixed_points": round(mixed, 1),
        }

    async def _gate_for_plan(
        self,
        plan: PlanDefinition,
        rows: list[dict[str, Any]],
        *,
        account_key: str = "",
    ) -> GateDecision:
        if not self.enabled:
            return GateDecision(
                plan_key=plan.key,
                level=LEVEL_DISABLED,
                enforce=self.enforce,
                account_key=account_key,
            )
        # 只按本机账号判定：别的账号的额度与本机无关。账号识别不到时退回整套餐口径。
        scoped = (
            [row for row in rows if str(row.get("account_key") or "") == account_key]
            if account_key
            else rows
        )
        percent, window_key = self._max_percent(scoped)
        if percent is None:
            percent, window_key = await self._local_percent(plan, account_key=account_key)
        if percent is None:
            return GateDecision(
                plan_key=plan.key,
                level=LEVEL_UNKNOWN,
                enforce=self.enforce,
                fallback_model=plan.fallback_model,
                account_key=account_key,
                message=f"{plan.label} 没有可用的额度数据（官方采集失败且未配置总额）",
            )
        level = self._level_of(percent)
        if level == LEVEL_OK:
            return GateDecision(
                plan_key=plan.key,
                level=LEVEL_OK,
                percent=round(percent, 2),
                window_key=window_key,
                enforce=self.enforce,
                fallback_model=plan.fallback_model,
                account_key=account_key,
            )
        stage = "用尽" if level == LEVEL_EXCEEDED else "告警"
        message = f"{plan.label} 额度{stage}：{window_key} 已用 {percent:.1f}%"
        if plan.fallback_model:
            message += f"，可降级到 {plan.fallback_model}"
        return GateDecision(
            plan_key=plan.key,
            level=level,
            percent=round(percent, 2),
            window_key=window_key,
            message=message,
            enforce=self.enforce,
            fallback_model=plan.fallback_model,
            account_key=account_key,
        )

    @staticmethod
    def _max_percent(rows: list[dict[str, Any]]) -> tuple[float | None, str]:
        """官方快照里取用量最高的窗口（任一窗口打满即视为超额）。"""
        best: float | None = None
        best_key = ""
        for row in rows:
            if row.get("error"):
                continue
            percent = row.get("percent")
            if percent is None:
                used = row.get("used")
                quota = row.get("quota")
                if used is None or not quota:
                    continue
                percent = float(used) / float(quota) * 100.0
            value = float(percent)
            if best is None or value > best:
                best = value
                best_key = str(row.get("window_key") or "")
        return best, best_key

    async def _local_percent(
        self, plan: PlanDefinition, *, account_key: str = ""
    ) -> tuple[float | None, str]:
        """官方不可用时，用本地月度成本 ÷ 套餐总额兜底。"""
        quota = plan.quota_usd
        if plan.key == PLAN_OPENCODE_GO and self._settings.billing_go_monthly_usd:
            quota = float(self._settings.billing_go_monthly_usd)
        if not quota or quota <= 0:
            return None, ""
        window: WindowSpec | None = plan.window(WINDOW_MONTHLY) or plan.windows[-1]
        if window is None:
            return None, ""
        totals = await self._repo.totals(
            plan_key=plan.key, since=window.start(), account_key=account_key or None
        )
        return float(totals["cost"]) / quota * 100.0, window.key

    def _level_of(self, percent: float) -> str:
        if percent >= float(self._settings.billing_block_percent):
            return LEVEL_EXCEEDED
        if percent >= float(self._settings.billing_alert_percent):
            return LEVEL_WARNING
        return LEVEL_OK

    async def _snapshot_rows(
        self, plan: PlanDefinition, *, refresh: bool = False
    ) -> list[dict[str, Any]]:
        if not self.enabled or plan.key not in self._sources:
            return []
        if refresh:
            rows, _ = await self._fetch_locked(plan)
            # 采集只覆盖本机账号；视图仍需带上其它账号的最近快照
            return self._cache.get(plan.key, rows)
        cached = self._cache.get(plan.key)
        if cached is None:
            stored = await self._repo.latest_snapshots(plan.key)
            if stored:
                self._cache[plan.key] = stored
                self._cache_at[plan.key] = _utcnow()
                cached = stored
        if cached is not None and not self._stale(plan.key):
            return cached
        rows, _ = await self._fetch_locked(plan)
        return rows or cached or []

    def _stale(self, plan_key: str) -> bool:
        if self._refresh_seconds <= 0:
            return True
        last = self._cache_at.get(plan_key)
        if last is None:
            return True
        return (_utcnow() - _as_utc(last)).total_seconds() >= self._refresh_seconds

    async def _fetch_locked(self, plan: PlanDefinition) -> tuple[list[dict[str, Any]], str | None]:
        lock = self._locks.setdefault(plan.key, asyncio.Lock())
        async with lock:
            return await self._fetch_and_store(plan)

    async def _fetch_and_store(
        self, plan: PlanDefinition
    ) -> tuple[list[dict[str, Any]], str | None]:
        """采集一次官方额度并落快照；失败时按窗口各写一条 error 快照。"""
        source = self._sources.get(plan.key)
        account_key = self._accounts.get(plan.key, "")
        now = _utcnow()
        if source is None:
            error = f"未配置采集器：{plan.key}"
            rows: list[dict[str, Any]] = []
            for window in plan.windows:
                await self._store_snapshot(
                    plan,
                    window.key,
                    account_key=account_key,
                    source="",
                    error=error,
                    fetched_at=now,
                )
            self._cache[plan.key] = rows
            self._cache_at[plan.key] = now
            return rows, error
        try:
            result = await source.fetch()
        except Exception as exc:  # 采集器自身缺陷也只降级，不炸调用方
            logger.warning("额度采集异常 plan=%s", plan.key, exc_info=True)
            result = None
            error = f"采集异常：{exc}"
        else:
            error = result.error
        if result is None or error or not result.rows:
            detail = error or "官方未返回任何窗口数据"
            for window in plan.windows:
                await self._store_snapshot(
                    plan,
                    window.key,
                    account_key=account_key,
                    source=plan.official_source,
                    error=detail,
                    fetched_at=now,
                )
            # 失败不覆盖旧的成功缓存，闸门继续用上一次的官方数据
            self._cache_at[plan.key] = now
            return self._cache.get(plan.key, []), detail
        rows = []
        for row in result.rows:
            await self._store_snapshot(
                plan,
                row.window_key,
                account_key=account_key,
                source=result.source,
                used=row.used,
                quota=row.quota,
                percent=row.percent,
                unit=row.unit,
                status=row.status,
                resets_at=row.resets_at,
                fetched_at=now,
            )
            rows.append(
                {
                    "plan_key": plan.key,
                    "account_key": account_key,
                    "window_key": row.window_key,
                    "used": row.used,
                    "quota": row.quota,
                    "percent": row.percent,
                    "unit": row.unit,
                    "status": row.status,
                    "resets_at": row.resets_at.isoformat() if row.resets_at else None,
                    "fetched_at": now.isoformat(),
                    "source": result.source,
                    "error": None,
                }
            )
        self._cache[plan.key] = await self._merge_snapshot_rows(plan, rows, account_key)
        self._cache_at[plan.key] = now
        return rows, None

    async def _merge_snapshot_rows(
        self, plan: PlanDefinition, rows: list[dict[str, Any]], account_key: str
    ) -> list[dict[str, Any]]:
        """本次采集只覆盖本机账号，其它账号沿用库里最近一条快照。

        同一套餐可能有多个订阅账号（换过 key、或以后云端下发别的机器采到的数据），
        采集本机账号时不能把别人的窗口从视图里抹掉。
        """
        if not rows:
            return rows
        merged = list(rows)
        try:
            stored = await self._repo.latest_snapshots(plan.key)
        except Exception:  # 读库失败不影响本次采集结果
            logger.warning("额度快照合并失败 plan=%s", plan.key, exc_info=True)
            return merged
        for row in stored:
            if str(row.get("account_key") or "") == account_key:
                continue
            merged.append(row)
        return merged

    async def _store_snapshot(
        self,
        plan: PlanDefinition,
        window_key: str,
        *,
        account_key: str = "",
        source: str,
        used: float | None = None,
        quota: float | None = None,
        percent: float | None = None,
        unit: str = "percent",
        status: str = "ok",
        resets_at: datetime | None = None,
        error: str | None = None,
        fetched_at: datetime,
    ) -> None:
        try:
            await self._repo.add_snapshot(
                plan_key=plan.key,
                account_key=account_key,
                window_key=window_key,
                used=used,
                quota=quota,
                percent=percent,
                unit=unit,
                status="error" if error else status,
                resets_at=resets_at,
                source=source,
                error=error,
                fetched_at=fetched_at,
            )
        except Exception:  # 快照写不进去不影响本次返回
            logger.warning(
                "额度快照落库失败 plan=%s window=%s", plan.key, window_key, exc_info=True
            )


__all__ = [
    "BillingService",
    "GateDecision",
    "LEVEL_DISABLED",
    "LEVEL_EXCEEDED",
    "LEVEL_OK",
    "LEVEL_UNKNOWN",
    "LEVEL_WARNING",
]
