"""日历连接器的运行时 API（CalDAV / ICS）。

与 `/connectors/{id}/install` 共用安装记录，但事件读写挂在
`/connectors/calendar` 下；账号标识与 `/connectors` 一致，来自当前登录人。

所有接口都要求先安装；未安装返回 404，密钥库不匹配返回 503（fail-closed），
上游不可达返回 502。响应里不会出现密码或带 userinfo 的完整 URL。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from flux.api.account_scope import account_key_for_request
from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.schemas.api import CalendarEventCreateRequest

router = APIRouter(prefix="/connectors/calendar", tags=["connectors"])


def _account_key(request: Request, container: Container) -> str:
    """账号标识 = 当前登录人（与 /connectors 同一口径）。"""
    return account_key_for_request(request, container.settings)


@router.get("/events")
async def list_calendar_events(
    request: Request,
    start: str | None = Query(default=None, max_length=64),
    end: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=100),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    account_key = _account_key(request, container)
    events = await container.calendar.list_events(
        account_key,
        start=start,
        end=end,
        limit=limit,
    )
    return ok(events)


@router.get("/events/{uid}")
async def get_calendar_event(
    uid: str,
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    account_key = _account_key(request, container)
    return ok(await container.calendar.get_event(account_key, uid))


@router.post("/events")
async def create_calendar_event(
    payload: CalendarEventCreateRequest,
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    account_key = _account_key(request, container)
    created = await container.calendar.create_event(
        account_key,
        title=payload.title,
        start=payload.start,
        end=payload.end,
        description=payload.description or "",
        location=payload.location or "",
    )
    return ok(created)


@router.post("/test")
async def test_calendar_connection(
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    account_key = _account_key(request, container)
    return ok(await container.calendar.test_connection(account_key))
