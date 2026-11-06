"""计费与套餐额度（本地账本 + 官方快照对账）。

模块边界：
- `plans`      套餐定义、模型前缀归因、窗口起点计算（纯函数，无 IO）
- `pricing`    官方 API 价目表（每百万 token）与费用估算
- `repository` usage_records / usage_snapshots 的读写（SQLAlchemy）
- `sources`    官方额度采集器（OpenCode Go usage 接口、火山 arkcli、自定义 HTTP/CLI）
- `service`    编排：落账、窗口聚合、对账快照、闸门判定

设计口径：本地账本是唯一能做「任务级归因」的来源；官方接口只给总量，
因此官方快照只用于对账与闸门，不反推单个任务的花费。
"""

from flux.core.billing.plans import (
    PLAN_AGENT_PLAN,
    PLAN_CLAUDE,
    PLAN_CODEX,
    PLAN_OPENCODE_GO,
    PLAN_OPENCODE_ZEN,
    PLANS,
    WINDOW_DAILY,
    WINDOW_MONTHLY,
    WINDOW_ROLLING_5H,
    WINDOW_WEEKLY,
    PlanDefinition,
    SourceSpec,
    WindowSpec,
    default_plans,
    load_plan_registry,
    resolve_plan,
)
from flux.core.billing.pricing import PriceBook, PriceEntry, load_price_book
from flux.core.billing.repository import BillingRepository
from flux.core.billing.service import BillingService, GateDecision

__all__ = [
    "PLANS",
    "PLAN_AGENT_PLAN",
    "PLAN_CLAUDE",
    "PLAN_CODEX",
    "PLAN_OPENCODE_GO",
    "PLAN_OPENCODE_ZEN",
    "WINDOW_DAILY",
    "WINDOW_MONTHLY",
    "WINDOW_ROLLING_5H",
    "WINDOW_WEEKLY",
    "BillingRepository",
    "BillingService",
    "GateDecision",
    "PlanDefinition",
    "PriceBook",
    "PriceEntry",
    "SourceSpec",
    "WindowSpec",
    "default_plans",
    "load_plan_registry",
    "load_price_book",
    "resolve_plan",
]
