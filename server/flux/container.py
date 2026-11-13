"""应用容器：把各层服务装配到一处，供 API 层依赖注入。

M0 是单进程装配。后续拆分微服务（§17.2 后端服务拆分）时，本文件是唯一的装配点。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from flux.cloud.context_sync import CloudContextSync
from flux.cloud.memory_sync import CloudMemorySync
from flux.config import Settings, get_settings
from flux.connectors.base import ConnectorRegistry
from flux.core.agent_runtime.adapters import build_default_adapters
from flux.core.agent_runtime.codex_account import CodexAccountService
from flux.core.agent_runtime.installation import InstallationRepository, InstallationService
from flux.core.agent_runtime.manager import AgentManager
from flux.core.agent_runtime.repository import AgentRepository
from flux.core.agent_runtime.run_repository import AgentRunRepository
from flux.core.agent_runtime.runtimes.service import TaskRunService
from flux.core.agent_runtime.session_repository import TaskRuntimeSessionRepository
from flux.core.agent_runtime.supervisor import RunSupervisor
from flux.core.agent_runtime.trace import RunTraceRecorder
from flux.core.agent_runtime.trace_repository import RunTraceRepository
from flux.core.billing.service import BillingService
from flux.core.bot.conversation import BotConversationService
from flux.core.bot.service import BotService
from flux.core.bot.webhook_triggers import WebhookTriggerService
from flux.core.capability_import.repository import ImportedCapabilityRepository
from flux.core.capability_import.scanners import AgentScanner
from flux.core.capability_import.service import CapabilityImportService
from flux.core.channels.feishu_bot import FeishuBotChannel
from flux.core.code_intelligence.service import CodeIntelligenceService
from flux.core.connectors.framework import ConnectorFrameworkService, build_default_catalog
from flux.core.context.lifecycle import ContextLifecycle
from flux.core.context.metrics import BackfillObserver
from flux.core.context.redact import RedactionStore
from flux.core.context.repository import ContextStepRepository
from flux.core.context.store import ContentStore
from flux.core.context.trust import VerificationLedger
from flux.core.desktop.service import DesktopService
from flux.core.devices.service import DeviceService
from flux.core.event.bus import EventBus, Events
from flux.core.git_integration.client import GitClient
from flux.core.git_integration.service import GitService
from flux.core.github.service import GitHubConnectorService
from flux.core.group.orchestrator import GroupOrchestrator
from flux.core.group.repository import GroupRepository
from flux.core.ide_completion import (
    CompletionContextBuilder,
    CompletionModelResolver,
    CompletionPrefsStore,
    CompletionService,
    CompletionStatsStore,
)
from flux.core.key_vault.service import KeyVaultService
from flux.core.knowledge import (
    EmbeddingClient,
    KnowledgeIndexer,
    KnowledgeRepository,
    KnowledgeRetriever,
    KnowledgeWorker,
)
from flux.core.mcp.auth import AgentTokenService
from flux.core.memory.bot_memory_repository import BotMemoryRepository
from flux.core.memory.bot_memory_service import BotMemoryService
from flux.core.memory.prefs import MemoryPrefsStore
from flux.core.memory.repository import MemoryRepository
from flux.core.memory.service import MemoryService
from flux.core.memory.shared_user import SharedUserMemoryService
from flux.core.memory.shared_user_repository import SharedUserMemoryRepository
from flux.core.model_gateway.providers.registry import build_providers
from flux.core.model_gateway.router import ModelRouter
from flux.core.permission_engine.policy import PermissionPolicy
from flux.core.project_brain.refresh import BrainRefresher
from flux.core.project_brain.repository import ProjectBrainRepository
from flux.core.project_brain.service import ProjectBrain
from flux.core.project_files.explorer import WorkspaceFileExplorer
from flux.core.project_scanner.scanner import ProjectScanner
from flux.core.supervisor import (
    FeishuChannel,
    FeishuWebhook,
    NullChannel,
    SupervisorAssessor,
    SupervisorChannel,
    SupervisorDispatcher,
    SupervisorNotifier,
    SupervisorOrchestrator,
    SupervisorOutbox,
    SupervisorPlanner,
    SupervisorRepository,
    SupervisorService,
    SupervisorWorker,
    WechatAssistant,
    WechatBridge,
    WechatChannel,
    WechatSessionRepository,
)
from flux.core.task_engine.agent_switch import AgentSwitchService
from flux.core.task_engine.assistant import TaskAssistant
from flux.core.task_engine.conversation_summary import ConversationSummarizer
from flux.core.task_engine.memory_repository import TaskMemoryRepository
from flux.core.task_engine.repository import TaskRepository
from flux.core.task_engine.run_bridge import AUTO_RETRY_FOLLOWUP, TaskRunBridge
from flux.core.task_engine.scheduler import TaskScheduler
from flux.core.task_engine.stream import TaskStreamHub, TaskStreamRelay
from flux.core.terminal.pty_service import HumanPtyService
from flux.core.terminal.repository import TerminalRepository
from flux.core.terminal.service import TerminalService
from flux.core.virtual_workspace.apply_engine import ApplyEngine
from flux.core.virtual_workspace.batch_repository import ApplyBatchRepository
from flux.core.virtual_workspace.repository import ProposalRepository
from flux.core.virtual_workspace.service import VirtualWorkspaceService
from flux.db.session import create_engine, create_session_factory
from flux.enums import ModelProvider


@dataclass
class Container:
    settings: Settings = field(default_factory=get_settings)
    bus: EventBus = field(default_factory=EventBus)
    policy: PermissionPolicy = field(default_factory=PermissionPolicy)
    engine: AsyncEngine = field(init=False)
    #: 手机设备配对码与长期设备令牌（鉴权依赖直接从这里取缓存）。
    devices: DeviceService = field(init=False)
    agents: AgentManager = field(init=False)
    router: ModelRouter = field(init=False)
    scheduler: TaskScheduler = field(init=False)
    connectors: ConnectorRegistry = field(init=False)
    workspace: VirtualWorkspaceService = field(init=False)
    apply_engine: ApplyEngine = field(init=False)
    terminal_repo: TerminalRepository = field(init=False)
    terminal: TerminalService = field(init=False)
    human_pty: HumanPtyService = field(init=False)
    git_client: GitClient = field(init=False)
    git: GitService = field(init=False)
    scanner: ProjectScanner = field(init=False)
    files: WorkspaceFileExplorer = field(init=False)
    #: 代码智能（IDE 补全）：LSP 语言服务器进程池 + WebSocket 网关，按工作区根复用
    code_intel: CodeIntelligenceService = field(init=False)
    brain_repo: ProjectBrainRepository = field(init=False)
    brain: ProjectBrain = field(init=False)
    brain_refresher: BrainRefresher = field(init=False)
    memory_repo: MemoryRepository = field(init=False)
    memory: MemoryService = field(init=False)
    task_repo: TaskRepository = field(init=False)
    key_vault: KeyVaultService = field(init=False)
    #: 外部连接器（GitHub）：按本地账号隔离的 OAuth 连接 + 只读 REST 工具
    github: GitHubConnectorService = field(init=False)
    #: 用户账号连接器框架（catalog / install / uninstall）；与 Agent 执行侧注册表分离
    connector_framework: ConnectorFrameworkService = field(init=False)
    #: Bot 桌面入口参数：公开隧道地址 + 环境变量里的凭据
    desktop: DesktopService = field(init=False)
    task_runs: TaskRunBridge = field(init=False)
    #: Agent 切换（执行中排队、本轮收尾后生效、交接卡）
    agent_switch: AgentSwitchService = field(init=False)
    assistant: TaskAssistant = field(init=False)
    #: 对话交接摘要（「上下文快满 → 新建对话带前情」）
    conversation_summarizer: ConversationSummarizer = field(init=False)
    #: 任务对话的实时增量（SSE）：进程内广播，最终消息仍以数据库为准
    task_stream: TaskStreamHub = field(init=False)
    #: 内部多 bot 群聊：群/成员/消息/epoch 读写 + 有限轮编排
    group_repo: GroupRepository = field(init=False)
    groups: GroupOrchestrator = field(init=False)
    #: Bot 自己的长期记忆（第一人称）：摘要层合并覆盖 + 全文 chunk 层只追加
    bot_memory_repo: BotMemoryRepository = field(init=False)
    bot_memories: BotMemoryService = field(init=False)
    #: 跨 Bot 共享的用户记忆：画像（只由用户侧 API 写）+ 去重日志（只追加）
    shared_user_memory_repo: SharedUserMemoryRepository = field(init=False)
    shared_user_memory: SharedUserMemoryService = field(init=False)
    proposal_repo: ProposalRepository = field(init=False)
    batch_repo: ApplyBatchRepository = field(init=False)
    #: Run 生命周期看护（所有 Agent runtime 共用：Claude Code / Codex / OpenCode）
    run_supervisor: RunSupervisor = field(init=False)
    agent_tokens: AgentTokenService = field(init=False)
    agent_repo: AgentRepository = field(init=False)
    run_repo: AgentRunRepository = field(init=False)
    installations: InstallationService = field(init=False)
    capability_import: CapabilityImportService = field(init=False)
    #: ChatGPT 账号（Codex 订阅通道）：设备码登录与 Run 时的 auth.json 注入
    codex_account: CodexAccountService = field(init=False)
    #: 计费与套餐额度（本地账本 + 官方快照对账 + 闸门）；必须容器级单例，
    #: 因为 CliRuntime 每个 Run 新建实例，实例内缓存跨 run 不共享。
    billing: BillingService = field(init=False)
    task_run_service: TaskRunService = field(init=False)
    #: Bot 模式：常驻 Bot 的生命周期编排与定时触发（对话模式不经过它）
    bots: BotService = field(init=False)
    #: 外部例程触发：Webhook 验签 + GitHub 事件映射，验签后统一投递到事件总线
    webhook_triggers: WebhookTriggerService = field(init=False)
    #: Bot 对话轮次：持久化 turn + 每 Bot 串行后台队列（legacy 对话也复用其助理逻辑）
    bot_conversation: BotConversationService = field(init=False)
    #: 正式飞书频道：加密回调入站建 turn，出站回复必须经审批卡放行
    feishu_bot: FeishuBotChannel = field(init=False)
    # IDE 行内补全（AI_COMPLETION_SPEC）：偏好/当前 Agent 落盘 + 模型解析 + 流式生成
    completion_prefs: CompletionPrefsStore = field(init=False)
    completion_resolver: CompletionModelResolver = field(init=False)
    completion: CompletionService = field(init=False)
    completion_stats: CompletionStatsStore = field(init=False)
    # 上下文机制（设计文档 §8）：只追加的 step 事件日志 + 内容寻址原文库
    context_steps: ContextStepRepository = field(init=False)
    content_store: ContentStore = field(init=False)
    context_lifecycle: ContextLifecycle = field(init=False)
    context_ledger: VerificationLedger = field(init=False)
    context_redactions: RedactionStore = field(init=False)
    # 回补率观测（§6.7）：进程内累积打包给出去的引用数与按需取回次数
    context_backfill: BackfillObserver = field(init=False)
    #: 助理（Supervisor）：外部事件幂等入库 + outbox；判断层无状态，执行仍归 Task Engine
    supervisor_repo: SupervisorRepository = field(init=False)
    supervisor_outbox: SupervisorOutbox = field(init=False)
    supervisor_assessor: SupervisorAssessor = field(init=False)
    supervisor_planner: SupervisorPlanner = field(init=False)
    supervisor_dispatcher: SupervisorDispatcher = field(init=False)
    supervisor_orchestrator: SupervisorOrchestrator = field(init=False)
    supervisor_worker: SupervisorWorker = field(init=False)
    supervisor: SupervisorService = field(init=False)
    #: 出站通道（按 source_key 索引）与报告投递器；未配凭据时飞书走空通道
    supervisor_channels: dict[str, SupervisorChannel] = field(init=False)
    supervisor_notifier: SupervisorNotifier = field(init=False)
    #: 飞书事件回调（验签 + 解析 + 幂等入库），免 REST 鉴权单独挂载
    supervisor_feishu: FeishuWebhook = field(init=False)
    #: 微信私聊 ↔ Flux 助理任务的映射（含入站幂等游标）
    wechat_sessions: WechatSessionRepository = field(init=False)
    #: 微信入站桥：OpenClaw / 微信 ClawBot 只做传输，Flux 负责理解与回复
    supervisor_wechat: WechatBridge = field(init=False)
    #: 助理知识库（RAG）：对话 / docs / 项目记忆的增量索引与混合检索
    knowledge_enabled: bool = field(init=False)
    knowledge_repo: KnowledgeRepository = field(init=False)
    knowledge_embedder: EmbeddingClient = field(init=False)
    knowledge_indexer: KnowledgeIndexer = field(init=False)
    knowledge: KnowledgeRetriever = field(init=False)
    knowledge_worker: KnowledgeWorker = field(init=False)

    def __post_init__(self) -> None:
        self.engine = create_engine(self.settings.database_url)
        self.session_factory = create_session_factory(self.engine)  # type: ignore[attr-defined]
        self.devices = DeviceService(self.session_factory, self.settings)  # type: ignore[attr-defined]
        #: 内建 CLI Agent Adapter 集合：installation 状态机与 P2-1 的 CLI runtime 共用同一批实例
        cli_adapters = build_default_adapters()
        self.task_repo = TaskRepository(self.session_factory)  # type: ignore[attr-defined]
        # 密钥库：Agent 模型通道的 API Key（加密落库），与任务级 model / api_key_id 配合解析
        self.key_vault = KeyVaultService(self.session_factory, self.settings)  # type: ignore[attr-defined]
        # 外部连接器：令牌复用同一把密钥库主密钥加密，按本地账号隔离
        self.github = GitHubConnectorService(
            self.session_factory,  # type: ignore[attr-defined]
            self.settings,
            self.key_vault,
        )
        # 账号侧连接器目录：当前只上架 GitHub；安装状态按 account_key 隔离。
        # Agent 执行侧的 connector 注册表（self.connectors）保持独立，互不替换。
        self.connector_framework = ConnectorFrameworkService(
            self.session_factory,  # type: ignore[attr-defined]
            self.key_vault,
            build_default_catalog(self.settings),
            github=self.github,
        )
        # Bot 桌面：只做公开隧道地址代读 + 凭据拼装，无状态
        self.desktop = DesktopService(self.settings)
        # 提案的权威存储在 virtual_changes 表（实施计划 ④），进程重启后审核队列不丢
        self.proposal_repo = ProposalRepository(self.session_factory)  # type: ignore[attr-defined]
        # Apply 批日志（P0-1）：每次 apply_many 一行，先于磁盘操作落库；
        # 崩溃恢复（启动全量扫 + Apply 入口懒检查）按它找出未对账的批
        self.batch_repo = ApplyBatchRepository(self.session_factory)  # type: ignore[attr-defined]
        # Agent 档案注册表：Flux 不执行 Agent，只维护档案与权限边界（目标架构 §1）。
        # P3-16 起档案落 agents 表，canonical UUID 是身份的唯一权威（重启后 load_from_db 重建）。
        self.agent_repo = AgentRepository(self.session_factory)  # type: ignore[attr-defined]
        self.agents = AgentManager(self.bus, self.agent_repo)
        # Agent Installation（最终方案 §3.2 / §4）：本机 CLI Agent 的发现与接入状态，
        # 与档案（身份/权限）分离；Adapter 只提供事实，状态机与持久化在本服务里。
        self.installations = InstallationService(
            InstallationRepository(self.session_factory),
            bus=self.bus,
            adapters=cli_adapters,
        )
        # 能力导入（批次③ §5）：扫描本机 Agent / Skill / Connector → Flux 标准对象 → 注册表。
        # 扫描源是代码常量（不给设置项、REST 不收路径）；Agent 事实从 installations
        # 只读投影（AgentScanner 扫描时先刷新 installation 事实），运行时执行仍归各自组件。
        self.capability_import = CapabilityImportService(
            ImportedCapabilityRepository(self.session_factory),
            agents=AgentScanner(self.installations),
            bus=self.bus,
        )
        # 模型路由仅供平台内部使用（T2 压缩、摘要等），不是任何 agent loop 的模型通道
        self.router = ModelRouter(
            build_providers(self.settings),
            default_provider=self._resolve_default_provider(),
        )
        # Solo 对话助手：平台侧助手（非 agent），回复内容来自 router 的真实模型调用
        self.assistant = TaskAssistant(self.router)
        # 对话交接摘要：新对话带旧对话前情时使用，与助手共用平台模型通道
        self.conversation_summarizer = ConversationSummarizer(self.router)
        self.task_stream = TaskStreamHub()
        # 内部多 bot 群聊：复用任务仓储读每个 bot 的私聊，复用平台模型通道发言
        self.group_repo = GroupRepository(self.session_factory)  # type: ignore[attr-defined]
        # Bot 长期记忆：跟随 Bot 跨群、跨开工；群聊 epoch 结束与独立开工都会读写这里
        self.bot_memory_repo = BotMemoryRepository(self.session_factory)  # type: ignore[attr-defined]
        self.bot_memories = BotMemoryService(
            self.bot_memory_repo,
            summarizer=self.conversation_summarizer,
        )
        # 共享用户记忆：与 Bot 记忆并存，只作为所有 Bot 都能看到的账号级背景
        self.shared_user_memory_repo = SharedUserMemoryRepository(
            self.session_factory  # type: ignore[attr-defined]
        )
        self.shared_user_memory = SharedUserMemoryService(self.shared_user_memory_repo)
        self.groups = GroupOrchestrator(
            self.group_repo,
            self.task_repo,
            self.router,
            key_vault=self.key_vault,
            summarizer=self.conversation_summarizer,
            bot_memories=self.bot_memories,
        )
        self.scheduler = TaskScheduler()
        self.connectors = ConnectorRegistry(self.bus, self.policy)
        # Apply Engine 是唯一会写用户真实文件的组件（§7.6），根目录与测试命令来自配置
        self.apply_engine = ApplyEngine(
            workspace_root=self.settings.workspace_root,
            test_command=self.settings.test_command,
            test_timeout_seconds=self.settings.test_timeout_seconds,
        )
        self.workspace = VirtualWorkspaceService(
            self.proposal_repo,
            self.bus,
            self.apply_engine,
            proposal_ttl_seconds=self.settings.proposal_ttl_seconds,
            batch_repository=self.batch_repo,
        )
        # 终端会话（Agent Terminal Console §6）：Flux 自己执行命令，命令与输出都可观察、可停止；
        # 工作区根与 Apply 同源（FLUX_WORKSPACE_ROOT），未配置时会话创建即 422
        self.terminal_repo = TerminalRepository(self.session_factory)  # type: ignore[attr-defined]
        self.terminal = TerminalService(
            self.terminal_repo,
            self.bus,
            workspace_root=self.settings.workspace_root,
        )
        # Human Terminal 是独立生命周期：PTY 进程要活过客户端 websocket 断线重连，
        # 但必须在 Flux 进程退出时统一回收（详见 doc/HUMAN_TERMINAL_PTY.md）。
        self.human_pty = HumanPtyService(
            self.terminal_repo,
            self.bus,
            workspace_root=self.settings.workspace_root,
        )
        # Git 集成（⑨）：与 Apply 共用同一个工作区根；按 change_ids 提交时
        # 由 GitService 复验提案是否已 applied，未落盘/未过测试的改动进不了 Git 历史
        self.git_client = GitClient(
            workspace_root=self.settings.workspace_root,
            timeout_seconds=self.settings.git_timeout_seconds,
        )
        self.git = GitService(self.git_client, self.workspace, self.bus)
        # Project Scanner（⑩）复用 ⑨ 的 GitClient 判断"是不是仓库、在哪个分支"；
        # Project Brain（⑪）把扫描结果与人工/Agent 写的记忆一起存进 projects 表
        self.scanner = ProjectScanner(
            git=self.git_client,
            max_files=self.settings.project_scan_max_files,
            max_depth=self.settings.project_scan_max_depth,
        )
        # 文件浏览（只读）：与 Apply / Scanner 共用同一个工作区根配置，
        # 请求里的 workspace_root 只是按次覆盖，不是新的默认来源。
        self.files = WorkspaceFileExplorer(workspace_root=self.settings.workspace_root)
        # 代码智能（IDE 补全）：语言服务器进程按 (工作区根, server) 常驻，空闲 30 分钟回收。
        # 装配本身不起进程，第一次建立 WS 连接时才拉活。
        self.code_intel = CodeIntelligenceService()
        self.brain_repo = ProjectBrainRepository(self.session_factory)  # type: ignore[attr-defined]
        self.brain = ProjectBrain(
            self.brain_repo,
            scanner=self.scanner,
            bus=self.bus,
            workspace_root=self.settings.workspace_root,
        )
        # 三层记忆（批次② §4.2）：User / Environment 落 memories 表，Project 复用 brain；
        # 写入必须走受控路径（用户 REST / 平台代码），Agent 面只有只读的 memory.recall
        self.memory_repo = MemoryRepository(self.session_factory)  # type: ignore[attr-defined]
        self.memory = MemoryService(self.memory_repo, brain=self.brain)
        # 账号级用户记忆：本地只做缓存，云端是权威存储；同步失败不影响本地读写
        self.memory_prefs = MemoryPrefsStore(
            self.settings.memory_prefs_path,
            default_auto_capture=self.settings.memory_auto_capture,
        )
        self.memory_cloud = CloudMemorySync(self.settings, self.memory)
        self.memory.attach_cloud(self.memory_cloud)
        # 对话上下文上云（第二阶段）：隐私输入以外的消息增量备份到账号云；
        # 待传队列落在 task_messages.cloud_uploaded_at，未配置设备凭据时自动跳过
        self.context_cloud = CloudContextSync(self.settings, self.task_repo)
        # IDE 行内补全（AI_COMPLETION_SPEC）：偏好与「当前 Agent」落单文件；
        # 三层上下文直接复用文件浏览 / 项目扫描 / Git 的只读能力，不新建索引。
        self.completion_prefs = CompletionPrefsStore(self.settings.completion_prefs_path)
        # 埋点独立落盘：接受率 / 首字延迟调优要用，写失败也不能影响补全
        self.completion_stats = CompletionStatsStore(self.settings.completion_stats_path)
        self.completion_resolver = CompletionModelResolver(
            settings=self.settings,
            key_vault=self.key_vault,
            agents=self.agents,
            prefs=self.completion_prefs,
        )
        self.completion = CompletionService(
            settings=self.settings,
            builder=CompletionContextBuilder(
                settings=self.settings,
                files=self.files,
                scanner=self.scanner,
                git_client=self.git_client,
            ),
            resolver=self.completion_resolver,
            prefs=self.completion_prefs,
        )
        # MCP 能力面的鉴权（目标架构 §3.2）：令牌是 agent 进入平台的唯一凭据，
        # 与权限策略同源——工具要求的 Capability 直接取自 flux.enums.Capability。
        # 先于 Agent Run 装配：起 Run 时要拿内置 Agent 的令牌注入 MCP 配置（P0-01）。
        # P3-16：令牌只引用 Agent Registry 的 canonical UUID，故注入注册表做校验与解析。
        self.agent_tokens = AgentTokenService(self.session_factory, self.agents)  # type: ignore[attr-defined]
        # Run 权威状态落 agent_runs 表（P2-15）：多实例共享同库时用 owner_pid 区分归属
        self.run_repo = AgentRunRepository(self.session_factory)  # type: ignore[attr-defined]
        # 执行轨迹（思考 / 命令 / 错误）按 Run 落库：对话页的「任务耗时 · 概览 · 详情」靠它
        self.run_trace_repo = RunTraceRepository(self.session_factory)  # type: ignore[attr-defined]
        self.run_trace = RunTraceRecorder(self.run_trace_repo)
        # 工作记忆卡（每轮结束平台自动写，下一轮注入执行指令）
        self.task_memory = TaskMemoryRepository(self.session_factory)  # type: ignore[attr-defined]
        # 任务 ↔ 外部 Agent 会话（opencode / codex 跨轮复用）
        self.runtime_sessions = TaskRuntimeSessionRepository(
            self.session_factory  # type: ignore[attr-defined]
        )
        # Run 权威状态机：状态流转、超时、进程树清理、对账与重启恢复都在这里，
        # 所有 Agent runtime（Claude Code / Codex / OpenCode）共用同一个实例。
        self.run_supervisor = RunSupervisor(
            settings=self.settings,
            repository=self.run_repo,
            bus=self.bus,
        )
        # Run 终态 → 任务状态（P0-05）：订阅事件总线，Run 结束即回写所属任务。
        # 同时把任务级对账挂上看护器的对账循环：事件在崩溃时会丢，running 任务需要兜底收尾
        # （P2-04 / TC-502），Run 状态先对完再让下游据真实状态判断。
        self.task_runs = TaskRunBridge(
            self.task_repo,
            self.run_repo,
            brain=self.brain,
            workspace=self.workspace,
            stream=self.task_stream,
            trace=self.run_trace,
            trace_repo=self.run_trace_repo,
            memory=self.task_memory,
        )
        self.task_runs.attach(self.bus)
        # Runtime 的运行事件 → 任务流：前端订阅 SSE 就能看到 Agent 正在干什么
        # 同时把增量合并成执行轨迹，Run 终态落库（任务详情的思考过程与命令输出靠它回看）
        TaskStreamRelay(self.task_stream, self.run_trace).attach(self.bus)
        # 上下文闭环：提案 applied 后刷新项目 Brain 快照（同项目节流，失败不阻塞 Apply）
        self.brain_refresher = BrainRefresher(
            brain=self.brain,
            tasks=self.task_repo,
            workspace=self.workspace,
            bus=self.bus,
        )
        self.brain_refresher.attach()
        self.run_supervisor.add_reconcile_hook(self.task_runs.reconcile_orphan_tasks)
        # MCP 工具调用成功 = 一次有效进展（P2-15 §2.4）：接线到看护器，避免 idle timeout 误杀
        self.bus.subscribe(Events.MCP_TOOL_CALLED, self._on_mcp_tool_called)
        # Run 落终态 → 回收该 Run 经 terminal.execute 派生的终端会话/命令进程（FINDING-1）：
        # 否则会出现"Run 已结束、命令进程还在跑、会话还 active"的泄漏。终态唯一汇合点见
        # RunSupervisor._record_terminal，5 个终态事件都带上 run_id。
        for _terminal_event in (
            Events.DSH_RUN_COMPLETED,
            Events.DSH_RUN_FAILED,
            Events.DSH_RUN_CANCELLED,
            Events.DSH_RUN_TIMEOUT,
            Events.DSH_RUN_INTERRUPTED,
        ):
            self.bus.subscribe(_terminal_event, self._on_run_terminal)
        # P2-1 §6.2：任务「开始执行」的编排收口——按 agent.config["runtime"] 分发
        # claude-code / codex / opencode。所有 runtime 复用同一个 RunSupervisor 与令牌服务。
        # ChatGPT 账号登录态由它提供：codex Run 会把 auth.json 注入隔离 CODEX_HOME。
        self.codex_account = CodexAccountService(self.settings)
        # 计费账本与额度闸门：CLI runtime 的每一步用量都落到这里；先于 TaskRunService
        # 装配，因为 Run 创建前的额度检查要读它。
        self.billing = BillingService(self.settings, self.session_factory)  # type: ignore[attr-defined]
        # Run 收尾补一次官方额度快照，给外部客户端消耗的差分估算切区间
        self.task_runs.set_billing(self.billing)
        # ChatGPT 账号登录成功时自动采集一次 Codex 额度（plan + 窗口百分比）
        self.codex_account.set_billing(self.billing)
        self.task_run_service = TaskRunService(self, cli_adapters=cli_adapters)
        # Bot 模式：每次开工都落一条带 bot_id / workspace 快照的任务，再交给同一个
        # TaskRunService；调度器由 main.py lifespan 起停（bot_scheduler_enabled）。
        self.bots = BotService(
            self,
            interval_seconds=self.settings.bot_scheduler_interval_seconds,  # type: ignore[attr-defined]
        )
        # 外部 Webhook：验签后走事件总线，与进程内事件共用同一条 Bot / 例程派发链路。
        self.webhook_triggers = WebhookTriggerService(self)
        # Bot 对话：一轮落一条 bot_turns，后台按 Bot 串行调用同一个助手逻辑。
        self.bot_conversation = BotConversationService(self)
        # 正式飞书频道：入站回调建 turn；出站草稿走审批卡，用户点「允许」后才发送。
        # 监听器只处理 kind=feishu_outbound 的审批结果，与既有审批闸门共存。
        self.feishu_bot = FeishuBotChannel(
            self,
            app_id=self.settings.feishu_app_id,
            app_secret=self.settings.feishu_app_secret,
            verification_token=self.settings.feishu_verification_token,
            encrypt_key=self.settings.feishu_encrypt_key,
            api_base=self.settings.feishu_api_base,
            bot_id=self.settings.feishu_bot_id,
        )
        self.bus.subscribe(Events.APPROVAL_RESOLVED, self.feishu_bot.on_approval_resolved)
        # 上游瞬时故障（429 / ReadTimeout）自动续跑一轮：事件桥只做判定与冷却，
        # 真正重新起 Run 交给 TaskRunService（沿用任务自己的 Agent / 模型 / 权限模式）。
        self.task_runs.set_retry_handler(self._auto_retry_task)
        # 无感切换 Agent：排队目标在本轮真实收尾后应用（自动重试冷却期间不应用）
        self.agent_switch = AgentSwitchService(self)
        self.task_runs.set_switch_handler(self.agent_switch.apply_pending)
        # 上下文机制（设计文档 §8）：step 事件日志是唯一真相源（§8.1 不变量 I1），
        # 原文单独进内容寻址库（I2 满保真）；两者都是纯存储，无副作用。
        self.context_steps = ContextStepRepository(self.session_factory)  # type: ignore[attr-defined]
        self.content_store = ContentStore(self.session_factory)  # type: ignore[attr-defined]
        # 删会话即级联删该 run / turn 的 step、索引与孤儿原文（§8.2，唯一允许丢原文的例外）
        self.context_lifecycle = ContextLifecycle(self.session_factory)  # type: ignore[attr-defined]
        # 验证账本：按「指纹 × 断言类」记验证结论，信任分级据此判定（§4.4）
        self.context_ledger = VerificationLedger(self.session_factory)  # type: ignore[attr-defined]
        # 入站脱敏的映射表：占位符 → 原文，单独加密；无 FLUX_REDACTION_KEY 时不落库（§10.1）
        self.context_redactions = RedactionStore(
            self.session_factory,
            key=self.settings.redaction_key,  # type: ignore[attr-defined]
        )
        # 终端事件的 command/chunk 落库前过同一套入站脱敏（§10.1）；此处补装配，
        # 因 TerminalService 先于 RedactionStore 构造。
        self.terminal.redactions = self.context_redactions
        # 回补率观测（§6.7）：纯内存计数，不落库；打包记 offer、取回记 fetch
        self.context_backfill = BackfillObserver()
        # 助理（Supervisor）：P0 只有事件幂等入库 + outbox 排队；判断层（P1）与派发
        # （P2）默认关闭，没有事件进入就不会产生任何副作用。
        self.supervisor_repo = SupervisorRepository(self.session_factory)  # type: ignore[attr-defined]
        self.supervisor_outbox = SupervisorOutbox(
            self.session_factory,  # type: ignore[attr-defined]
            max_attempts=self.settings.supervisor_outbox_max_attempts,
        )
        self.supervisor_assessor = SupervisorAssessor(
            self.router,
            timeout_seconds=self.settings.supervisor_assess_timeout_seconds,
            max_retries=self.settings.supervisor_max_assess_retries,
        )
        # 规划层只产计划；派发层只把工单落到 Task Engine，执行仍归 task_run_service
        self.supervisor_planner = SupervisorPlanner(
            self.router,
            timeout_seconds=self.settings.supervisor_assess_timeout_seconds,
            max_retries=self.settings.supervisor_max_plan_retries,
        )
        self.supervisor_dispatcher = SupervisorDispatcher(
            self.supervisor_repo,
            self.task_repo,
            self.task_run_service,
            self.agents,
            supervisor=self.run_supervisor,
        )
        self.supervisor_orchestrator = SupervisorOrchestrator(
            self.supervisor_repo,
            self.supervisor_planner,
            self.supervisor_dispatcher,
            outbox=self.supervisor_outbox,
            brain=self.brain,
            dispatch_timeout_seconds=self.settings.supervisor_dispatch_timeout_seconds,
            run_timeout_seconds=self.settings.supervisor_run_timeout_seconds,
        )
        # 出站通道：报告落库后由 outbox 驱动投递回订阅的原会话。
        # 飞书凭据缺失时不装配真实通道（空实现只记日志），避免本地环境反复重试。
        self.supervisor_channels = _supervisor_channels(self.settings)
        self.supervisor_notifier = SupervisorNotifier(
            self.supervisor_repo, self.supervisor_channels
        )
        self.supervisor_worker = SupervisorWorker(
            self.supervisor_repo,
            self.supervisor_outbox,
            self.supervisor_assessor,
            orchestrator=self.supervisor_orchestrator,
            notifier=self.supervisor_notifier,
            poll_seconds=self.settings.supervisor_poll_seconds,
        )
        self.supervisor = SupervisorService(
            self.supervisor_repo,
            self.supervisor_outbox,
            worker=self.supervisor_worker,
            orchestrator=self.supervisor_orchestrator,
        )
        self.supervisor_feishu = FeishuWebhook(
            self.supervisor,
            source_key=self.settings.supervisor_feishu_source_key,
            verification_token=self.settings.supervisor_feishu_verification_token,
            encrypt_key=self.settings.supervisor_feishu_encrypt_key,
        )
        # 助理知识库（RAG）：对话 / docs / 项目记忆增量落入 knowledge_chunks，
        # 检索走 BM25 + 向量 + MMR。未配置 embedding 密钥时自动降级为纯词法，
        # 装配本身不发起任何网络请求。
        self.knowledge_enabled = self.settings.knowledge_enabled
        self.knowledge_repo = KnowledgeRepository(
            self.session_factory  # type: ignore[attr-defined]
        )
        self.knowledge_embedder = EmbeddingClient(
            api_key=self.settings.knowledge_embedding_api_key or self.settings.openai_api_key,
            base_url=self.settings.knowledge_embedding_base_url,
            model=self.settings.knowledge_embedding_model,
            dimensions=self.settings.knowledge_embedding_dimensions,
            timeout=self.settings.knowledge_embedding_timeout_seconds,
            batch_size=self.settings.knowledge_embedding_batch_size,
        )
        self.knowledge_indexer = KnowledgeIndexer(
            session_factory=self.session_factory,  # type: ignore[attr-defined]
            repository=self.knowledge_repo,
            embedder=self.knowledge_embedder,
            brain_repo=self.brain_repo,
            docs_path=self.settings.knowledge_docs_path,
            docs_max_bytes=self.settings.knowledge_docs_max_bytes,
            docs_max_files=self.settings.knowledge_docs_max_files,
            chunk_tokens=self.settings.knowledge_chunk_tokens,
            chunk_overlap_tokens=self.settings.knowledge_chunk_overlap_tokens,
            conversation_window_tokens=self.settings.knowledge_conversation_window_tokens,
            max_messages_per_conversation=self.settings.knowledge_max_messages_per_conversation,
        )
        self.knowledge = KnowledgeRetriever(
            repository=self.knowledge_repo,
            embedder=self.knowledge_embedder,
            candidate_limit=self.settings.knowledge_candidate_limit,
        )
        self.knowledge_worker = KnowledgeWorker(
            self.knowledge_indexer,
            interval_seconds=self.settings.knowledge_index_interval_seconds,
        )
        # 微信接入：OpenClaw + 微信 ClawBot 负责登录与收发，Flux 只做大脑。
        # 每条微信私聊绑定一条 assistant 任务，消息落 task_messages 后自动进入知识库索引；
        # 令牌 / 白名单未配置时 fail-closed（见 `flux.core.supervisor.wechat`）。
        self.wechat_sessions = WechatSessionRepository(
            self.session_factory  # type: ignore[attr-defined]
        )
        self.supervisor_wechat = WechatBridge(
            WechatAssistant(
                self.router,
                self.task_repo,
                self.wechat_sessions,
                knowledge=self.knowledge if self.knowledge_enabled else None,
                source_key=self.settings.supervisor_wechat_source_key,
                history_limit=self.settings.supervisor_wechat_history_limit,
                knowledge_limit=self.settings.supervisor_wechat_knowledge_limit,
            ),
            bridge_token=self.settings.supervisor_wechat_bridge_token,
            allow_from=self.settings.wechat_allow_from,
            source_key=self.settings.supervisor_wechat_source_key,
        )

    def set_workspace_root(self, root: str) -> str:
        """Switch the live IDE workspace root for all filesystem-bound services."""
        resolved = Path(root).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"工作目录不存在或不是目录：{resolved}")
        value = str(resolved)
        self.settings.workspace_root = value
        self.apply_engine.workspace_root = value
        self.terminal.workspace_root = value
        self.human_pty.workspace_root = value
        self.git_client.workspace_root = value
        self.files.workspace_root = value
        self.brain.workspace_root = value
        return value

    async def _on_mcp_tool_called(self, _event: str, payload: dict[str, object]) -> None:
        """MCP 工具调用成功后刷新对应 Agent 名下 Run 的 last_mcp_activity_at。"""
        if payload.get("error"):
            return
        agent_id = payload.get("agent_id")
        if isinstance(agent_id, str):
            await self.run_supervisor.note_mcp_activity(agent_id)

    async def _on_run_terminal(self, _event: str, payload: dict[str, object]) -> None:
        """Run 落终态后，回收它名下派生的终端会话与命令进程（FINDING-1 防泄漏）。"""
        run_id = payload.get("run_id")
        if isinstance(run_id, str):
            await self.terminal.stop_run_sessions(run_id)

    async def _auto_retry_task(self, task: Any) -> None:
        """自动重试回调：重新读一次任务（取最新状态），只换续跑指令重新起 Run。"""
        fresh = await self.task_repo.get(task.id)
        await self.task_run_service.start(fresh, followup=AUTO_RETRY_FOLLOWUP)

    def _resolve_default_provider(self) -> ModelProvider:
        try:
            return ModelProvider(self.settings.default_provider)
        except ValueError:
            return ModelProvider.LOCAL

    async def dispose(self, *, close_terminal_sessions: bool = True) -> None:
        # 正式飞书频道先收摊：取消仍在等 turn 的后台任务并关发送端连接池，
        # 避免 Bot 对话 worker 停掉时它们又去挂新的出站审批。
        await self.feishu_bot.aclose()
        # Bot 对话 worker 先停：它要写 bot_turns / task_messages，不能跑在 dispose 之后。
        await self.bot_conversation.shutdown()
        # 退出时先停看护循环并清理仍在跑的 Agent 进程树，再释放连接（P2-15 §2.5）
        await self.human_pty.shutdown()
        await self.terminal.shutdown(close_sessions=close_terminal_sessions)
        await self.code_intel.shutdown()
        await self.completion.shutdown()
        # CLI runtime 的常驻进程（声明 reusable_process 的，如 opencode serve）不在
        # 单轮结束时停：退出时必须显式清空池，否则进程与任务级令牌都会残留。
        from flux.core.agent_runtime.runtimes.cli import shutdown_cli_process_pool

        await shutdown_cli_process_pool()
        # 出站通道持有 httpx 连接池，退出时一并释放
        for channel in self.supervisor_channels.values():
            await channel.aclose()
        # 知识库 embedding 客户端同样持有连接池（未启用时是空操作）
        await self.knowledge_embedder.aclose()
        await self.run_supervisor.stop_background()
        await self.run_supervisor.kill_active_processes()
        await self.engine.dispose()


def _supervisor_channels(settings: Settings) -> dict[str, SupervisorChannel]:
    """按配置装配出站通道：没配凭据就退化为只记日志的空通道。"""
    channels: dict[str, SupervisorChannel] = {}
    feishu_key = settings.supervisor_feishu_source_key
    if settings.supervisor_feishu_app_id and settings.supervisor_feishu_app_secret:
        channels[feishu_key] = FeishuChannel(
            app_id=settings.supervisor_feishu_app_id,
            app_secret=settings.supervisor_feishu_app_secret,
            api_base=settings.supervisor_feishu_api_base,
        )
    else:
        channels[feishu_key] = NullChannel(feishu_key)
    # 微信主动出站要 OpenClaw 网关的发送地址 + 同一个 bridge token；
    # 入站的被动回复不经过这里（直接走 HTTP 响应）。
    wechat_key = settings.supervisor_wechat_source_key
    if settings.supervisor_wechat_send_url and settings.supervisor_wechat_bridge_token:
        channels.setdefault(
            wechat_key,
            WechatChannel(
                send_url=settings.supervisor_wechat_send_url,
                token=settings.supervisor_wechat_bridge_token,
                timeout_seconds=settings.supervisor_wechat_timeout_seconds,
            ),
        )
    else:
        channels.setdefault(wechat_key, NullChannel(wechat_key))
    return channels
