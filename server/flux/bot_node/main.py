"""Flux Bot 独立服务入口。

只暴露 Bot 相关能力：Bot 调度、群聊、任务执行、记忆与模型配置。
Flux 主服务里的对话、设计工作室、IDE、终端、助理等能力不在这里挂载，
两边共用同一份执行内核（agent_runtime / task_engine / mcp），但产品边界分开。

启动：uvicorn flux.bot_node.main:app --host 0.0.0.0 --port 8600
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import urlsplit

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
from flux.logging import configure_logging, get_logger
from flux.version import VERSION

logger = get_logger(__name__)

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


def resolve_bot_settings(settings: Settings) -> Settings:
    """Bot 节点的生效配置：MCP 端点默认跟随监听端口，鉴权默认 fail-closed。

    `Settings.dsh_mcp_url` 的默认值属于原始 Flux 主应用（8000），Bot 节点监听
    8600；若直接把默认值注入 DSH，模型请求与流式旁路都会打到无人监听的端口。
    只有显式配置的 FLUX_DSH_MCP_URL（含 .env / 构造参数）才覆盖推导结果。

    `allow_no_auth` 未显式配置时在 Bot 节点收敛为 False：未设置 FLUX_AUTH_TOKEN
    时只放行同机回环直连，避免把可执行命令的 REST 面直接暴露给整个网络。
    Flux 主应用不走这里，继续保留 None（旧行为）。
    """
    updates: dict[str, object] = {}
    if "dsh_mcp_url" not in settings.model_fields_set:
        updates["dsh_mcp_url"] = f"http://127.0.0.1:{settings.bot_port}/mcp"
    if "allow_no_auth" not in settings.model_fields_set:
        updates["allow_no_auth"] = False
    return settings.model_copy(update=updates) if updates else settings


def _url_port(url: str) -> int | None:
    """取 URL 的显式端口；未写端口时按 scheme 补默认值。解析失败返回 None。"""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    if parsed.port is not None:
        return parsed.port
    if parsed.scheme == "http":
        return 80
    if parsed.scheme == "https":
        return 443
    return None


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = resolve_bot_settings(settings or get_settings())
    configure_logging(app_settings.log_level, app_settings.log_dir_path)
    container = Container(app_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # 鉴权是公网暴露前的最后一道闸：没有令牌时必须让运维一眼看到当前生效模式。
        if not (app_settings.auth_token or "").strip():
            if app_settings.allow_no_auth:
                logger.warning(
                    "鉴权已显式关闭（FLUX_ALLOW_NO_AUTH=true，未设置 FLUX_AUTH_TOKEN）："
                    "REST 接口对所有网络开放，任何能访问端口的人都能创建 Bot / 触发任务。"
                    "仅限完全可信的内网，公网部署必须设置 FLUX_AUTH_TOKEN。"
                )
            else:
                logger.warning(
                    "未设置 FLUX_AUTH_TOKEN：Bot 节点仅放行本机回环直连（无转发头），"
                    "其他来源一律 401。请在 .env 设置 FLUX_AUTH_TOKEN；"
                    "若部署在完全可信的内网，可显式设置 FLUX_ALLOW_NO_AUTH=true。"
                )
        # MCP 端点与 Bot 监听端口不一致时，DSH 的模型请求与流式旁路会打到死端口。
        # 这里只告警不阻断：测试/单机排查可能有意让二者不同。
        mcp_port = _url_port(app_settings.dsh_mcp_url)
        if mcp_port is not None and mcp_port != app_settings.bot_port:
            logger.warning(
                "DSH MCP URL 端口 %s 与 Bot 监听端口 FLUX_BOT_PORT=%s 不一致"
                "（FLUX_DSH_MCP_URL=%s）；请确认二者指向同一实例",
                mcp_port,
                app_settings.bot_port,
                app_settings.dsh_mcp_url,
            )
        # Bot 开工要认 agent 档案与令牌，启动时先重建内存注册表。
        await container.agents.load_from_db()
        await container.agent_tokens.migrate_legacy_tokens()
        # 单进程部署：上一进程遗留的 Bot 对话 turn 不会自动续跑，统一收成 interrupted。
        await container.bot_conversation.recover_interrupted()
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
            # 健康探针免鉴权：Dockerfile 的 HEALTHCHECK 只带 curl，不能依赖令牌。
            dependencies=[] if module is health else [Depends(require_rest_auth)],
        )
    # MCP 能力面：给 agent 用的协议端点，与 REST 同进程同生命周期。
    app.include_router(mcp_router)
    # 内置 Agent 的 token 流式旁路：只给本机 DSH 子进程用。
    app.include_router(llm_tap_router)
    return app


app = create_app()
