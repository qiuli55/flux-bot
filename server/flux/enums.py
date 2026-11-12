"""Flux 全局枚举（主规格 §3 术语 / §5.1 生命周期 / §5.5 权限 / §11 状态）。"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """Python 3.10 兼容的字符串枚举基类（3.11 起可用 enum.StrEnum）。"""

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志与序列化
        return str(self.value)


class AgentState(StrEnum):
    """Agent 生命周期状态机（主规格 §5.1）。"""

    CREATED = "CREATED"
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_TOOL = "WAITING_TOOL"
    REVIEWING = "REVIEWING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    # 源状态机未定义停止态，但 Code Implementation Specification 要求 Agent 接口提供 stop()，
    # 故补 STOPPED（主规格附录 A 裁决 A15）。
    STOPPED = "STOPPED"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    #: 执行中撞上需要用户拍板的决策点（文档 §5 模式 B）：任务停在原地等人，
    #: 既不是失败也不是完成——用户长时间不响应时状态必须保持在这里。
    WAITING_FOR_USER_DECISION = "waiting_for_user_decision"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskOrigin(StrEnum):
    """对话来源（2026-11-03）：首页的普通对话，或助理页的助理对话。

    助理对话同样是一条真实任务（能跑 Agent、选模型），只是不进首页对话列表，
    避免助理的工作会话把用户自己的对话挤下去。读取全部对话时默认也会排除它。
    """

    CHAT = "chat"
    ASSISTANT = "assistant"


class BotStatus(StrEnum):
    """常驻 Bot 的生命周期（bots.status）。

    ACTIVE 才会被定时调度；PAUSED 保留配置与历史，但不再自动开工。
    """

    ACTIVE = "active"
    PAUSED = "paused"


class BotMemoryRelevance(StrEnum):
    """Bot 记忆与自己关系的三档（无关内容不落库，所以只有两档）。"""

    DIRECT = "direct"
    INDIRECT = "indirect"


class BotMemoryKind(StrEnum):
    """Bot 记忆的类别（bot_memories.kind）。"""

    PREFERENCE = "preference"
    FACT = "fact"
    DECISION = "decision"
    COMMITMENT = "commitment"
    SUMMARY = "summary"


class BotMemorySource(StrEnum):
    """Bot 记忆的来源（bot_memories.source）。"""

    GROUP = "group"
    CHAT = "chat"
    MANUAL = "manual"


class DecisionMode(StrEnum):
    """任务级决策策略（文档 §5）：Agent 遇到可选方案时是自己拍板还是停下来问用户。"""

    AUTO = "auto"
    MANUAL = "manual"


class DecisionStatus(StrEnum):
    """一个决策点的状态（文档 §5）。"""

    PENDING = "pending"
    #: auto 模式：平台按推荐方案自行拍板，不打断用户
    AUTO_RESOLVED = "auto_resolved"
    #: manual 模式：用户已在候选方案里选了一个
    RESOLVED = "resolved"
    #: 用户拒绝全部候选方案，要求重新给方案
    REJECTED = "rejected"
    #: 审批超时（10 分钟无人响应）：命令不执行，任务回到 running，Agent 需重新征询
    EXPIRED = "expired"


class PermissionMode(StrEnum):
    """对话级权限模式（2026-10-25 三模式定案 + 2026-10-26 免审核）。

    - PLAN：只读 + 只写计划文档，不执行 shell、不改代码、不分享设计稿；
    - NORMAL（默认）：正常执行，低风险命令自动放行，高风险命令逐次等用户允许；
    - FULL：跳过逐步审批（可直写工作区、跑 git 提交推送），但受保护对象仍需逐次申请；
    - UNATTENDED：免审核（无人值守），高风险命令与受保护对象都不再打断用户。
      仅用于用户明确离开设备、希望任务自行跑完的场景；风险与后果由用户承担。
    """

    PLAN = "plan"
    NORMAL = "normal"
    FULL = "full"
    UNATTENDED = "unattended"


class TaskMessageRole(StrEnum):
    """任务对话消息的说话方（task_messages.role）。"""

    USER = "user"
    ASSISTANT = "assistant"


class VirtualChangeStatus(StrEnum):
    """Virtual Workspace 文件状态机（主规格 §7.2）。

    FAILED 用于 Apply 阶段：补丁打不上或校验不通过时留下终态记录（实施计划 §5 状态列表），
    不允许从 FAILED 回到任何可执行状态——失败原因必须由人重新生成提案。

    EXPIRED 用于提案过期/失效（P0-02）：超过 TTL 仍未审核，或同文件被更新的提案取代时，
    提案进入 EXPIRED 终态，不允许再被批准或落盘——审核队列里不该长期挂着改不动的旧提案。

    APPLYING 是 Apply 进行中的中间态（P0-1）：进入它之前 apply_batches 已落 in_progress 行，
    服务被杀后由崩溃恢复把提案退回 ACCEPTED（附 recovery_note），磁盘按批日志对账还原。
    """

    PENDING = "pending"
    ACCEPTED = "accepted"
    APPLYING = "applying"
    REJECTED = "rejected"
    APPLIED = "applied"
    FAILED = "failed"
    EXPIRED = "expired"
    #: ROLLED_BACK 是 P1-2 正式回滚写入的终态：已落盘的提案被整体撤销、文件还原为改动前。
    #: 与 APPLIED 一样不可再被批准/落盘——要再改同一文件必须重新提交提案。
    ROLLED_BACK = "rolled_back"


class ChangeKind(StrEnum):
    """virtual_changes.kind：一条提案对文件的动作类型（P1-1）。

    CREATE 新建、MODIFY 修改、DELETE 删除。缺省由 parser 留空、服务层按"原文件是否存在"
    自动判定；DELETE 的 proposed_content 为 NULL（没有"改动后内容"这回事）。
    """

    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"


class ApplyBatchStatus(StrEnum):
    """apply_batches.status（P0-1 的 Apply 事务日志）。

    取值即终态集合：in_progress 只在"批正在跑或崩溃后尚未对账"时出现；
    recovered 表示崩溃恢复已按磁盘事实处理完；needs_attention 表示恢复时发现外部修改、
    无法安全还原，等人处理。ROLLED_BACK 由 P1-2 的正式回滚写入。
    """

    IN_PROGRESS = "in_progress"
    APPLIED = "applied"
    FAILED = "failed"
    RECOVERED = "recovered"
    NEEDS_ATTENTION = "needs_attention"
    ROLLED_BACK = "rolled_back"


class RecoveryResolution(StrEnum):
    """virtual_changes.recovery_resolution：崩溃恢复挂起项的人工决策结果（P0-1 §2.2）。

    为空表示"尚未决策"。COVER = 用备份覆盖当前内容（还原为改动前原文，提案回 accepted）；
    KEEP = 保持磁盘现状、不覆盖用户改动（提案留 failed 作废）。写下即为终态，不可反复。
    备份缺失时只能 KEEP。
    """

    COVER = "cover"
    KEEP = "keep"


class TerminalSessionStatus(StrEnum):
    """terminal_sessions.status（Agent Terminal Console §8 / §12）。"""

    ACTIVE = "active"
    STOPPED = "stopped"
    CLOSED = "closed"


class TerminalSessionKind(StrEnum):
    """terminal_sessions.kind：区分 Agent Terminal 与 Human Terminal，会话持久化层面不可混淆。"""

    AGENT = "agent"
    HUMAN = "human"


class TerminalSource(StrEnum):
    """终端命令的来源：AI 还是 USER。§5 要求两者在输出中必须明确区分。"""

    AI = "ai"
    USER = "user"


class TerminalEventKind(StrEnum):
    """terminal_events.kind（§8 事件模型）。"""

    SESSION_CREATED = "terminal.session.created"
    COMMAND_STARTED = "terminal.command.started"
    OUTPUT = "terminal.output"
    COMMAND_FINISHED = "terminal.command.finished"
    COMMAND_FAILED = "terminal.command.failed"
    STOP_REQUESTED = "terminal.stop.requested"
    PROCESS_TERMINATED = "terminal.process.terminated"
    SESSION_CLOSED = "terminal.session.closed"


class BrainSection(StrEnum):
    """Project Brain 的记忆分区（主规格 §5.6；实施计划 ⑪）。

    `overview` / `tech_stack` / `architecture` / `coding_rules` / `progress` 是
    "项目现状"型分区，每区只保留一份最新内容（重新写入即覆盖）；
    `decisions` / `agent_notes` 是"累积"型分区，每次写入都新增一条，历史不可被覆盖。
    """

    OVERVIEW = "overview"
    TECH_STACK = "tech_stack"
    ARCHITECTURE = "architecture"
    CODING_RULES = "coding_rules"
    #: 由助理在线程收尾后从摘要自动汇总，覆盖写入
    PROGRESS = "progress"
    DECISIONS = "decisions"
    AGENT_NOTES = "agent_notes"


#: 单例分区（写入即覆盖当前内容）
SINGLETON_BRAIN_SECTIONS = frozenset(
    {
        BrainSection.OVERVIEW,
        BrainSection.TECH_STACK,
        BrainSection.ARCHITECTURE,
        BrainSection.CODING_RULES,
        BrainSection.PROGRESS,
    }
)


class MemoryLayer(StrEnum):
    """三层记忆边界（批次② §4.2）。

    - USER：跨 Workspace 的偏好与约定，用户确认后才写，可查可删；
    - PROJECT：每个 Workspace / Project 自动沉淀（底座是 Project Brain）；
    - ENVIRONMENT：Flux 运行规则，全局、由平台代码维护，优先于普通项目上下文。
    """

    USER = "user"
    PROJECT = "project"
    ENVIRONMENT = "environment"


class AcceptMode(StrEnum):
    """Supervisor 的准入模式（参考 DeepSeek Super-agent 的三档）。

    - OFF：只收事件落库，不做任何判断与派活；
    - OBSERVE：跑完整的 Gate + Assessment，把判断结果写成可回放的 observation，
      但不创建 ticket、不起 Run——用来在真实流量上校准判断层；
    - AUTO：判断通过后直接进入编排（plan → dispatch）。
    """

    OFF = "off"
    OBSERVE = "observe"
    AUTO = "auto"


class SupervisorDecision(StrEnum):
    """一次判断的结论。UNAVAILABLE 表示判断层没跑成（模型超时/输出不合协议），
    与 IGNORE 严格区分：前者是"不知道"，后者是"明确不该管"。"""

    ACCEPT = "accept"
    STOP = "stop"
    IGNORE = "ignore"
    UNAVAILABLE = "unavailable"


class MandateStrength(StrEnum):
    """用户授权的强度：显式下达 vs 从上下文推断（推断授权必须更保守）。"""

    EXPLICIT = "explicit"
    INFERRED = "inferred"


class TicketStage(StrEnum):
    """工单生命周期阶段。阶段不落库，由结构化事实（task/report/assignment）派生。"""

    INTAKE = "intake"
    PLANNED = "planned"
    RUNNING = "running"
    WAITING = "waiting"
    REVIEWING = "reviewing"
    DONE = "done"
    STOPPED = "stopped"


class ReportStatus(StrEnum):
    """子 Agent 交回的六态报告（与 TaskStatus 解耦，避免把"任务状态"当"结论"）。"""

    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    NEEDS_INPUT = "needs_input"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class MessageType(StrEnum):
    """Agent 通信协议消息类型（主规格 §12.8）。"""

    REQUEST = "REQUEST"
    RESPONSE = "RESPONSE"
    EVENT = "EVENT"
    ERROR = "ERROR"
    HANDOFF = "HANDOFF"


class DshRunStatus(StrEnum):
    """DSH Agent Run 状态机（修复方案 §2.2）。

    PENDING → STARTING → RUNNING → COMPLETED / FAILED / TIMEOUT，
    取消走 RUNNING → CANCELLING → CANCELLED（进程树确认清理完成才算 CANCELLED），
    重启接管的遗留 Run 归 INTERRUPTED。QUEUED 为 Phase 1 遗留值，保留以免旧数据反序列化失败。
    """

    QUEUED = "queued"
    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class Capability(StrEnum):
    """Capability 权限项（主规格 §5.5 / §8.4）。"""

    FILE_READ = "file.read"
    FILE_WRITE = "file.write"
    TERMINAL_EXECUTE = "terminal.execute"
    GITHUB_CREATE_PR = "github.create_pr"
    BROWSER_OPEN = "browser.open"
    DEPLOY = "deploy"
    SECRET_ACCESS = "secret.access"
    DEVICE_COMMAND = "device.command"
    #: 只写 Flux 自有计划目录（/opt/flux/plans/），不涉及任何代码写入；
    #: Plan 模式下签发的令牌只带 file.read + plan.write。
    PLAN_WRITE = "plan.write"


class CapabilityKind(StrEnum):
    """可导入能力的种类（批次③ §5）：本机 Agent / Skill / Connector。"""

    AGENT = "agent"
    SKILL = "skill"
    CONNECTOR = "connector"


class AgentInstallStatus(StrEnum):
    """本机 Agent 的可接入状态机（最终方案 §3.2 / §4.1）。

    NOT_INSTALLED 是 Scanner 的发现事实（本机没有这个 CLI），不是"流程中的一步"；
    已安装的 Agent 沿 DISCOVERED → VERIFIED → CONNECTED → READY 单向收敛。
    """

    NOT_INSTALLED = "NOT_INSTALLED"
    DISCOVERED = "DISCOVERED"
    VERIFIED = "VERIFIED"
    CONNECTED = "CONNECTED"
    READY = "READY"


class FluxCapability(StrEnum):
    """Flux Runtime 能力项（最终方案 §8）：与权限项 Capability 不同，它描述
    "当前这套 Flux 能不能做这件事"，由 `flux_context.capabilities` 声明，缺失即降级。"""

    CONTEXT = "context"
    WORKSPACE = "workspace"
    PROPOSAL = "proposal"
    APPLY = "apply"
    ROLLBACK = "rollback"
    GIT = "git"


class Role(StrEnum):
    """企业角色（主规格 §5.5）。"""

    ADMIN = "admin"
    DEVELOPER = "developer"
    REVIEWER = "reviewer"
    OBSERVER = "observer"


class ModelProvider(StrEnum):
    """模型供应商（主规格 §5.3 裁决 A4）。

    服务于 Flux 侧基础设施的模型调用（T2 压缩、扫描摘要等）——
    Agent 用什么模型是 Agent 自己的事，不经本枚举（目标架构 §1）；
    Agent 要用的第三方模型仍可经密钥库通道下发。

    除 OpenAI / Anthropic / DeepSeek 外，Flux Bot 还把四家中国区供应商作为
    一等公民接入（Qwen / 智谱 GLM / Kimi / 豆包），它们与 DeepSeek 同属
    OpenAI 兼容协议。`openai_compatible` 仍是通用逃生口，没有枚举值。
    """

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    DEEPSEEK = "deepseek"
    QWEN = "qwen"
    ZHIPU = "zhipu"
    KIMI = "kimi"
    DOUBAO = "doubao"
    LOCAL = "local"


class AgentRole(StrEnum):
    """六个内置 Agent 角色（主规格 §6.1）。"""

    TECH_LEAD = "tech_lead"
    ARCHITECT = "architect"
    DEVELOPER = "developer"
    REVIEWER = "reviewer"
    TESTER = "tester"
    DEVOPS = "devops"


class StepKind(StrEnum):
    """context_steps.kind：一条 step 到底做了什么（FLUX_CONTEXT_MECHANISM §3.3）。"""

    TOOL_CALL = "tool_call"
    COMMAND = "command"
    MODEL_CALL = "model_call"
    FILE_OP = "file_op"
    ASSERTION = "assertion"
    FACT = "fact"


class StepStatus(StrEnum):
    """context_steps.status（§3.3）。

    只有 `ok` 能被当成"这一步做成了"；`partial` 与 `timeout` 都不算，
    否则断言会被后来的失败覆盖成假事实。
    """

    OK = "ok"
    FAIL = "fail"
    PARTIAL = "partial"
    TIMEOUT = "timeout"


class BlobCodec(StrEnum):
    """context_blobs.codec：原文的存储编码（§3.5 / §8.4）。

    三种编码都是**无损**的——I3 说的"不压缩"指语义不压缩，不含存储层编码。
    `raw` 用于压不动或过短的输入；解码一律按本列取值，不看长度猜。
    """

    RAW = "raw"
    ZSTD = "zstd"
    LZMA = "lzma"


class ContextTokenKind(StrEnum):
    """context_step_tokens.kind：反向索引的词条类别（FLUX_CONTEXT_MECHANISM §7.2）。

    对应规范里五类查询中的四类——按文件 / 命令 / URL / 错误关键字找 step；
    第五类「按时间窗」直接查 `context_steps.created_at`，不需要词条。
    """

    FILE = "file"
    COMMAND = "command"
    URL = "url"
    ERROR = "error"


class ClaimKind(StrEnum):
    """被验证的断言类别（FLUX_CONTEXT_MECHANISM §4.4）。

    §4.4 的账本按「**指纹 × 断言类**」记账——同一个 agent 在"我能跑通测试"上可信，
    不代表它在"我改的文件没破坏别的模块"上也可信。分类依据是"拿什么证据核"，
    与 §4.2 的证据四类同源，故两边可以互相转译。
    """

    COMMAND = "command"
    TEST = "test"
    FILE = "file"
    URL = "url"


class VerificationOutcome(StrEnum):
    """一次验证的结论（§4.4）。

    只有两种：通过、或**没通过**。"没通过"既包含"断言是错的"，也包含"被抓到造假"——
    §4.4 的降级规则对两者一视同仁（都回落 L0），细分反而会让规则变糊。
    """

    PASS = "pass"
    FAIL = "fail"


class TrustLevel(StrEnum):
    """信任分级（§4.4）——"不信任"是默认起点，但必须是**动态量**。

    常量式的不信任等于每次接手全量重验，成本一爆，人最后就干脆盲信——这是必然后果，
    不是可能。故引入账本让信任能**长回来**，也能一次被抓就**摔回去**（回落 L0）。
    """

    #: 新指纹 / 曾被证伪 → 全量重验
    L0 = "l0"
    #: 最近一次验证通过，但还没到"稳定" → 抽查
    L1 = "l1"
    #: 稳定 → 只验关键项
    L2 = "l2"
    #: 长期稳定 → 采信但留痕
    L3 = "l3"


class RedactionKind(StrEnum):
    """context_redactions.kind：入站脱敏的标记类别（FLUX_CONTEXT_MECHANISM §10.1）。

    取值与占位符一一对应（`[REDACTED:<value>]`），也是映射表的分类键。同一类可能
    出现多个**不同**原文，故占位符按类递增编号（`[REDACTED:key]`、`[REDACTED:key:2]`…），
    映射因此始终 1:1。
    """

    #: `sk-…` 一类的密钥；熵检测捞到的无前缀密钥也归这里（"key"是兜底的密钥标记）
    KEY = "key"
    #: `-----BEGIN …-----END …-----` 的 PEM 私钥 / 证书块
    PEM = "pem"
    #: `Bearer …` 一类的令牌
    TOKEN = "token"
    #: `user:pass@host` 里的认证段（`@host` 保留，不打码）
    CRED = "cred"
