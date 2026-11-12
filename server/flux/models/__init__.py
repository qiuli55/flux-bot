"""数据模型包。

导入全部模型，保证 Base.metadata 完整（Alembic autogenerate 依赖这一点）。
"""

from flux.models.agent import Agent, AgentExecutionLog, AgentSkill
from flux.models.agent_installation import AgentInstallation
from flux.models.agent_run import AgentRun
from flux.models.agent_token import AgentToken
from flux.models.api_key import ApiKey
from flux.models.apply_batch import ApplyBatch
from flux.models.base import Base
from flux.models.bot import Bot
from flux.models.bot_memory import BotMemory, BotMemoryChunk
from flux.models.bot_notification import BotNotification
from flux.models.bot_routine import BotRoutine
from flux.models.bot_turn import BotTurn
from flux.models.connector import ConnectorConfig, ConnectorLog
from flux.models.context import ContextBlob, ContextStep, ContextStepToken
from flux.models.device import Device, PairingCode
from flux.models.github import GithubConnection
from flux.models.group import (
    ChatGroup,
    ChatGroupEpoch,
    ChatGroupMember,
    ChatGroupMemberState,
    ChatGroupMemory,
    ChatGroupMessage,
)
from flux.models.imported_capability import ImportedCapability
from flux.models.knowledge import KnowledgeChunk, KnowledgeDocument
from flux.models.memory import MemoryEntry
from flux.models.project import Project, ProjectMemory
from flux.models.run_trace import RunTraceItem
from flux.models.runtime_session import TaskRuntimeSession
from flux.models.supervisor import (
    SupervisorAssignment,
    SupervisorDigest,
    SupervisorEval,
    SupervisorMandate,
    SupervisorOutbox,
    SupervisorPolicy,
    SupervisorReport,
    SupervisorSettings,
    SupervisorSource,
    SupervisorSubscription,
    SupervisorThread,
    SupervisorTicket,
    SupervisorTrigger,
)
from flux.models.task import Task, TaskMessage
from flux.models.task_memory import TaskMemoryCard
from flux.models.terminal import TerminalEvent, TerminalSession
from flux.models.usage import UsageRecord, UsageSnapshot
from flux.models.user import Organization, OrganizationMember, ProviderAccount, User
from flux.models.wechat import WechatSession
from flux.models.workspace import VirtualChange

__all__ = [
    "Agent",
    "AgentExecutionLog",
    "AgentInstallation",
    "AgentRun",
    "AgentSkill",
    "AgentToken",
    "ApiKey",
    "ApplyBatch",
    "Base",
    "Bot",
    "BotMemory",
    "BotMemoryChunk",
    "BotNotification",
    "BotRoutine",
    "BotTurn",
    "ChatGroup",
    "ChatGroupEpoch",
    "ChatGroupMember",
    "ChatGroupMemberState",
    "ChatGroupMemory",
    "ChatGroupMessage",
    "ConnectorConfig",
    "ConnectorLog",
    "ContextBlob",
    "ContextStep",
    "ContextStepToken",
    "Device",
    "GithubConnection",
    "ImportedCapability",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "MemoryEntry",
    "Organization",
    "OrganizationMember",
    "Project",
    "ProjectMemory",
    "PairingCode",
    "ProviderAccount",
    "RunTraceItem",
    "SupervisorAssignment",
    "SupervisorDigest",
    "SupervisorEval",
    "SupervisorMandate",
    "SupervisorOutbox",
    "SupervisorPolicy",
    "SupervisorReport",
    "SupervisorSettings",
    "SupervisorSource",
    "SupervisorSubscription",
    "SupervisorThread",
    "SupervisorTicket",
    "SupervisorTrigger",
    "Task",
    "TaskMemoryCard",
    "TaskMessage",
    "TaskRuntimeSession",
    "TerminalEvent",
    "TerminalSession",
    "UsageRecord",
    "UsageSnapshot",
    "User",
    "VirtualChange",
    "WechatSession",
]
