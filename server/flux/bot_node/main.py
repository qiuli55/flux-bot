"""Flux Bot 独立服务入口。

只暴露 Bot 相关能力：Bot 调度、群聊、任务执行、记忆与模型配置。
Flux 主服务里的对话、设计工作室、IDE、终端、助理等能力不在这里挂载，
两边共用同一份执行内核（agent_runtime / task_engine / mcp），但产品边界分开。

启动：uvicorn flux.bot_node.main:app --host 0.0.0.0 --port 8600
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from flux.api.auth import require_rest_auth
from flux.api.errors import register_exception_handlers
from flux.api.internal_tap import router as llm_tap_router
from flux.api.v1 import (
    agents,
    bots,
    capabilities,
    codex_account,
    connectors,
    deliveries,
    dsh,
    groups,
    health,
    keys,
    memory,
    models,
    tasks,
    uploads,
)
from flux.config import Settings, get_settings
from flux.container import Container
from flux.core.agent_runtime.llm_stream_tap import llm_stream_tap
from flux.core.mcp.server import router as mcp_router
from flux.logging import configure_logging
from flux.version import VERSION

# Bot 能用的业务路由。顺序只影响 OpenAPI 里的排列，不改变行为。
_BOT_ROUTERS = (
    health,
    agents,
    bots,
    groups,
    tasks,
    deliveries,
    connectors,
    memory,
    models,
    keys,
    dsh,
    capabilities,
    uploads,
    codex_account,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    configure_logging(app_settings.log_level, app_settings.log_dir_path)
    container = Container(app_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Bot 开工要认 agent 档案与令牌，启动时先重建内存注册表。
        await container.agents.load_from_db()
        await container.agent_tokens.migrate_legacy_tokens()
        # 上一轮进程被杀时中断的工作区 Apply 需要按磁盘事实恢复，否则 Bot 的改动会卡在 applying。
        await container.workspace.recover_interrupted_applies()
        if app_settings.dsh_enabled:
            # 收摊孤儿 Run，再拉起心跳/对账循环：Bot 的定时任务全靠它推进。
            await container.dsh.supervisor.startup_recovery()
            await container.task_runs.reconcile_orphan_tasks()
            container.dsh.supervisor.ensure_background()
        # Bot 调度：轮询 bots.next_run_at，到期且空闲就开新任务。关掉后仍可手动触发。
        if app_settings.bot_scheduler_enabled:
            await container.bots.start()
        yield
        # 调度循环先停：定时开工要写 tasks / bots，必须在 dispose 之前收尾。
        await container.bots.stop()
        await container.dispose()
        await llm_stream_tap.aclose()

    app = FastAPI(
        title="Flux Bot",
        version=VERSION,
        description="Flux Bot 独立服务（Bot 调度 + 群聊 + 任务执行）",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.container = container
    register_exception_handlers(app)
    for module in _BOT_ROUTERS:
        app.include_router(
            module.router,
            prefix=app_settings.api_v1_prefix,
            dependencies=[Depends(require_rest_auth)],
        )
    # MCP 能力面：给 agent 用的协议端点，与 REST 同进程同生命周期。
    app.include_router(mcp_router)
    # 内置 Agent 的 token 流式旁路：只给本机 DSH 子进程用。
    app.include_router(llm_tap_router)
    return app


app = create_app()
