"""计费与套餐额度 API（成本中心）。

- `GET /billing/plans`           所有套餐额度概览（本地账本 + 官方快照 + 闸门）
- `GET /billing/usage`           单套餐视图（不传 plan 等价于概览）
- `GET /billing/prices`          官方 API 价目表（USD / 1M tokens，用于估算成本）
- `GET /billing/runs/{run_id}`   单个 Run 的逐条用量与汇总
- `POST /billing/refresh`        立即拉取官方额度（页面「刷新」按钮）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/plans")
async def list_plans(
    refresh: bool = Query(False, description="是否立即拉取官方额度"),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    return ok(await container.billing.overview(refresh=refresh))


@router.get("/usage")
async def plan_usage(
    plan: str = Query("", description="套餐 key：opencode-go / agent-plan / opencode"),
    refresh: bool = Query(False, description="是否立即拉取官方额度"),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    # 空 plan 时返回全量概览，省掉前端一次分支判断
    if not plan:
        return ok(await container.billing.overview(refresh=refresh))
    return ok(await container.billing.plan_view(plan, refresh=refresh), metadata={"plan": plan})


@router.get("/prices")
async def list_prices(
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """官方价目表：每百万 token 单价 + 基准日期 + 人民币换算率。"""
    return ok(container.billing.prices())


@router.get("/runs/{run_id}")
async def run_usage(
    run_id: str,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    return ok(await container.billing.run_usage(run_id), metadata={"run_id": run_id})


@router.post("/refresh")
async def refresh_usage(
    plan: str = Query("", description="只刷新指定套餐；空表示全部"),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    result = await container.billing.refresh(plan or None)
    return ok(result, metadata={"plan": plan or None})


__all__ = ["router"]
