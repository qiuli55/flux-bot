"""API v1 路由聚合（主规格 §12）。"""

from fastapi import APIRouter

from flux.api.v1 import (
    agents,
    billing,
    bots,
    capabilities,
    codex_account,
    connectors,
    deliveries,
    designs,
    desktop,
    devices,
    dsh,
    git,
    groups,
    health,
    ide,
    installations,
    keys,
    memory,
    models,
    projects,
    skills,
    supervisor,
    tasks,
    terminal,
    uploads,
    user_memory,
    workspace,
)

api_v1_router = APIRouter()
api_v1_router.include_router(health.router)
api_v1_router.include_router(agents.router)
api_v1_router.include_router(billing.router)
api_v1_router.include_router(keys.router)
api_v1_router.include_router(installations.router)
api_v1_router.include_router(tasks.router)
api_v1_router.include_router(bots.router)
api_v1_router.include_router(desktop.router)
api_v1_router.include_router(devices.router)
api_v1_router.include_router(groups.router)
api_v1_router.include_router(workspace.router)
api_v1_router.include_router(terminal.router)
api_v1_router.include_router(ide.router)
api_v1_router.include_router(git.router)
api_v1_router.include_router(projects.router)
api_v1_router.include_router(memory.router)
api_v1_router.include_router(models.router)
api_v1_router.include_router(connectors.router)
api_v1_router.include_router(dsh.router)
api_v1_router.include_router(capabilities.router)
api_v1_router.include_router(designs.router)
api_v1_router.include_router(deliveries.router)
api_v1_router.include_router(uploads.router)
api_v1_router.include_router(user_memory.router)
api_v1_router.include_router(skills.router)
api_v1_router.include_router(skills.bot_router)
api_v1_router.include_router(codex_account.router)
api_v1_router.include_router(supervisor.router)

__all__ = ["api_v1_router"]
