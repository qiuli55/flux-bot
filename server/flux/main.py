"""FastAPI 应用工厂。

本地启动：uvicorn flux.main:app --reload --app-dir backend
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from flux.api.auth import require_rest_auth
from flux.api.errors import register_exception_handlers
from flux.api.internal_tap import router as llm_tap_router
from flux.api.v1 import api_v1_router, channels_feishu, hooks, ide, supervisor_webhook, terminal
from flux.api.v1 import cloud as cloud_api
from flux.cloud.uploader import upload_previous_run
from flux.config import Settings, get_settings
from flux.container import Container
from flux.core.agent_runtime.llm_stream_tap import llm_stream_tap
from flux.core.mcp.server import router as mcp_router
from flux.logging import configure_logging
from flux.version import VERSION


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    configure_logging(app_settings.log_level, app_settings.log_dir_path)
    container = Container(app_settings)
    log_upload_task: asyncio.Task[None] | None = None
    memory_sync_task: asyncio.Task[None] | None = None
    billing_sync_task: asyncio.Task[None] | None = None
    context_sync_task: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal log_upload_task, memory_sync_task, billing_sync_task, context_sync_task
        # P3-16：Agent 档案的权威在 agents 表，启动时重建内存注册表（canonical UUID 不因重启而变），
        # 并把历史"自由字符串"令牌迁移到 canonical id 上。
        await container.agents.load_from_db()
        await container.agent_tokens.migrate_legacy_tokens()
        # Bot 对话 turn 不跨进程续跑：启动时把遗留的 queued/running 标记为 interrupted。
        await container.bot_conversation.recover_interrupted()
        # P0-1：对账上一轮进程被杀时中断的 Apply（按磁盘事实恢复，幂等），
        # 与 Run 的 startup_recovery 并列——崩在落盘中途的提案不能永远停在 applying。
        await container.workspace.recover_interrupted_applies()
        await container.human_pty.recover_orphaned_sessions()
        # FINDING-2：上一进程被 SIGKILL 后，命令进程组 id 还留在库里——据此核实并回收
        # 遗留的孤儿命令进程（比对 /proc cmdline 才动手，避免误杀复用 id 的无关进程）。
        await container.terminal.recover_orphan_commands()
        if app_settings.dsh_enabled:
            # P2-15 §2.7：对账上一轮遗留的非终态 Run（属主已死的孤儿进程一并清理），
            # 再拉起心跳/对账循环——否则重启后卡死的 Run 永远停在 running。
            recovered_runs = await container.dsh.supervisor.startup_recovery()
            # FINDING-1：对账出来的终态 Run 名下若有派生终端会话，一并收摊（幂等；事件订阅
            # 也会触发一次，这里显式补一遍以覆盖订阅时序边界）。
            for run_id in recovered_runs:
                await container.terminal.stop_run_sessions(run_id)
            # P2-04 / TC-502：Run 对完账后收尾"没有活 Run 支撑的 running 任务"，
            # 崩溃遗留的僵尸任务在重启后立刻退回待执行或落终态，不再永久 running。
            await container.task_runs.reconcile_orphan_tasks()
            container.dsh.supervisor.ensure_background()
        # 自动上传「上一轮运行日志」到账号云：后台跑，不阻塞启动，失败只记日志。
        # log_dir 未配置时整体关闭（测试与本地零配置不触发）。
        if app_settings.log_dir_path is not None:
            log_upload_task = asyncio.create_task(upload_previous_run(app_settings))
        # 账号级用户记忆：启动先拉一次云端快照，之后每 15 分钟对账；未配置设备凭据时
        # 循环内部直接跳过，不产生任何网络请求。
        if app_settings.memory_cloud_sync:
            memory_sync_task = asyncio.create_task(container.memory_cloud.start())
        # 跨设备套餐用量归集：增量上传本机账本 + 拉取「全部设备」合计；未配置设备
        # 凭据时循环内部直接跳过，不产生任何网络请求。
        if app_settings.billing_cloud_sync:
            billing_sync_task = asyncio.create_task(container.billing.start_cloud_sync())
        # 对话上下文上云：隐私输入以外的消息增量备份到账号云；未配置设备凭据时
        # 循环内部直接跳过，不产生任何网络请求。
        if app_settings.context_cloud_sync:
            context_sync_task = asyncio.create_task(container.context_cloud.start())
        # 助理（Supervisor）后台判断：消费 outbox → Gate → Assessment；观察模式只记判断，
        # 不派活。没有事件进入就永远空转等待，不产生任何副作用。
        if app_settings.supervisor_worker_enabled:
            await container.supervisor_worker.start()
        # 知识库后台索引：对话 / docs / 项目记忆增量入库，供助理 assistant.search 检索。
        # 默认每 5 分钟一轮；测试与本地零配置可用 knowledge_enabled / worker 开关关掉。
        if app_settings.knowledge_enabled and app_settings.knowledge_worker_enabled:
            await container.knowledge_worker.start()
        # Bot 模式：定时轮询 bots.next_run_at（落库），到期且空闲就开一条新任务。
        # 关掉时 Bot 仍可手动 /run，只是不再自动触发。
        if app_settings.bot_scheduler_enabled:
            await container.bots.start()
        yield
        if log_upload_task is not None and not log_upload_task.done():
            log_upload_task.cancel()
        if memory_sync_task is not None and not memory_sync_task.done():
            memory_sync_task.cancel()
        if billing_sync_task is not None and not billing_sync_task.done():
            billing_sync_task.cancel()
        if context_sync_task is not None and not context_sync_task.done():
            context_sync_task.cancel()
        # 先停助理 worker，再 dispose 容器：避免停库后仍有判断在写 outbox。
        await container.supervisor_worker.stop()
        # 知识库索引同样先停：索引事务不能在引擎 dispose 之后还在写。
        await container.knowledge_worker.stop()
        # Bot 调度循环先停：定时开工要写 tasks / bots，必须在 dispose 之前收尾。
        await container.bots.stop()
        # Container.dispose() owns service shutdown ordering, including Human PTY.
        await container.dispose()
        # 内部旁路的上游连接池随应用一起关闭
        await llm_stream_tap.aclose()

    app = FastAPI(
        title=app_settings.app_name,
        version=VERSION,
        description="Flux 后端 API（主规格 §12）",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.container = container
    register_exception_handlers(app)
    app.include_router(
        api_v1_router,
        prefix=app_settings.api_v1_prefix,
        dependencies=[Depends(require_rest_auth)],
    )
    # 账号云接入面单独挂载（不经 require_rest_auth）：浏览器在未登录时也要能读到
    # 登录入口、并用一次性 code 换本机会话——这正是"先有鸡还是先有蛋"的那一步。
    app.include_router(cloud_api.router, prefix=app_settings.api_v1_prefix)
    # 助理的平台回调（飞书事件订阅）同样免 REST 鉴权：平台服务器没有本机令牌，
    # 安全性由回调体里的 verification token 把关（未配置时 fail-closed）。
    app.include_router(supervisor_webhook.router, prefix=app_settings.api_v1_prefix)
    # 外部例程触发（CI / GitHub / 自建 Webhook）同样免 REST 鉴权：
    # 安全性由 HMAC 签名把关，密钥未配置时 fail-closed。
    app.include_router(hooks.router, prefix=app_settings.api_v1_prefix)
    # 正式 Bot 飞书频道：平台回调同样免本机 REST 令牌，靠 verification token /
    # Encrypt Key 签名 / 解密把关；出站仍由审批卡控制。
    app.include_router(channels_feishu.router, prefix=app_settings.api_v1_prefix)
    # WebSocket 不能复用 Request + HTTPBearer 的 REST 依赖；Human PTY 自己执行
    # same-origin + token/cookie 鉴权，并单独挂到应用，避免依赖注入在 WS 上失效。
    app.include_router(
        terminal.websocket_router,
        prefix=app_settings.api_v1_prefix,
    )
    # IDE 代码智能（LSP 网关）：同样是 WS，鉴权与 terminal 复用同一套
    # same-origin + token/cookie 检查，故与 REST 路由分开挂载。
    app.include_router(
        ide.websocket_router,
        prefix=app_settings.api_v1_prefix,
    )
    # MCP 能力面（目标架构 §3.1）：与 REST 同进程同生命周期，但不挂在 /api/v1 下——
    # 它是给 agent 用的协议端点，不是给前端用的业务接口。
    app.include_router(mcp_router)
    # 内置 Agent 的 token 流式旁路：只给本机 DSH 子进程用，无 REST 鉴权（回环 + 一次性 token）
    app.include_router(llm_tap_router)
    return app


app = create_app()
