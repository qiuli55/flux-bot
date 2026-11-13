"""配置系统（主规格 §18.4 配置项 / §18.5 API Key 管理）。"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from flux.core.model_gateway.providers.official_models import (
    DOUBAO_BASE_URL,
    DOUBAO_DEFAULT_MODEL,
    KIMI_BASE_URL,
    KIMI_DEFAULT_MODEL,
    QWEN_BASE_URL,
    QWEN_DEFAULT_MODEL,
    ZHIPU_BASE_URL,
    ZHIPU_DEFAULT_MODEL,
)

# backend/flux/config.py -> backend/flux -> backend -> 仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FLUX_",
        # 固定指向仓库根的 .env：pydantic-settings 默认按进程 cwd 解析相对路径，
        # 而 uvicorn 从仓库根启动、alembic 从 backend/ 启动，若用相对路径两者会
        # 读到不同的配置文件（甚至读不到），导致 make run 与 make migrate 连不同的库。
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="before")
    @classmethod
    def _fallback_timezone_from_tz(cls, data: object) -> object:
        """FLUX_TIMEZONE 未提供时，沿用标准 `TZ` 环境变量（合法 IANA 名称）。"""
        if not isinstance(data, dict):
            return data
        # pydantic-settings 会把显式 env / .env / 构造参数合并进 data；没有该键
        # 就说明 FLUX_TIMEZONE 未设置（或与默认值相同而被剥离）。此时才看 TZ。
        if "timezone" not in data and "FLUX_TIMEZONE" not in os.environ:
            tz = (os.environ.get("TZ") or "").strip().removeprefix(":")
            if tz:
                try:
                    ZoneInfo(tz)
                except (ZoneInfoNotFoundError, ValueError):
                    pass
                else:
                    data = {**data, "timezone": tz}
        return data

    @field_validator("anthropic_auth_mode")
    @classmethod
    def _validate_anthropic_auth_mode(cls, value: str) -> str:
        """非法值不让服务起不来：告警并由解析层统一回退 auto。"""
        # 惰性导入：config 是最底层模块，避免在模块加载期拉起 agent_runtime 包
        from flux.core.agent_runtime.claude_auth import normalize_auth_mode

        return normalize_auth_mode(value)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        """启动即校验时区名；非法值给出明确指向 FLUX_TIMEZONE 的错误。"""
        name = (value or "").strip() or "UTC"
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"FLUX_TIMEZONE 不是合法的 IANA 时区名：{name!r}（例：Asia/Shanghai）"
            ) from exc
        return name

    # --- 应用 ---
    app_name: str = "Flux"
    env: str = "local"
    log_level: str = "INFO"
    api_v1_prefix: str = "/api/v1"
    # Bot 的 cron 表达式与每日 token 预算重置都按这个 IANA 时区解释（如 Asia/Shanghai）。
    # 未设置 FLUX_TIMEZONE 时会回退到标准环境变量 TZ（仅当其值是合法 IANA 名称）；
    # 两者都没有则使用 UTC。非法名称会在启动加载配置时直接报错。
    timezone: str = "UTC"
    # REST 面单用户令牌（公网暴露前置，见 flux.api.auth）。留空 = 关闭鉴权，
    # 本地开发与桌面端（同机直连）零配置可用；公网部署必须显式设置。
    auth_token: str | None = None
    # 未设置 auth_token 时的兜底策略（FLUX_ALLOW_NO_AUTH）：
    #   None  = 旧行为（整体开放，仅 flux.main 保留，兼容既有测试）；
    #   False = 仅允许同机回环直连，其余请求一律 401（Bot 节点默认，fail-closed）；
    #   True  = 显式开放（仅限完全可信的内网，启动时告警）。
    allow_no_auth: bool | None = None
    # Electron 桌面端用于“打开文件夹”的一次运行期能力令牌；普通 Web 客户端不能使用。
    desktop_control_token: str | None = None

    # --- 设备配对（手机长期令牌）---
    # 配对成功的设备令牌有效期（天）。0 = 永不过期。
    device_token_ttl_days: int = 90
    # 一次性配对码有效期（秒）。
    pairing_code_ttl_seconds: int = 600

    # --- 云桌面（按 Bot 隔离的桌面会话）---
    # 桌面网关地址与长期网关令牌。地址 / 令牌的兼容别名见文件末尾「Bot 桌面」段，
    # 统一解析在 flux.core.desktop.resolve；地址齐全且（长期凭据成对存在 或 配置了
    # 票据密钥）时才视为已配置，绝不返回半个连接参数。
    desktop_host: str | None = None
    desktop_gateway_token: str | None = None
    # 桌面网桥地址与密钥（可选，随会话下发给客户端）。
    desktop_bridge: str | None = None
    desktop_bridge_key: str | None = None
    # 桌面票据签名密钥；留空时仍返回会话信息，但 ticket 为 null。
    desktop_ticket_secret: str | None = None
    # 桌面票据有效期（秒）。
    desktop_ticket_ttl_seconds: int = 300
    # 是否把长期 gateway_token / bridge_key 一并下发。网关启用票据校验后可关掉，
    # 让手机端不再持有长期密钥。
    desktop_send_gateway_token: bool = True
    # 脱敏映射表（context_redactions）的加密密钥（FLUX_CONTEXT_MECHANISM §10.1）。
    # 留空 = 映射不落库（fail-closed）：正文照常打码，只是不可回填。
    redaction_key: str | None = None

    # --- 设计工作室（Luma Studio，独立进程）---
    # 设计稿的持久化与 AI 改组件都由本机 Luma Studio 负责；Flux 只做鉴权代理，
    # 手机端因此复用 Flux 既有登录态，不需要另建账号体系。
    # 公网路径 /design-studio/api/designs 已在 nginx 屏蔽，只有回环直连可读写。
    design_studio_url: str = "http://127.0.0.1:8091"
    design_studio_timeout_seconds: float = 60.0

    # --- 存储（主规格 §11.1）---
    database_url: str = "sqlite+aiosqlite:///./flux.db"
    redis_url: str = "redis://localhost:6379/0"
    # 事件总线后端：memory = 进程内发布/订阅（默认，单机零依赖、测试稳定）；
    # redis = 在本机分发之外再走 Redis pub/sub 跨进程广播（FLUX_EVENT_BUS_BACKEND=redis）。
    # 注意：不会因为 redis_url 有默认值就自动切到 Redis——必须显式选 redis。
    event_bus_backend: str = "memory"
    # Redis 事件总线使用的频道名（仅 event_bus_backend=redis 时生效）。
    event_bus_redis_channel: str = "flux.events"
    # doctor 的数据库探针上限（秒）。除了在协程层用 asyncio.wait_for 兜底，
    # 还会作为 asyncpg 的连接超时，避免不可达主机把 `flux doctor` 拖住。
    doctor_db_timeout_seconds: float = 5.0

    # --- 模型供应商（主规格 §5.3）---
    # 仅供 Flux 侧基础设施调用（T2 压缩、扫描摘要等），不是 Agent 的模型通道；
    # 密钥仅由对应 Provider 读取，绝不下发给 Agent（§14.3）
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-5.5"
    anthropic_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_model: str = "claude-sonnet-5-5"
    # Claude Code 的鉴权头（code.claude.com/docs/en/env-vars，[2026-11-12 快照]）：
    #   auto       = base_url 为空或官方域名时用 ANTHROPIC_API_KEY（X-Api-Key），
    #                否则用 ANTHROPIC_AUTH_TOKEN（Authorization: Bearer），适合 LLM 网关；
    #   api_key    = 强制 ANTHROPIC_API_KEY；
    #   auth_token = 强制 ANTHROPIC_AUTH_TOKEN。
    # 两者互斥（同时设置会触发 CLI 鉴权冲突告警），非法值告警后按 auto 处理。
    anthropic_auth_mode: str = "auto"
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    # 中国区供应商（2026-11-12 核验）：默认端点与模型 id 的唯一来源是
    # flux.core.model_gateway.providers.official_models，这里只做同名配置项。
    # 密钥同样只由对应 Provider 读取，绝不下发给 Agent（§14.3）。
    qwen_api_key: str | None = None
    qwen_base_url: str = QWEN_BASE_URL
    qwen_model: str = QWEN_DEFAULT_MODEL
    zhipu_api_key: str | None = None
    zhipu_base_url: str = ZHIPU_BASE_URL
    zhipu_model: str = ZHIPU_DEFAULT_MODEL
    kimi_api_key: str | None = None
    kimi_base_url: str = KIMI_BASE_URL
    kimi_model: str = KIMI_DEFAULT_MODEL
    doubao_api_key: str | None = None
    doubao_base_url: str = DOUBAO_BASE_URL
    doubao_model: str = DOUBAO_DEFAULT_MODEL
    local_model_base_url: str | None = None
    local_model_name: str = "local-echo"
    # Anthropic 的 max_tokens 是必填字段（做不到 OpenAI / DeepSeek 那样"不传=不限"），
    # 调用方未指定时下发这个值。给足余量：平台内部的长文任务（T2 压缩、摘要）
    # 兜底值偏小会把输出截断；换成输出上限更小的模型时请同步调小。
    anthropic_max_tokens: int = 32000
    default_provider: str = "local"

    # --- Virtual Workspace Apply（主规格 §7.6）---
    # Agent 改动的落盘根目录。留空则 apply 直接报错——绝不默认写到某个"看起来还行"的目录。
    workspace_root: str | None = None
    # Apply 之后要跑的测试命令（如 "pytest -q"）；留空表示不跑测试
    test_command: str | None = None
    test_timeout_seconds: float = 300.0
    # 提案审核 TTL（秒）。> 0 时，新提案带一个 expires_at，超过即失效（pending → expired）；
    # <= 0 表示不启用超时失效——改动的有效性完全由 original_hash 复验来把关。
    proposal_ttl_seconds: int = 86400
    # 平台规则（最终方案 §13）：Agent 的改动是否必须经 Proposal 审核才能落盘。
    # 这是 Flux 的规则，不是 Agent 自己选的；下发在 flux_context.policy 里，Agent 不得绕过。
    proposal_required: bool = True

    # --- Git 集成（主规格 §17.6；实施计划 ⑨）---
    # git 命令超时；Git 操作同样只在 workspace_root 下执行
    git_timeout_seconds: float = 30.0

    # --- Project Scanner（主规格 §5.8；实施计划 ⑩）---
    # 扫描上限：文件数与目录深度都必须有界，避免在巨型仓库上把时间/内存打满。
    # 触顶时画像照常产出，但 truncated=True，让调用方知道结果被裁剪过。
    project_scan_max_files: int = 2000
    project_scan_max_depth: int = 6

    # HTTP 调用策略（§5.1 错误处理：模型失败 / 超时 → 重试）
    model_timeout_seconds: float = 60.0
    model_max_retries: int = 2
    # 平台内部模型调用的单次输出上限；None 表示不设上限，由模型自身的默认输出上限决定。
    model_max_output_tokens: int | None = None

    # --- Agent 执行内核（Claude Code / Codex / OpenCode 共用）---
    # 运行期文件根目录：会话目录、隔离配置、Bot 工作区的父目录，必须放仓库外
    runtime_home: str = "/opt/flux/runtime-home"
    # Agent 的默认工作目录（cwd），未指定任务工作区时使用
    runtime_workspace: str = "/opt/flux/runtime-ws"
    # 手机端聊天附件（用户上传的文件）：按上传 id 一目录存放，Agent 按绝对路径直接读
    uploads_dir: str = "/opt/flux/uploads"
    # AI 下发给用户的文件：按 delivery id 一目录存放，手机端按卡片里的 id/序号取文件
    deliveries_dir: str = "/opt/flux/deliveries"

    # --- Run 生命周期看护（修复方案 §2.3~§2.7）---
    # 三类 timeout，全部可关（0 = 关闭该判据）：
    #   startup：进程起来但始终没有进入 RUNNING（构造/握手卡住）的上限
    #   idle：进程还在、但既无输出也无 MCP 活动、状态也不变的上限
    #   hard：无论有无输出都不放行的绝对上限
    run_startup_timeout_seconds: float = 120.0
    run_idle_timeout_seconds: float = 600.0
    run_hard_timeout_seconds: float = 1800.0
    # 心跳周期：写 last_heartbeat_at 并检查三类超时 / 进程存活
    run_heartbeat_interval_seconds: float = 5.0
    # Reconciler 对账周期：数据库状态 ↔ 真实进程状态
    run_reconcile_interval_seconds: float = 10.0
    # Cancel 优雅期：SIGTERM 进程组后等这么久，仍在则 SIGKILL
    run_cancel_grace_seconds: float = 5.0

    # --- Agent 执行时限通用覆盖（所有 runtime 共用；0 = 用 runtime 默认表）---
    # 各 runtime 的默认值登记在 runtimes/base.py 的 RUNTIME_LIMITS 里；这里只做运维级
    # 统一收放，避免为每个 runtime 各写一套配置项，未来接入的 runtime 自动生效。
    # 提交一轮的上限（HTTP 请求 / JSON-RPC 调用）；超时视为提交失败、可重试
    agent_turn_submit_timeout_seconds: float = 0.0
    # 无任何进展（无输出 / 无 MCP 活动 / 状态不变）的容忍时长
    agent_turn_idle_timeout_seconds: float = 0.0
    # 无论有无进展都不放行的绝对上限
    agent_turn_hard_timeout_seconds: float = 0.0
    # 兜底查询 / 轮询步长
    agent_turn_poll_seconds: float = 0.0
    # 是否允许跨 Run 复用常驻进程（仅对声明 reusable_process 的 runtime 生效）
    agent_process_reuse: bool = True
    # 常驻进程空闲多久后回收（秒）
    agent_process_idle_ttl_seconds: float = 300.0
    # 上下文预算：估算输入超过该 token 数时压缩/重开会话；0 = 不限制
    agent_context_compact_tokens: int = 80000

    # --- MCP 注入（目标架构 §3；P0-01 真闭环）---
    # Agent 只能经 MCP 看项目、提提案；起 Run 时自动生成隔离的 MCP 客户端配置。
    # 关闭后 Agent 会退化成"没有平台的裸 agent"——仅用于本地排查，默认必须开着。
    mcp_enabled: bool = True
    # Flux MCP 端点（streamable-http）。默认同机默认端口；隔离实例请显式指向自己的端口
    mcp_url: str = "http://127.0.0.1:8000/mcp"
    # MCP serverName（注入后工具名形如 mcp__flux__workspace.read）
    mcp_server_name: str = "flux"
    # 内置 Agent 的令牌身份：Flux 用它在 MCP 面盖章 provenance
    mcp_agent_id: str = "flux-builtin"
    # 单次工具调用超时（毫秒）
    mcp_tool_timeout_ms: int = 60000
    # Claude Code 内核的扩展 MCP server（运维扩展点，JSON 对象字符串，形状同
    # Claude Code 的 mcpServers）。只接受 http / sse 型远程 server：stdio / command
    # 会在容器内拉起任意进程，一律拒绝；"flux" 是保留名。解析与校验见
    # flux.core.agent_runtime.extra_mcp。示例：
    # {"browser": {"type": "http", "url": "http://playwright-mcp:8931/mcp"}}
    claude_extra_mcp_servers: str = ""

    # --- 外部 CLI Agent：Codex 的模型通道（P2-1 §6.2 续，2026-10-06）---
    # codex 把「鉴权方式」当全局状态，Flux 只能给它一个隔离的 CODEX_HOME（顺便注入 MCP），
    # 而隔离目录里没有用户的登录态——不写这几项，codex 在 Run 里就是「未登录」直接失败。
    # 密钥不落盘：第三方供应商块只写 env_key（环境变量名），值由服务进程环境提供。
    # "openai" = 用 codex 内置供应商（ChatGPT 登录 / OPENAI_API_KEY），不写供应商块；
    # 其他值按第三方供应商写 [model_providers.<name>]（当前支持 deepseek）。
    codex_provider: str = "openai"
    # 空 = 用 codex 自带默认模型；非 openai 供应商必须显式给（codex 默认模型 id 是 OpenAI 的）
    codex_model: str = ""
    # 第三方模型的元数据目录（codex 的 model_catalog_json）：缺了 codex 会退化成 fallback
    # 元数据（实测警告「Model metadata not found…can degrade performance」）；空 = 不写
    codex_model_catalog: str = ""
    # ChatGPT 账号（Codex 订阅通道）的登录态目录。codex login 把 auth.json 写在这里，
    # 每次 Run 只把这一个文件复制进隔离的 CODEX_HOME（0600），不动用户的全局 ~/.codex。
    codex_account_home: str = "~/.flux/codex-account"
    # 设备码登录与订阅出网的代理地址（如 http://10.99.0.1:7890）；
    # 空 = 不注入，继承 Flux 进程环境（当前由 /usr/local/bin/codex wrapper 自带代理）。
    codex_proxy: str = ""
    # opencode 的模型清单来自它自己的全局配置（provider.<id>.models，含 Zen 免费模型）。
    # Flux 只读模型 id，不复制清单；文件不存在 = 不提供切换，沿用 opencode 自带默认。
    opencode_config_file: str = "~/.config/opencode/opencode.json"

    # --- 计费与套餐额度（本地账本 + 官方快照对账）---
    # 关掉后不再写 usage_records / usage_snapshots，闸门整体失效
    billing_enabled: bool = True
    # 是否真的拦截超额 Run。默认 False = 只告警放行：官方快照有 5~30 分钟延迟，
    # 直接用延迟数据拒单会误伤；确认口径可信后再打开。
    billing_enforce: bool = False
    # 告警 / 拦截阈值（已用百分比，0-100）
    billing_alert_percent: float = 80.0
    billing_block_percent: float = 95.0
    # 官方额度快照的缓存时长（秒）；0 = 每次都重新采集
    billing_refresh_seconds: int = 300
    # Go 套餐的总额（美元）。官方只给百分比，配了它才能在官方不可用时用本地账本折算占比
    billing_go_monthly_usd: float | None = None
    # 火山 Agent Plan 额度采集命令（本机需要先 `arkcli auth login`）；
    # 留空 = 不采集，闸门退化为「官方数据不可用」
    billing_arkcli_cmd: str = "arkcli usage plan --product agent-plan"
    billing_arkcli_timeout_seconds: float = 30.0
    # 自定义套餐定义文件（JSON）。内置套餐 + 本文件合并，同 key 覆盖内置；
    # 新增 Codex / Claude / 任意订阅套餐只需往这里加一条，不用改代码。
    billing_plans_file: str = "~/.flux/billing-plans.json"
    # 官方价目表覆盖文件（JSON，同 model 覆盖内置单价，单位每百万 token）
    billing_prices_file: str = "~/.flux/billing-prices.json"
    # 人民币价（火山方舟）换算美元用的汇率；只影响本地估算，不影响官方账单
    billing_cny_per_usd: float = 7.2
    # 跨设备用量归集：各设备把本地账本增量推到账号云，再把云端合计拉回来显示
    # 「全部设备」。关掉后只显示本机账本（无设备凭据时也自动跳过）。
    billing_cloud_sync: bool = True
    # 增量上传游标（已推到云端最大的 occurred_at）；留空 = ~/.flux/billing-sync.json
    billing_sync_file: str | None = None
    # 对话上下文上云：隐私输入以外的消息（文本 / 摘要 / 交接 / 安全输入卡片）增量
    # 备份到账号云，换设备也能回看。手滑贴进聊天的密钥会被本地红线跳过；
    # 关闭后对话只留在本机（无设备凭据时同样自动跳过）。
    context_cloud_sync: bool = True

    # --- Supervisor（助理）运行期参数 ---
    # 准入模式与计划上限落库（supervisor_settings），这里只放不属于业务策略的运维参数。
    # 后台判断 worker 开关：测试里关掉，避免与用例的事件入队断言抢时序。
    supervisor_worker_enabled: bool = True
    # outbox 轮询间隔（秒）：判断/派发 worker 的节拍，0.5 以下对 SQLite 压力偏大。
    supervisor_poll_seconds: float = 5.0
    # 判断层单次模型调用上限（秒）；超时按"判断不可用"处理，绝不默认放行。
    supervisor_assess_timeout_seconds: float = 120.0
    # 判断层瞬时失败的重试次数（不含首次）。
    supervisor_max_assess_retries: int = 2
    # 规划层瞬时失败的重试次数（不含首次）；用尽后降级为平台兜底的单工单。
    supervisor_max_plan_retries: int = 1
    # outbox 行失败重试上限；超过落 dead，保留错误信息供排查。
    supervisor_outbox_max_attempts: int = 5
    # watchdog：派活后迟迟没进入执行的租约上限（秒），超时回收并写 timed_out 报告。
    supervisor_dispatch_timeout_seconds: float = 120.0
    # watchdog：单次 Run 执行时长上限（秒）；0 表示不限制，交给 runtime 自身超时。
    supervisor_run_timeout_seconds: float = 0.0

    # --- Bot 模式（常驻 Bot）---
    # Bot 独立服务监听的端口（entrypoint.sh / Dockerfile 用 FLUX_BOT_PORT 传入）。
    # Bot 节点据此在未显式配置 FLUX_MCP_URL 时推导同机 MCP 端点。
    bot_port: int = 8600
    # Bot 节点启动时自动接入 Claude Code（默认关闭）。开启后若能在 PATH 找到 claude
    # CLI，就把 claude-code 的 installation 推进到 READY，等价于手动执行
    # `flux agents connect claude-code`；找不到 CLI 时只记 WARNING，不阻断启动。
    auto_connect_claude_code: bool = False
    # 后台调度 worker：按 next_run_at 扫到期 Bot 并开工。测试里关掉，避免与用例抢时序。
    bot_scheduler_enabled: bool = True
    # 轮询间隔（秒）：cron 的最小粒度是分钟，30 秒足够且不会压垮 SQLite。
    bot_scheduler_interval_seconds: float = 30.0
    # 后台子任务等待内核 Run 收尾的总时限（秒）：超时即终止 Run 并按失败回写父对话；
    # 0 或负数表示不设上限（仍受 Run 自身的 idle / hard 超时约束）。
    bot_subtask_timeout_seconds: float = 3600.0
    # 外部例程触发（Webhook）的 HMAC 密钥。留空 = fail-closed，拒绝所有入站请求。
    # 通用入口校验 X-Flux-Signature；GitHub 入口校验 X-Hub-Signature-256。
    routine_webhook_secret: str | None = None
    github_webhook_secret: str | None = None

    # --- Email 入站触发（IMAP 轮询）---
    # 定期轮询已安装的 email 连接器，发现新邮件后发布 email.received 事件，
    # 与 Webhook / GitHub 共用 Bot / 例程的事件触发链路。只读邮件头，不自动回复。
    email_trigger_enabled: bool = True
    # 轮询间隔（秒）。小于 15 会被抬到 15，避免测试 / 本地把 IMAP 打爆；
    # 设为 0 或负数等价于关闭轮询（即使 email_trigger_enabled=true）。
    email_trigger_interval_seconds: float = 60.0
    # 轮询的邮箱目录；需要非 INBOX 目录时改这里。
    email_trigger_mailbox: str = "INBOX"
    # 每个账号每轮最多读取的邮件头数量（连接器自身上限 50）。
    email_trigger_limit: int = 20

    # --- 助理知识库（RAG / wiki）---
    # 给助理建一份覆盖「全部对话 + docs + 项目记忆」的检索层（见 flux.core.knowledge）。
    # 关闭后不做任何索引，assistant.search 直接返回空——测试里关掉，避免后台索引抢时序。
    knowledge_enabled: bool = True
    # 后台索引 worker：按间隔把新对话 / 变更文档增量写进知识库。
    knowledge_worker_enabled: bool = True
    knowledge_index_interval_seconds: float = 300.0
    # 文档来源目录（相对路径按仓库根解析）：递归收 *.md。
    knowledge_docs_dir: str = "docs"
    # 单文件与总文件数上限：避免把巨型文档或整棵依赖树吞进来。
    knowledge_docs_max_bytes: int = 1_000_000
    knowledge_docs_max_files: int = 2000
    # 文档切块预算（估算 token）；overlap 用于跨块保留上下文。
    knowledge_chunk_tokens: int = 420
    knowledge_chunk_overlap_tokens: int = 60
    # 对话按「会话 × 时间窗」切块：窗口预算与单个会话最多取最近多少条消息。
    knowledge_conversation_window_tokens: int = 600
    knowledge_max_messages_per_conversation: int = 5000
    # 关键词预筛后进入打分的候选数；越大越准、越慢。
    knowledge_candidate_limit: int = 200
    # Embedding（OpenAI 兼容 /embeddings）。未单独配置 api_key 时复用
    # openai_api_key；两者都为空 = 纯词法检索，索引照常可用，只是少了语义召回。
    knowledge_embedding_api_key: str | None = None
    knowledge_embedding_base_url: str = "https://api.openai.com/v1"
    knowledge_embedding_model: str = "text-embedding-3-large"
    knowledge_embedding_dimensions: int | None = 3072
    knowledge_embedding_timeout_seconds: float = 30.0
    knowledge_embedding_batch_size: int = 32

    # --- Supervisor · 飞书接入 ---
    # 自建应用凭据：只用于主动发消息（报告直通回原会话）。留空 = 走空通道，
    # 投递只记日志，不让 outbox 在本地开发环境里反复重试到 dead。
    supervisor_feishu_app_id: str | None = None
    supervisor_feishu_app_secret: str | None = None
    # 事件订阅的 verification token；留空 = 拒绝一切回调（fail-closed）。
    supervisor_feishu_verification_token: str | None = None
    # 加密回调的 Encrypt Key：本仓库不做 AES 解密，配了只用于在报错里提示关闭。
    supervisor_feishu_encrypt_key: str | None = None
    # 飞书开放平台 API 基址（私有化部署可改）。
    supervisor_feishu_api_base: str = "https://open.feishu.cn"
    # 回调事件在 Supervisor 里的来源标识，同时也是订阅/通道的键。
    supervisor_feishu_source_key: str = "feishu"

    # --- Bot 频道 · 飞书（正式入站 + 受审批出站）---
    # 与 Supervisor 的飞书接入相互独立：这里把飞书文本消息变成 Bot 对话轮次，
    # 回复必须经审批卡「允许」后才发送。未配置 verification token / bot id 时
    # 消息事件 fail-closed。
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    # 事件订阅的 verification token；留空 = 拒绝一切回调。
    feishu_verification_token: str | None = None
    # 加密回调的 Encrypt Key；配置后回调必须带合法签名，密文用 AES-256-CBC 解密。
    feishu_encrypt_key: str | None = None
    # 飞书开放平台 API 基址（私有化部署可改）。
    feishu_api_base: str = "https://open.feishu.cn"
    # 这条飞书频道归属的 Flux Bot UUID；消息事件必须能定位到 Bot 才处理。
    feishu_bot_id: str | None = None

    # --- Bot 频道 · 钉钉（HTTP 事件订阅入站 + 受审批出站）---
    # AppKey 同时是应用级事件订阅的 ownerKey 与默认 robotCode；AppSecret 只用于
    # 换取 accessToken 发消息。token / EncodingAESKey 未配置时回调 fail-closed。
    dingtalk_app_key: str | None = None
    dingtalk_app_secret: str | None = None
    # 事件订阅后台配置的签名 token（3~32 位英文或数字）；留空 = 拒绝一切回调。
    dingtalk_token: str | None = None
    # 43 位 EncodingAESKey；回调密文用 AES-256-CBC 解密，应答也要用它加密。
    dingtalk_aes_key: str | None = None
    # 钉钉 OpenAPI 基址（私有化部署可改）。
    dingtalk_api_base: str = "https://api.dingtalk.com"
    # 这条钉钉频道归属的 Flux Bot UUID；消息事件必须能定位到 Bot 才处理。
    dingtalk_bot_id: str | None = None
    # --- Bot 频道 · 企业微信（WeCom，正式入站 + 受审批出站）---
    # 自建应用凭据：corp_id / agent_id / secret 用于取 access_token 并发送应用消息。
    wecom_corp_id: str | None = None
    wecom_agent_id: str | None = None
    wecom_secret: str | None = None
    # 接收消息与事件的回调 token；留空 = 拒绝一切回调（fail-closed）。
    wecom_token: str | None = None
    # 回调加密的 EncodingAESKey（43 字符）；留空 = 拒绝一切回调。
    wecom_encoding_aes_key: str | None = None
    # 企业微信 API 基址（私有化部署可改）。
    wecom_api_base: str = "https://qyapi.weixin.qq.com"
    # 这条企业微信频道归属的 Flux Bot UUID；消息事件必须能定位到 Bot 才处理。
    wecom_bot_id: str | None = None

    # --- Supervisor · 微信接入（OpenClaw + 微信 ClawBot 仅作传输层）---
    # 微信消息由本机 OpenClaw 网关转给 Flux，Flux 当大脑。入站令牌：插件每次转发
    # 都带 `Authorization: Bearer <token>`；留空 = 拒绝一切微信回调（fail-closed）。
    supervisor_wechat_bridge_token: str | None = None
    # 允许使用的微信发送者（OpenClaw senderId，通常 wxid_xxx），逗号/分号/空白分隔。
    # 留空 = 谁都拒绝：机器人绑的是个人微信，必须显式列白名单。
    supervisor_wechat_allow_from: str = ""
    # 微信来源标识：同时是订阅/出站通道的键。
    supervisor_wechat_source_key: str = "wechat"
    # 主动出站（报告回推）地址：由 OpenClaw 网关提供；留空 = 只记日志的空通道。
    # 被动回复不走这里——它直接作为入站请求的响应返回给插件。
    supervisor_wechat_send_url: str | None = None
    supervisor_wechat_timeout_seconds: float = 15.0
    # 单次回复携带的最近对话条数与知识库片段数（控制上下文预算）。
    supervisor_wechat_history_limit: int = 12
    supervisor_wechat_knowledge_limit: int = 6

    # --- 外部连接器（GitHub）---
    # OAuth App 的 client_id（public，不含密钥）；设备码流程（Device Flow）必须在
    # OAuth App 设置里勾选 Enable Device Flow 才能用。连接记录按本地账号隔离。
    github_client_id: str = "Ov23li4zYbYUuu07mqB0"
    # 申请的最小权限：repo = 读写私有仓库（列表/issue/PR 只读接口用），read:user = 读资料
    github_scopes: str = "repo read:user"
    github_api_base: str = "https://api.github.com"
    github_oauth_base: str = "https://github.com"
    github_timeout_seconds: float = 20.0

    # --- 外部连接器（日历）---
    # CalDAV / ICS 单次请求的默认超时；安装时可用 settings.timeout 覆盖（1–60 秒）。
    calendar_timeout_seconds: float = 15.0

    # --- Bot 桌面（Hermes 云电脑 noVNC）---
    # App 进入「Bot 桌面」时向服务端要入口参数。隧道地址会随隧道重启变化，所以放在
    # 公开的 desktop.json 里由服务端代读；token / bridge_key 属于凭据，只从环境变量下发，
    # 不进 App 源码（公开仓库不泄漏），轮换也不需要重新发版。
    # 与上面的 desktop_host / desktop_gateway_token / desktop_bridge 是同一组事实的
    # 兼容别名（老部署仍可用）：公开 JSON 优先，其次主用字段，最后这些别名。
    # 解析规则集中在 flux.core.desktop.resolve。
    desktop_config_url: str = "https://flux.qiuli55.top/fluxbot-mobile/desktop.json"
    # 桌面访问令牌的兼容别名（FLUX_DESKTOP_TOKEN）；桥接密钥统一用 desktop_bridge_key。
    desktop_token: str | None = None
    # desktop.json 读不到时的兜底地址（通常是上一次可用的隧道）
    desktop_gateway: str | None = None
    desktop_bridge_url: str | None = None
    desktop_timeout_seconds: float = 10.0

    # --- 密钥库（API Key Vault）---
    # 加密 API Key 的主密钥；留空 = 首次启动时在 key_vault_key_file 自动生成并 0600 落盘。
    # 主密钥丢失后已存 key 无法解密，只能重新录入（接口不会返回乱码明文）。
    key_vault_secret: str | None = None
    # 主密钥文件；留空 = ~/.flux/key-vault.key
    key_vault_key_file: str | None = None

    # --- 账号云（B 机 flux.qiuli55.top，独立私有仓库）---
    # Flux 本体的登录由账号云统一签发令牌：账号的权威/后台都在那边，本机只做本地验签
    # （见 flux.api.auth.is_cloud_access_token），不每次回源。
    cloud_base_url: str = "https://flux.qiuli55.top"
    # 与账号云共享的 HS256 密钥（FLUX_CLOUD_SECRET_KEY）。留空 = 不认账号云令牌，
    # 登录闭环整体关闭，退回"FLUX_AUTH_TOKEN / 回环直连"的老口径。
    cloud_secret_key: str | None = None
    # 是否要求走账号云登录（FLUX_CLOUD_LOGIN_REQUIRED）。桌面端安装包用：它不
    # 携带共享密钥（随包分发就等于泄露签名密钥），但产品上必须要求登录，于是把
    # 「要不要登录」与「能不能本地验签」拆开——置 True 时前端会跳账号云登录页。
    cloud_login_required: bool = False
    # 本机云会话（access / refresh token）落盘文件；留空 = ~/.flux/cloud.json。
    cloud_session_file: str | None = None
    # 登录完成后要回到的 Flux 工作台地址；登录页以 ?next=<它> 携带回跳目标。
    cloud_return_url: str | None = None
    # 运行日志落盘目录（相对路径按仓库根解析）。留空 = 不落盘、也不自动上传。
    # 生产必须配上：Flux 启动时会把「上一轮运行日志」自动上传到账号云。
    log_dir: str | None = None
    # 本机设备身份凭据（flux-device-agent 写入，0600）。用户记忆的账号级同步
    # 用设备令牌，而不是「本机是否登录了账号云」。留空 = /etc/flux-device-credentials。
    device_credentials_file: str | None = None
    # 用户长期记忆：任务执行中由 Agent 调 memory.remember 自行记录并同步到账号云；
    # 关闭后仍可在手机端手动编辑。
    memory_auto_capture: bool = True
    memory_cloud_sync: bool = True
    # 自动记录开关的落盘位置；留空 = ~/.flux/memory-prefs.json
    memory_prefs_file: str | None = None
    # IDE 行内补全偏好与当前 Agent 选择的落盘位置；留空 = ~/.flux/completion-prefs.json
    completion_prefs_file: str | None = None
    # IDE 行内补全埋点（接受率 / 首字延迟）的落盘位置；留空 = ~/.flux/completion-stats.json
    completion_stats_file: str | None = None

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def log_dir_path(self) -> Path | None:
        """运行日志目录；未配置时为 None（= 关闭落盘与自动上传）。"""
        raw = (self.log_dir or "").strip()
        if not raw:
            return None
        path = Path(raw).expanduser()
        return path if path.is_absolute() else (REPO_ROOT / path)

    @property
    def cloud_session_path(self) -> Path:
        """本机云会话文件路径。"""
        raw = (self.cloud_session_file or "").strip()
        if raw:
            path = Path(raw).expanduser()
            return path if path.is_absolute() else (REPO_ROOT / path)
        return Path.home() / ".flux" / "cloud.json"

    @property
    def device_credentials_path(self) -> Path:
        """设备身份凭据路径；未配置时为 /etc/flux-device-credentials。"""
        raw = (self.device_credentials_file or "").strip()
        if raw:
            path = Path(raw).expanduser()
            return path if path.is_absolute() else (REPO_ROOT / path)
        return Path("/etc/flux-device-credentials")

    @property
    def billing_sync_path(self) -> Path:
        """套餐用量增量上传游标文件路径。"""
        raw = (self.billing_sync_file or "").strip()
        if raw:
            path = Path(raw).expanduser()
            return path if path.is_absolute() else (REPO_ROOT / path)
        return Path.home() / ".flux" / "billing-sync.json"

    @property
    def memory_prefs_path(self) -> Path:
        """用户记忆自动记录开关的落盘路径。"""
        raw = (self.memory_prefs_file or "").strip()
        if raw:
            path = Path(raw).expanduser()
            return path if path.is_absolute() else (REPO_ROOT / path)
        return Path.home() / ".flux" / "memory-prefs.json"

    @property
    def completion_prefs_path(self) -> Path:
        """IDE 行内补全偏好与当前 Agent 选择的落盘路径。"""
        raw = (self.completion_prefs_file or "").strip()
        if raw:
            path = Path(raw).expanduser()
            return path if path.is_absolute() else (REPO_ROOT / path)
        return Path.home() / ".flux" / "completion-prefs.json"

    @property
    def completion_stats_path(self) -> Path:
        """IDE 行内补全埋点的落盘路径。"""
        raw = (self.completion_stats_file or "").strip()
        if raw:
            path = Path(raw).expanduser()
            return path if path.is_absolute() else (REPO_ROOT / path)
        return Path.home() / ".flux" / "completion-stats.json"

    @property
    def knowledge_docs_path(self) -> Path:
        """知识库文档来源目录；相对路径按仓库根解析。"""
        path = Path(self.knowledge_docs_dir).expanduser()
        return path if path.is_absolute() else (REPO_ROOT / path)

    @property
    def wechat_allow_from(self) -> set[str]:
        """微信白名单集合：把逗号 / 分号 / 空白分隔的配置拆成 senderId 集合。"""
        raw = self.supervisor_wechat_allow_from or ""
        return {part for part in re.split(r"[,;\s]+", raw.strip()) if part}

    @property
    def key_vault_key_path(self) -> Path:
        """密钥库主密钥文件路径；未配置时为 ~/.flux/key-vault.key。"""
        raw = (self.key_vault_key_file or "").strip()
        if raw:
            path = Path(raw).expanduser()
            return path if path.is_absolute() else (REPO_ROOT / path)
        return Path.home() / ".flux" / "key-vault.key"

    def agent_limits(self, runtime_id: str):
        """取某个 runtime 的生效执行时限（能力表默认值 + 本类覆盖项）。

        惰性导入 runtimes.base，避免 config ↔ runtime 的循环依赖；未登记的 runtime
        回落到 DEFAULT_RUNTIME_LIMITS，因此未来接入的 runtime 不改这里也能跑。
        """
        from dataclasses import replace

        from flux.core.agent_runtime.runtimes.base import runtime_limits

        limits = runtime_limits(runtime_id)
        overrides: dict[str, float | bool] = {}
        if self.agent_turn_submit_timeout_seconds > 0:
            overrides["submit_timeout_seconds"] = self.agent_turn_submit_timeout_seconds
        if self.agent_turn_idle_timeout_seconds > 0:
            overrides["idle_timeout_seconds"] = self.agent_turn_idle_timeout_seconds
        if self.agent_turn_hard_timeout_seconds > 0:
            overrides["hard_timeout_seconds"] = self.agent_turn_hard_timeout_seconds
        if self.agent_turn_poll_seconds > 0:
            overrides["poll_interval_seconds"] = self.agent_turn_poll_seconds
        if not self.agent_process_reuse:
            overrides["reusable_process"] = False
        # 上下文预算始终以本项为准（含 0 = 关闭）：它是运维唯一口径，不按 runtime 分叉。
        limits = replace(
            limits, context_compact_tokens=max(0, int(self.agent_context_compact_tokens))
        )
        return replace(limits, **overrides) if overrides else limits


@lru_cache
def get_settings() -> Settings:
    """进程内单例。测试中可直接实例化 Settings(...) 覆盖。"""
    return Settings()
