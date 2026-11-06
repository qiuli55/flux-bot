"""健康检查与就绪探针。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from flux.api.deps import get_container
from flux.api.response import fail, ok
from flux.container import Container
from flux.db.session import check_database

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(container: Container = Depends(get_container)) -> dict[str, object]:
    """存活探针：只表明进程能响应，不检查外部依赖。"""
    return ok(
        {
            "status": "ok",
            "app": container.settings.app_name,
            "env": container.settings.env,
        }
    )


@router.get("/health/ready")
async def ready(container: Container = Depends(get_container)) -> JSONResponse:
    """就绪探针：数据库连通 + 当前可用模型供应商。"""
    database_ok = await check_database(container.engine)
    payload = {
        "database": database_ok,
        "providers": [str(name) for name in container.router.available()],
        "agents": container.agents.count(),
    }
    if not database_ok:
        return JSONResponse(
            status_code=503,
            content=fail("not_ready", "数据库不可用", details=payload),
        )
    return JSONResponse(status_code=200, content=ok(payload))
