"""API 请求模型（主规格 §12 各接口的入参契约）。"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from flux.enums import (
    AcceptMode,
    AgentRole,
    BotMemoryKind,
    BotMemoryRelevance,
    BotStatus,
    BrainSection,
    Capability,
    CapabilityKind,
    DecisionMode,
    ModelProvider,
    PermissionMode,
    TaskOrigin,
)


class AgentCreateRequest(BaseModel):
    """登记一个 Agent 档案（身份 + 权限边界）。

    模型与 system prompt 不在登记范围内——那是 Agent 自己的事（目标架构 §1）。
    """

    name: str = Field(min_length=1, max_length=128)
    role: AgentRole
    description: str = ""
    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    permissions: list[Capability] = Field(default_factory=list)
    #: 执行该 Agent 的 runtime（P2-1 §6.2）：内置 DSH 默认，或本机 CLI（codex / opencode）。
    #: 取值在边界即校验（非法 → 422），并由 AgentManager 再校验一次（内部调用方同样受约束）。
    runtime: Literal["dsh", "codex", "opencode"] = "dsh"


class DevicePairRequest(BaseModel):
    """手机用一次性配对码兑换设备令牌。"""

    code: str = Field(min_length=1, max_length=32)
    device_name: str = Field(default="", max_length=128)


class AgentTokenIssueRequest(BaseModel):
    """为某个 Agent 签发一枚 MCP 接入令牌（目标架构 §3.2）。

    `scopes` 决定这枚令牌能调用哪些工具；`secret.access` 不在可授予范围内（§3.5）。
    明文令牌只在签发响应里出现一次，之后任何接口都取不回——丢了就重新签一枚。
    """

    # 默认只给只读：让"什么都没配"的调用方拿到的是最小权限，而不是能写提案的权限
    scopes: list[Capability] = Field(default_factory=lambda: [Capability.FILE_READ])
    label: str = Field(default="", max_length=128)


class TaskCreateRequest(BaseModel):
    description: str = Field(min_length=1)
    project_id: str | None = None
    agent_id: str | None = None
    # 数字越小越优先（主规格 §5.1 调度依据）
    priority: int = 100
    # 任务级决策策略（文档 §5）：建任务时就定下来，之后可单独切换
    decision_mode: DecisionMode = DecisionMode.AUTO
    # 对话级权限模式（2026-10-25）：新建对话默认「正常模式」
    permission_mode: PermissionMode = PermissionMode.NORMAL
    # 任务级模型选择：model + key_id。key_id 留空 = 原生模型；两者都留空 = Agent 默认。
    model: str | None = Field(default=None, max_length=128)
    key_id: str | None = Field(default=None, max_length=64)
    # 「上下文快满 → 新建对话带摘要」：旧对话的前情摘要随新任务一起落库，
    # 既进入助手历史，也拼进 Agent 执行指令；summary_from 仅用于溯源旧对话。
    summary: str | None = Field(default=None, max_length=8000)
    summary_from: str | None = Field(default=None, max_length=64)
    # 随首条消息一起发来的附件（先 POST /uploads 换 id，再在发送时带上）
    attachment_ids: list[str] = Field(default_factory=list, max_length=5)
    # 对话来源（2026-11-03）：助理页建的任务传 assistant，首页普通对话保持默认 chat。
    origin: TaskOrigin = TaskOrigin.CHAT
    # Bot 档案（2026-11-04）：每条对话都是一个 bot，群聊里用名称 + 头像区分。
    # 头像只接受「形状 × 颜色」组合，取值见 flux.core.group.avatar。
    bot_name: str | None = Field(default=None, max_length=64)
    avatar_shape: str | None = Field(default=None, max_length=32)
    avatar_color: str | None = Field(default=None, max_length=32)
    # 建 bot 时先落一条空对话、不把它当成需求去跑 Agent；第一条消息再走正常对话链路。
    start: bool = True


class BotCreateRequest(BaseModel):
    """创建一个常驻 Bot（Bot 模式）。

    与对话不同，Bot 有自己的工作目录和固定的 Agent / 权限模式，可以按 cron 定时开工。
    `description` 是它的固定作业内容：定时触发时就是本轮的执行指令。
    `schedule` 为 5 段 cron（分 时 日 月 周），留空表示只手动触发。
    """

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=8000)
    avatar_shape: str | None = Field(default=None, max_length=32)
    avatar_color: str | None = Field(default=None, max_length=32)
    agent_id: str | None = None
    model: str | None = Field(default=None, max_length=128)
    key_id: str | None = Field(default=None, max_length=64)
    #: 无人值守是 Bot 的常态；需要人工把关时显式改成 normal / plan / full
    permission_mode: PermissionMode = PermissionMode.UNATTENDED
    decision_mode: DecisionMode = DecisionMode.AUTO
    #: 独立工作目录（绝对路径）；留空 = <dsh_home>/bots/<id>/workspace
    workspace: str | None = Field(default=None, max_length=512)
    schedule: str | None = Field(default=None, max_length=64)
    #: 连续失败多少次自动暂停；0 = 永不自动暂停
    failure_threshold: int = Field(default=3, ge=0, le=20)
    #: 每日 token 预算（按 FLUX_TIMEZONE 的自然日）；留空或 0 = 无上限
    token_budget: int | None = Field(default=None, ge=0, le=1_000_000_000)
    #: 开工失败时是否主动通知（写一条 bot_notifications）
    notify_on_failure: bool = True
    #: 可触发本 Bot 的事件名（如 task.completed）；留空 = 不响应事件
    event_triggers: list[str] | None = Field(default=None, max_length=32)
    #: 允许调用的 MCP 工具名；留空 = 不额外限制
    allowed_tools: list[str] | None = Field(default=None, max_length=64)
    #: 允许执行的命令首 token；留空 = 不限制
    allowed_commands: list[str] | None = Field(default=None, max_length=64)
    #: 允许访问的路径前缀（相对工作目录）；留空 = 不限制
    allowed_paths: list[str] | None = Field(default=None, max_length=64)


class BotUpdateRequest(BaseModel):
    """部分更新：只改请求里出现的字段。

    用 `model_fields_set` 区分「没传」与「显式传 null 清空」——否则前端只想改名，
    却会把 schedule / workspace 一起清掉。
    """

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=8000)
    avatar_shape: str | None = Field(default=None, max_length=32)
    avatar_color: str | None = Field(default=None, max_length=32)
    agent_id: str | None = None
    model: str | None = Field(default=None, max_length=128)
    key_id: str | None = Field(default=None, max_length=64)
    permission_mode: PermissionMode | None = None
    decision_mode: DecisionMode | None = None
    workspace: str | None = Field(default=None, max_length=512)
    schedule: str | None = Field(default=None, max_length=64)
    status: BotStatus | None = None
    failure_threshold: int | None = Field(default=None, ge=0, le=20)
    #: 每日 token 预算；显式传 null 表示改为无上限
    token_budget: int | None = Field(default=None, ge=0, le=1_000_000_000)
    notify_on_failure: bool | None = None
    #: 显式传 null / [] 表示清空事件触发
    event_triggers: list[str] | None = Field(default=None, max_length=32)
    #: 显式传 null / [] 表示取消白名单限制
    allowed_tools: list[str] | None = Field(default=None, max_length=64)
    allowed_commands: list[str] | None = Field(default=None, max_length=64)
    allowed_paths: list[str] | None = Field(default=None, max_length=64)


class BotRoutineCreateRequest(BaseModel):
    """给 Bot 加一个例程：自己的指令 + 自己的定时。"""

    name: str = Field(min_length=1, max_length=64)
    instruction: str = Field(default="", max_length=8000)
    #: 5 段 cron；留空 = 只手动触发
    schedule: str | None = Field(default=None, max_length=64)
    enabled: bool = True


class BotRoutineUpdateRequest(BaseModel):
    """部分更新例程；`schedule` 显式传 null 表示改成手动触发。"""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    instruction: str | None = Field(default=None, max_length=8000)
    schedule: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None


class BotRunRequest(BaseModel):
    """手动触发一次开工；`instruction` 留空则用 Bot 自己的 description。"""

    instruction: str | None = Field(default=None, max_length=8000)


class BotMemoryCreateRequest(BaseModel):
    """手工给 Bot 记一条长期记忆（第一人称）。

    自动沉淀来自群聊与开工；这里用于用户直接告诉 Bot「你该记住这件事」。
    """

    content: str = Field(min_length=1, max_length=8000)
    kind: BotMemoryKind = BotMemoryKind.FACT
    relevance: BotMemoryRelevance = BotMemoryRelevance.DIRECT


class BotTurnCreateRequest(BaseModel):
    """给 Bot 发一条对话消息（异步轮次）。

    HTTP 只落 queued 就返回；客户端用 GET /bots/{id}/turns/{turn_id} 长轮询结果。
    `client_turn_id` 是幂等键：网络重试带同一个值不会产生第二轮。
    """

    content: str = Field(min_length=1, max_length=20_000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=5)
    client_turn_id: str | None = Field(default=None, max_length=128)


class TaskMessageCreateRequest(BaseModel):
    """发一条任务消息（任务执行中心的对话输入）。

    只有 user 能由调用方写入：assistant 的回复必须来自平台真实模型调用，
    不接受客户端伪造（role 不在请求体里，由服务端决定）。
    正文可以留空——只发附件也是合法的一条消息（"看看这个"）。
    """

    content: str = Field(default="", max_length=8000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=5)


class SecureInputRequest(BaseModel):
    """在安全输入卡片里提交一个密钥（明文只出现在这一次请求里，落库即加密）。

    `message_id` 指向那张助手卡片：密钥的用途（provider / models / 名称）以卡片上的
    描述为准，客户端只需要回传用户填的值，不能借卡片之外的参数改写入库字段。
    """

    message_id: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=4096)


class ConfirmationItem(BaseModel):
    """需求确认的一项：维度 + 结论（P0-06 六个固定维度，label 由服务端校验）。"""

    label: str = Field(min_length=1, max_length=32)
    value: str = Field(min_length=1, max_length=2000)


class ConfirmationUpdateRequest(BaseModel):
    """用户改后的需求确认内容：必须恰好覆盖六个维度（服务端 fail-closed 校验）。"""

    items: list[ConfirmationItem] = Field(min_length=1, max_length=8)


class DecisionModeRequest(BaseModel):
    """切换任务级决策策略（文档 §5 模式 A / B）。"""

    mode: DecisionMode


class PermissionModeRequest(BaseModel):
    """切换对话级权限模式（Plan / Normal / Full）。

    任何任务状态都允许切换；正在执行的 Run 按启动时的模式跑完，新模式从下一轮生效。
    """

    mode: PermissionMode


class TaskAgentRequest(BaseModel):
    """切换任务的执行 Agent：同一对话继续，后续 Run 交给新 Agent。"""

    agent_id: str = Field(min_length=1)


class TaskModelRequest(BaseModel):
    """切换任务的执行模型：必须是该 Agent runtime 支持的模型。

    `key_id` 非空表示模型来自密钥库；留空 = 原生模型。
    `model` 留空表示清掉任务级覆盖，回到 Agent 默认模型（App 的「跟随默认」）。
    """

    model: str = Field(default="", max_length=128)
    key_id: str | None = Field(default=None, max_length=64)


class ApiKeyCreateRequest(BaseModel):
    """在密钥库中登记一条 API Key。明文只在请求体里出现，落库即加密。"""

    name: str = Field(min_length=1, max_length=128)
    provider: Literal["deepseek", "openai", "openai_compatible", "anthropic"]
    base_url: str = Field(default="", max_length=256)
    api_key: str = Field(min_length=1, max_length=4096)
    models: list[str] = Field(default_factory=list)
    #: codex 的 wire_api：空 = 按供应商推导；兼容端点可选 chat / responses
    wire_api: str = Field(default="", max_length=16)
    enabled: bool = True


class ApiKeyUpdateRequest(BaseModel):
    """修改密钥库条目；未出现的字段保持不变，api_key 留空也表示不改。"""

    name: str | None = Field(default=None, max_length=128)
    provider: Literal["deepseek", "openai", "openai_compatible", "anthropic"] | None = None
    base_url: str | None = Field(default=None, max_length=256)
    api_key: str | None = Field(default=None, max_length=4096)
    models: list[str] | None = None
    wire_api: str | None = Field(default=None, max_length=16)
    enabled: bool | None = None


class DecisionOption(BaseModel):
    """一个候选方案：做什么、带来什么影响、是不是推荐项。"""

    label: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=2000)
    impact: str | None = Field(default=None, max_length=2000)
    recommended: bool = False


class DecisionCreateRequest(BaseModel):
    """Agent 遇到决策点时向平台登记（文档 §5）：平台按任务策略自动拍板或挂起等用户。"""

    question: str = Field(min_length=1, max_length=2000)
    options: list[DecisionOption] = Field(min_length=2, max_length=6)
    context: str | None = Field(default=None, max_length=4000)
    recommendation: str | None = Field(default=None, max_length=2000)


class DecisionChooseRequest(BaseModel):
    """用户对挂起的决策点做选择：choose 需要给出 option，reject 表示全部候选都不接受。"""

    decision_id: str = Field(min_length=1, max_length=64)
    action: Literal["choose", "reject"] = "choose"
    option: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=2000)


class TaskStartRequest(BaseModel):
    """开始执行（P0-05）。confirmation 留空表示直接使用任务上已保存的确认内容。"""

    confirmation: list[ConfirmationItem] | None = None


class ChangeIdsRequest(BaseModel):
    change_ids: list[str] = Field(min_length=1)


class RejectRequest(ChangeIdsRequest):
    reason: str | None = None


class ExpireRequest(ChangeIdsRequest):
    """显式让一批提案失效（P0-02）。reason 留空时用默认的"过期"原因。"""

    reason: str | None = None


class GroupRequest(BaseModel):
    """按一次提交（group_id）整组操作（P0-02）。"""

    group_id: str = Field(min_length=1)
    reason: str | None = None


class RecoveryResolveRequest(BaseModel):
    """对一条崩溃恢复挂起项做人工决策（P0-1 §2.2，2026-10-05 定案）。

    cover = 用备份覆盖当前内容（还原为改动前原文，提案回 accepted，可重试）；
    keep = 保持磁盘现状、绝不覆盖用户改动（提案留 failed 作废）。
    """

    change_id: str = Field(min_length=1)
    action: Literal["cover", "keep"]


class WorkspaceRootRequest(BaseModel):
    """Native desktop-selected local workspace root."""

    root: str = Field(min_length=1, max_length=4096)


class WorkspaceFileCreateRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(default="", max_length=262144)


class WorkspaceFileWriteRequest(BaseModel):
    """编辑器保存：覆写一个已存在的文件。"""

    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(default="", max_length=262144)
    workspace_root: str | None = Field(default=None, max_length=4096)


class WorkspaceFilePathRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class WorkspaceFileRenameRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    new_path: str = Field(min_length=1, max_length=4096)


class WorkspaceFileReplaceRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    replacement: str = Field(max_length=10000)
    path: str | None = Field(default=None, max_length=4096)
    case_sensitive: bool = False
    regex: bool = False


class TerminalSessionCreateRequest(BaseModel):
    """开一个终端会话（Agent Terminal Console §6）。工作区根由服务端配置决定，不接受客户端指定。"""

    run_id: str | None = None


class TerminalCommandRequest(BaseModel):
    """在终端会话里执行一条命令（§4）。T1 只接 USER 来源；T2 起 Agent 走同一 Session Manager。"""

    command: str = Field(min_length=1, max_length=4000)


class TerminalStopRequest(BaseModel):
    """停止终端会话（§3.3 / §3.4）：force=true 直接 SIGKILL，false 走 SIGTERM → grace → SIGKILL。"""

    force: bool = False


class ChatMessageIn(BaseModel):
    role: str = Field(pattern="^(system|user|assistant)$")
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessageIn] = Field(min_length=1)
    model: str | None = None
    provider: ModelProvider | None = None
    task_id: str | None = None
    project_id: str | None = None


class FimRequest(BaseModel):
    """行内补全（Fill-In-the-Middle）请求（AI_COMPLETION_SPEC §5.4）。

    服务端一次性构建三层上下文：`prefix`/`suffix` 是文件级 L0，`context` 可携带
    扩展已取到的 L1/L2 片段（可选，缺省时服务端自行从工作区补齐）。
    """

    prefix: str = ""
    suffix: str = ""
    # 扩展显式传入的上下文快照（见 /ide/completion/context 响应结构）；缺省时服务端构建
    context: dict[str, Any] | None = None
    path: str | None = Field(default=None, max_length=1024)
    project_id: str | None = Field(default=None, max_length=64)
    language_id: str | None = Field(default=None, max_length=64)
    # 光标位置（UTF-16 偏移或行号均可，仅用于上下文切片，不参与语义）
    caret: int | None = Field(default=None, ge=0)
    text: str | None = None
    # 模型解析：显式 model/key_id 优先；留空走 follow-agent → 偏好 → 默认
    model: str | None = Field(default=None, max_length=128)
    key_id: str | None = Field(default=None, max_length=64)
    agent_id: str | None = Field(default=None, max_length=64)
    max_tokens: int | None = Field(default=None, ge=16, le=8192)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    stop: list[str] = Field(default_factory=list, max_length=8)
    # 生成后最多保留多少行（多出的截断），由服务端统一处理，避免前端补丁式截断
    max_lines: int = Field(default=8, ge=1, le=64)


class CompletionContextRequest(BaseModel):
    """补全上下文查询（AI_COMPLETION_SPEC §3.2）：只读，不写任何文件。"""

    project_id: str | None = Field(default=None, max_length=64)
    path: str = Field(min_length=1, max_length=1024)
    caret: int | None = Field(default=None, ge=0)
    text: str | None = None
    language_id: str | None = Field(default=None, max_length=64)


class CompletionPrefsRequest(BaseModel):
    """补全偏好（AI_COMPLETION_SPEC §5.3）。

    所有字段都是可选的：未传的字段保持原值，避免 IDE 只切换开关时
    把桌面端 / Web 端已经选好的模型悄悄重置回默认。
    """

    enabled: bool | None = None
    model: str | None = Field(default=None, max_length=128)
    debounce_ms: int | None = Field(default=None, ge=100, le=2000)
    max_lines: int | None = Field(default=None, ge=1, le=64)


class ActiveAgentRequest(BaseModel):
    """当前生效的 Agent 选择（AI_COMPLETION_SPEC §7）。

    `model`/`key_id` 留空表示沿用该 Agent 自身默认。
    """

    agent_id: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    key_id: str | None = Field(default=None, max_length=64)


class CompletionAcceptedRequest(BaseModel):
    """补全接受埋点（AI_COMPLETION_SPEC §8）：只做计数，不记录代码内容。"""

    accepted: bool = True
    #: 展示这条补全时服务端实际用的通道（`fim` / `chat`），用于分模式看接受率
    mode: str | None = Field(default=None, max_length=16)
    model: str | None = Field(default=None, max_length=128)
    language_id: str | None = Field(default=None, max_length=64)
    #: 整行模式 / 插入模式的区分，便于对比两种交互的接受率
    completion_mode: str | None = Field(default=None, max_length=16)


class ConnectorExecuteRequest(BaseModel):
    connector: str = Field(min_length=1)
    action: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    # 权限由服务端按 agent_id 解析（见 api/v1/connectors.py），客户端无法自行声明能力
    agent_id: str | None = None


class GitPathsRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=200)


class GitDiffRequest(BaseModel):
    """查看差异。paths 为空表示整个工作区。"""

    paths: list[str] = Field(default_factory=list)
    staged: bool = False


class GitCheckoutRequest(BaseModel):
    target: str = Field(min_length=1, max_length=255)
    # True = 新建并切换（git checkout -b），False = 切换到已有分支
    create: bool = False


class GitCommitRequest(BaseModel):
    """提交改动。message 由调用方给出（用户可改），Agent 无权直接决定提交信息。"""

    message: str = Field(min_length=1, max_length=2000)
    # 只允许提交 applied 状态的提案（已批准 + 已落盘 + 测试通过，§7.6）
    change_ids: list[str] = Field(default_factory=list)
    # 也可直接指定路径；与 change_ids 都为空时提交暂存区已有内容
    paths: list[str] = Field(default_factory=list)


class ProjectCreateRequest(BaseModel):
    """登记一个项目（⑪ Project Brain 的宿主实体）。"""

    name: str = Field(min_length=1, max_length=128)
    repository: str | None = Field(default=None, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryWriteRequest(BaseModel):
    """写一条项目记忆。分区语义见 flux.enums.BrainSection。"""

    section: BrainSection
    content: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class UserMemoryWriteRequest(BaseModel):
    """写一条 User Memory（跨 Workspace；只有用户侧入口，Agent 无直写通道）。

    上限与 `flux.core.memory.policies.MAX_CONTENT_CHARS` 对齐；密钥/令牌/凭证
    由服务层再次拦截（此处只做长度与形态校验，不做安全判定）。
    """

    content: str = Field(min_length=1, max_length=8000)
    source: str = Field(default="user", max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)


class UserMemorySettingsRequest(BaseModel):
    """设备侧「自动记录用户记忆」开关。"""

    auto_capture: bool


class ScanRequest(BaseModel):
    """扫描被登记项目的工作区目录（⑩）。

    workspace_root 留空表示用服务端配置的 FLUX_WORKSPACE_ROOT；record=false
    时只返回画像，不把结果写进 Project Brain。
    """

    workspace_root: str | None = None
    record: bool = True


class CapabilityImportRequest(BaseModel):
    """导入一个扫描到的能力（批次③ §5）。

    - kind + name 定位候选；候选必须来自服务端扫描，调用方不能注入 spec；
    - 重复导入且内容有变化时，decision 必须显式给出 keep（保留现有）或
      replace（替换）；缺省时接口返回 409 冲突，details 里带字段级差异。
    """

    kind: CapabilityKind
    name: str = Field(min_length=1, max_length=128)
    decision: Literal["keep", "replace"] | None = None


class InstallationConnectRequest(BaseModel):
    """接入一个本机 CLI Agent（或全部已安装的）。

    - `agent`：adapter 名（codex / opencode），必须是服务端内置 adapter 之一；
    - `all=true`：接入全部**已安装**的 Agent（未安装的跳过，不导致整体失败）；
    - 两者要么给一个，要么给 all，都不给按 422 拒绝（避免"接入什么"含糊不清）。
    """

    agent: str | None = Field(default=None, max_length=64)
    all: bool = False


class SupervisorEventRequest(BaseModel):
    """一条外部事件（飞书 / 微信 / Flux 内部）。

    `external_id` 必须是平台侧稳定标识：重复投递靠 `(source_key, event_type,
    external_id)` 幂等，重投只会命中同一条 trigger，不会再排一次动作。
    """

    source_key: str = Field(min_length=1, max_length=64)
    external_id: str = Field(min_length=1, max_length=255)
    event_type: str = Field(default="message", max_length=64)
    actor_id: str = Field(default="", max_length=255)
    actor_platform: str = Field(default="", max_length=32)
    thread_key: str = Field(default="", max_length=255)
    project_id: uuid.UUID | None = None
    content: str = Field(default="", max_length=20000)
    raw: dict[str, Any] = Field(default_factory=dict)


class SupervisorSettingsRequest(BaseModel):
    """Supervisor 设置的局部更新：字段为 None 表示"不改"。"""

    accept_mode: AcceptMode | None = None
    model_provider: str | None = Field(default=None, max_length=32)
    model_name: str | None = Field(default=None, max_length=128)
    max_tickets_per_thread: int | None = Field(default=None, ge=1, le=20)
    max_ticket_depth: int | None = Field(default=None, ge=1, le=3)
    max_concurrent_runs: int | None = Field(default=None, ge=1, le=8)


class SupervisorActionRequest(BaseModel):
    """工单动作（停止等）的附加说明；只做审计，不参与派发决策。"""

    note: str = Field(default="", max_length=2000)


class SupervisorSubscriptionRequest(BaseModel):
    """给工单订阅一个平台会话：报告落库后按 `mode` 直通投递回该会话。"""

    source_key: str = Field(min_length=1, max_length=64)
    external_id: str = Field(min_length=1, max_length=255)
    mode: str = Field(default="terminal", max_length=32)


class GroupCreateRequest(BaseModel):
    """创建一个内部多 bot 群聊；成员是已有的常驻 Bot（bots.id）。"""

    name: str = Field(min_length=1, max_length=128)
    project_id: str | None = None
    description: str | None = Field(default=None, max_length=2000)
    #: 每个 epoch 的最大轮数（结构性闸门，不是预算）
    max_rounds: int = Field(default=2, ge=1, le=5)
    bot_ids: list[str] = Field(default_factory=list, max_length=20)
    #: 指定群主（成员之一）；不指定则建群后由用户手动设置
    admin_bot_id: str | None = None


class GroupMemberAddRequest(BaseModel):
    """把一个 Bot 拉进群。"""

    bot_id: str = Field(min_length=1)
    role: Literal["admin", "member"] = "member"


class GroupMemberUpdateRequest(BaseModel):
    """调整群成员的角色 / 模型 / 权限预设。

    只能由人通过 API 修改：bot 在群里的发言不会改自己的角色或权限。
    字段留空表示保持不变。
    """

    role: Literal["admin", "member"] | None = None
    model: str | None = Field(default=None, max_length=128)
    key_id: str | None = Field(default=None, max_length=64)
    permission_mode: PermissionMode | None = None


class GroupMessageCreateRequest(BaseModel):
    """在群里发一条消息；会打断正在进行的 epoch 并开启新一轮。"""

    content: str = Field(min_length=1, max_length=8000)
