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
from flux.api.v1 import (
    agents,
    billing,
    bot_subtasks,
    bots,
    capabilities,
    channels_dingtalk,
    channels_feishu,
    channels_wecom,
    codex_account,
    connector_calendar,
    connectors,
    deliveries,
    desktop,
    devices,
    groups,
    health,
    hooks,
    installations,
    keys,
    memory,
    models,
    outbound_drafts,
    pages,
    public_pages,
    skills,
    tasks,
    uploads,
    user_memory,
    vault,
)
from flux.config import Settings, get_settings
from flux.container import Container
from flux.core.mcp.server import router as mcp_router
from flux.errors import ValidationError
from flux.logging import configure_logging, get_logger
from flux.version import VERSION

logger = get_logger(__name__)

# Bot 能用的业务路由。顺序与 flux.main 的 api_v1_router 保持一致（跳过 IDE 专属模块），
# 只影响 OpenAPI 里的排列，不改变行为。
# 有意排除的 IDE / 桌面工作台路由：projects / git / ide / terminal / workspace /
# designs / supervisor——Bot 节点不提供这些产品能力。
_BOT_ROUTERS = (
    health.router,
    agents.router,
    billing.router,
    keys.router,
    installations.router,
    tasks.router,
    bots.router,
    outbound_drafts.router,
    bot_subtasks.router,
    desktop.router,
    devices.router,
    groups.router,
    memory.router,
    models.router,
    connector_calendar.router,
    pages.router,
    connectors.router,
    capabilities.router,
    deliveries.router,
    uploads.router,
    user_memory.router,
    skills.router,
    # 分配到 Bot 的技能接口（/bots/{bot_id}/skills）与技能包本体同属 skills 模块。
    skills.bot_router,
    codex_account.router,
    vault.router,
)

#: 免 REST 鉴权的公开入口：Webhook 用 HMAC 签名自证身份，不能套本机令牌；
#: 制品页面分享链接面向没有登录态的外部读者，只按不可猜测的 share_token 读取。
_BOT_PUBLIC_ROUTERS = (hooks, public_pages)


def resolve_bot_settings(settings: Settings) -> Settings:
    """Bot 节点的生效配置：MCP 端点默认跟随监听端口，鉴权默认 fail-closed。

    `Settings.mcp_url` 的默认值属于原始 Flux 主应用（8000），Bot 节点监听
    8600；若直接把默认值注入 Claude Code 的 MCP 配置，工具调用会打到无人监听的
    端口。只有显式配置的 FLUX_MCP_URL（含 .env / 构造参数）才覆盖推导结果。

    `allow_no_auth` 未显式配置时在 Bot 节点收敛为 False：未设置 FLUX_AUTH_TOKEN
    时只放行同机回环直连，避免把可执行命令的 REST 面直接暴露给整个网络。
    Flux 主应用不走这里，继续保留 None（旧行为）。
    """
    updates: dict[str, object] = {}
    if "mcp_url" not in settings.model_fields_set:
        updates["mcp_url"] = f"http://127.0.0.1:{settings.bot_port}/mcp"
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


#: Bot 节点的底座 Agent；其 installation 推进到 READY 后，/bots/{id}/run 才不会 409。
_BOT_AGENT_NAME = "claude-code"


