"""Supervisor（助理）API：事件入口、观察回放、手动重判、编排动作、设置。

权限面与其它 v1 接口一致，统一由 `require_rest_auth` 把关（见 flux.main）。
本组接口只写"事实"：事件入库、判断追加、工单落库、派发记录；执行仍归 Task Engine。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.schemas.api import (
    SupervisorActionRequest,
    SupervisorEventRequest,
    SupervisorSettingsRequest,
    SupervisorSubscriptionRequest,
)

router = APIRouter(prefix="/supervisor", tags=["supervisor"])


@router.post("/events")
async def ingest_event(
    payload: SupervisorEventRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """记录一条外部事件（幂等）。重复投递只回同一条记录，metadata.created=false。"""
    data = await container.supervisor.ingest_event(
        source_key=payload.source_key,
        external_id=payload.external_id,
        event_type=payload.event_type,
        actor_id=payload.actor_id,
        actor_platform=payload.actor_platform,
        thread_key=payload.thread_key,
        content=payload.content,
        raw=payload.raw,
        project_id=payload.project_id,
    )
    return ok(data["trigger"], metadata={"created": data["created"]})


@router.get("/observations")
async def list_observations(
    limit: int = Query(default=50, ge=1, le=200),
    status: str | None = Query(default=None, description="按 trigger.status 过滤"),
    project_id: uuid.UUID | None = Query(default=None),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """观察模式回放：事件按时间倒序，带最近一次判断（若已判断）。"""
    data = await container.supervisor.observations(
        limit=limit, status=status, project_id=project_id
    )
    return ok(
        data["items"],
        metadata={
            "count": len(data["items"]),
            "pending_outbox": data["pending_outbox"],
        },
    )


@router.get("/observations/{trigger_id}")
async def read_observation(
    trigger_id: uuid.UUID,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """单条事件的完整判断历史（旧结论不会被覆盖）。"""
    return ok(await container.supervisor.observation_detail(trigger_id))


@router.post("/observations/{trigger_id}/assess")
async def reassess_observation(
    trigger_id: uuid.UUID,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """人工重判：追加一条判断并推进状态；观察模式下不会派活。"""
    data = await container.supervisor.assess_now(trigger_id)
    assessment = data.get("assessment") or {}
    return ok(data, metadata={"decision": assessment.get("decision")})


@router.post("/observations/{trigger_id}/plan")
async def plan_observation(
    trigger_id: uuid.UUID,
    dispatch: bool = Query(default=False, description="是否同时派发可执行工单"),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """为一条事件产出工单树；`dispatch=false` 时只落库不派活。"""
    data = await container.supervisor.plan_now(trigger_id, dispatch=dispatch)
    return ok(
        data,
        metadata={
            "tickets": len(data["tickets"]),
            "degraded": data["plan"]["degraded"],
        },
    )


@router.get("/overview")
async def supervisor_overview(
    limit: int = Query(default=20, ge=1, le=200),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """助理看板：设置快照、阶段计数、最近工单。"""
    return ok(await container.supervisor.overview(limit=limit))


@router.get("/projects/{project_id}")
async def read_supervisor_project(
    project_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """项目页：项目信息、阶段计数、工单、最近报告、线程摘要与时间线。"""
    data = await container.supervisor.project_overview(project_id, limit=limit)
    return ok(
        data,
        metadata={"tickets": len(data["tickets"]), "digests": len(data["digests"])},
    )


@router.get("/search")
async def search_supervisor(
    q: str = Query(min_length=1, max_length=200, description="检索关键字"),
    limit: int = Query(default=20, ge=1, le=200),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """在工单、报告、线程摘要里检索；`%` / `_` 按字面量处理。"""
    items = await container.supervisor.search(q, limit=limit)
    return ok(items, metadata={"count": len(items), "query": q})


@router.post("/watchdog")
async def run_supervisor_watchdog(
    limit: int = Query(default=50, ge=1, le=200),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """手动跑一轮超时回收：卡住的在途派活被收尾并写 `timed_out` 报告。"""
    result = await container.supervisor.watchdog(limit=limit)
    return ok(result, metadata={"checked": result["checked"], "expired": result["expired"]})


@router.get("/tickets")
async def list_supervisor_tickets(
    thread_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """工单列表；阶段由 task / assignment / report 事实派生，不读工单状态列。"""
    items = await container.supervisor.tickets(thread_id=thread_id, limit=limit)
    return ok(items, metadata={"count": len(items)})


@router.get("/tickets/{ticket_id}")
async def read_supervisor_ticket(
    ticket_id: uuid.UUID,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """单张工单的详情：阶段、最近派活、全部报告。"""
    return ok(await container.supervisor.ticket_detail(ticket_id))


@router.get("/reports")
async def list_supervisor_reports(
    limit: int = Query(default=50, ge=1, le=200),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """最近的报告（时间倒序）；只读，供助理看板与外部助手查询。"""
    items = await container.supervisor.reports(limit=limit)
    return ok(items, metadata={"count": len(items)})


@router.get("/timeline")
async def read_supervisor_timeline(
    thread_id: uuid.UUID | None = Query(default=None),
    project_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """把事件、工单、报告合成一条时间线（时间倒序），只读不写。"""
    items = await container.supervisor.timeline(
        thread_id=thread_id, project_id=project_id, limit=limit
    )
    return ok(items, metadata={"count": len(items)})


@router.get("/tickets/{ticket_id}/subscriptions")
async def list_supervisor_subscriptions(
    ticket_id: uuid.UUID,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """工单的订阅列表：报告落库后直通投递的目标会话。"""
    items = await container.supervisor.list_subscriptions(ticket_id)
    return ok(items, metadata={"count": len(items)})


@router.post("/tickets/{ticket_id}/subscriptions")
async def create_supervisor_subscription(
    ticket_id: uuid.UUID,
    payload: SupervisorSubscriptionRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """给工单订阅一个平台会话；同一目标重复提交返回既有订阅。"""
    data = await container.supervisor.create_subscription(
        ticket_id,
        source_key=payload.source_key,
        external_id=payload.external_id,
        mode=payload.mode,
    )
    return ok(data["subscription"], metadata={"created": data["created"]})


@router.delete("/subscriptions/{subscription_id}")
async def delete_supervisor_subscription(
    subscription_id: uuid.UUID,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """取消订阅：之后该会话不再收到这张工单的报告。"""
    await container.supervisor.delete_subscription(subscription_id)
    return ok({"deleted": True}, metadata={"subscription_id": str(subscription_id)})


@router.post("/threads/{thread_id}/dispatch")
async def dispatch_supervisor_thread(
    thread_id: uuid.UUID,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """按依赖与并发位派发一个线程下所有可执行工单。"""
    data = await container.supervisor.dispatch_thread(thread_id)
    return ok(data, metadata={"dispatched": data["dispatched"]})


@router.post("/tickets/{ticket_id}/dispatch")
async def dispatch_supervisor_ticket(
    ticket_id: uuid.UUID,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """手动派发单张工单；已有任务记录的工单不会被重复派活。"""
    data = await container.supervisor.dispatch_ticket(ticket_id)
    return ok(data, metadata={"dispatched": data["dispatched"]})


@router.post("/tickets/{ticket_id}/retry")
async def retry_supervisor_ticket(
    ticket_id: uuid.UUID,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """重试：新建一个 Task 与派活记录，旧记录保留供回放。"""
    data = await container.supervisor.retry_ticket(ticket_id)
    return ok(data, metadata={"dispatched": data["dispatched"]})


@router.post("/tickets/{ticket_id}/stop")
async def stop_supervisor_ticket(
    ticket_id: uuid.UUID,
    payload: SupervisorActionRequest | None = None,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """停止工单：能停 Run 就停 Run，再按平台事实写一条 cancelled 报告。"""
    note = payload.note if payload is not None else ""
    return ok(await container.supervisor.stop_ticket(ticket_id, reason=note))


@router.post("/process")
async def process_pending(
    limit: int = Query(default=10, ge=1, le=50),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """手动推进一轮 outbox（与后台 worker 同一条路径，供运维与测试使用）。"""
    return ok(await container.supervisor.process_pending(limit=limit))


@router.get("/settings")
async def read_supervisor_settings(
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """读取准入模式与计划上限。"""
    return ok(await container.supervisor.read_settings())


@router.put("/settings")
async def update_supervisor_settings(
    payload: SupervisorSettingsRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """局部更新设置；字段缺省/为 null 表示保持原值。"""
    fields = payload.model_dump(exclude_none=True, mode="json")
    return ok(await container.supervisor.update_settings(fields))