async def auto_connect_claude_code(container: Container, settings: Settings) -> None:
    """按开关在启动时自动接入 Claude Code（等价于 `flux agents connect claude-code`）。

    复用 InstallationService.connect：它内部会重新探测本机 CLI，只有确实找到
    `claude` 可执行文件才允许把状态推进到 READY。未安装时 connect 抛
    ValidationError，这里只记 WARNING 并返回——自动接入失败绝不能阻断 Bot 节点启动。
    """
    if not settings.auto_connect_claude_code:
        return
    try:
        row = await container.installations.connect(_BOT_AGENT_NAME)
    except ValidationError:
        logger.warning(
            "FLUX_AUTO_CONNECT_CLAUDE_CODE 已开启，但本机未找到 claude CLI"
            "（%s 未安装）；跳过自动接入，POST /bots/{id}/run 仍会返回 409。"
            "请确认镜像内已安装 claude，或手动执行 flux agents connect %s。",
            _BOT_AGENT_NAME,
            _BOT_AGENT_NAME,
        )
        return
    logger.info(
        "已自动接入 %s：installation 状态 %s（等价于 flux agents connect %s）",
        _BOT_AGENT_NAME,
        row.status,
        _BOT_AGENT_NAME,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = resolve_bot_settings(settings or get_settings())
    configure_logging(app_settings.log_level, app_settings.log_dir_path)
    container = Container(app_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # 事件总线先起：Redis 后端在此订阅跨进程频道；连接失败只降级并告警。
        await container.bus.start()
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
        # MCP 端点与 Bot 监听端口不一致时，Agent 的工具调用会打到死端口。
        # 这里只告警不阻断：测试/单机排查可能有意让二者不同。
        mcp_port = _url_port(app_settings.mcp_url)
        if mcp_port is not None and mcp_port != app_settings.bot_port:
            logger.warning(
                "MCP URL 端口 %s 与 Bot 监听端口 FLUX_BOT_PORT=%s 不一致"
                "（FLUX_MCP_URL=%s）；请确认二者指向同一实例",
                mcp_port,
                app_settings.bot_port,
                app_settings.mcp_url,
            )
        # Bot 开工要认 agent 档案与令牌，启动时先重建内存注册表。
        await container.agents.load_from_db()
        await container.agent_tokens.migrate_legacy_tokens()
        # 可选：启动时自动把 claude-code 接入状态推进到 READY（默认关闭）。
        await auto_connect_claude_code(container, app_settings)
        # 单进程部署：上一进程遗留的 Bot 对话 turn 不会自动续跑，统一收成 interrupted。
        await container.bot_conversation.recover_interrupted()
        # 上一轮进程被杀时中断的工作区 Apply 需要按磁盘事实恢复，否则 Bot 的改动会卡在 applying。
        await container.workspace.recover_interrupted_applies()
        # 收摊孤儿 Run，再拉起心跳/对账循环：Bot 的定时任务全靠它推进。
        await container.run_supervisor.startup_recovery()
        await container.task_runs.reconcile_orphan_tasks()
        container.run_supervisor.ensure_background()
        # Bot 调度：轮询 bots.next_run_at，到期且空闲就开新任务。关掉后仍可手动触发。
        if app_settings.bot_scheduler_enabled:
            await container.bots.start()
        yield
        # 调度循环先停：定时开工要写 tasks / bots，必须在 dispose 之前收尾。
        await container.bots.stop()
        await container.dispose()

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
    for router in _BOT_ROUTERS:
        app.include_router(
            router,
            prefix=app_settings.api_v1_prefix,
            # 健康探针免鉴权：Dockerfile 的 HEALTHCHECK 只带 curl，不能依赖令牌。
            dependencies=[] if router is health.router else [Depends(require_rest_auth)],
        )
    # 外部 Webhook：HMAC 验签自证身份，与主应用同一套入口。
    for module in _BOT_PUBLIC_ROUTERS:
        app.include_router(module.router, prefix=app_settings.api_v1_prefix)
    # 正式飞书频道回调：平台服务器没有本机令牌，免 REST 鉴权；安全性由
    # verification token / Encrypt Key 签名把关（未配置时 fail-closed）。
    app.include_router(channels_feishu.router, prefix=app_settings.api_v1_prefix)
    # 正式钉钉频道回调：同样免 REST 鉴权，安全性由签名 token / EncodingAESKey 把关
    # （未配置时 fail-closed）；出站回复仍必须经审批卡放行。
    app.include_router(channels_dingtalk.router, prefix=app_settings.api_v1_prefix)
    # 正式企业微信频道回调：同样免 REST 鉴权，安全性由 token / EncodingAESKey 把关。
    app.include_router(channels_wecom.router, prefix=app_settings.api_v1_prefix)
    # MCP 能力面：给 agent 用的协议端点，与 REST 同进程同生命周期。
    app.include_router(mcp_router)
    return app


app = create_app()
