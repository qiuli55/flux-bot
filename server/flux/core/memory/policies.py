"""三层记忆的容量 / TTL 契约（批次② §4.2）。

每层都有容量上限与清理机制；TTL 只给"会过时的偏好类"记忆，规则与沉淀不设过期：

- User Memory：跨 Workspace 的偏好与约定，180 天后自然失效（重新确认即续期）；
- Project Memory：项目沉淀（含决策），不设 TTL——旧结论由容量上限兜底，不做无声删除；
- Environment Memory：平台运行规则，不设 TTL，由平台代码维护（容量只是兜底）。

写入路径在插入后按本表裁剪（`max_entries`），读取路径排除已过期条目。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from flux.enums import MemoryLayer


@dataclass(frozen=True)
class MemoryPolicy:
    """一层的容量与 TTL。ttl 为 None 表示不因时间失效。"""

    max_entries: int
    ttl: timedelta | None = None


#: 单个项目分区的条目上限（只作用于累积型分区；现状型分区本就只有一条）
PROJECT_SECTION_MAX_ENTRIES = 200

#: 单条记忆的字符上限：记忆是"结论"，不是文档搬运
MAX_CONTENT_CHARS = 8000

#: 默认策略；测试/部署可按层覆盖（服务构造时注入）
MEMORY_POLICIES: dict[MemoryLayer, MemoryPolicy] = {
    MemoryLayer.USER: MemoryPolicy(max_entries=200, ttl=timedelta(days=180)),
    MemoryLayer.PROJECT: MemoryPolicy(max_entries=PROJECT_SECTION_MAX_ENTRIES, ttl=None),
    MemoryLayer.ENVIRONMENT: MemoryPolicy(max_entries=50, ttl=None),
}

#: 平台维护的 Environment Memory 真源：(key, content)。
#: key 稳定，内容随平台版本更新（服务按 key 幂等 upsert）；不暴露任何外部写入口。
ENVIRONMENT_MEMORY_SEED: tuple[tuple[str, str], ...] = (
    (
        "runtime.proposal_required",
        "改动一律经 proposal.create 提交，由 Flux 完成验证、人审与落盘；不要假设可以直接写工作区。",
    ),
    (
        "runtime.mcp_fallback",
        "MCP 不可用时用 Flux Server CLI 兜底继续工作：flux tools call <tool> --params '<json>'。",
    ),
    (
        "runtime.memory_write",
        "记忆写入必须走受控路径：User Memory 仅由用户确认后写入，Agent 不得直写；"
        "Project Memory 由扫描与工作沉淀产生。",
    ),
    (
        "runtime.secrets",
        "密钥、令牌、凭证永不入库、不入上下文；疑似凭证的记忆内容会被拒绝写入。",
    ),
)

#: `memory.recall` 固定写进返回包的采信规则（与 context.get 的 note 同一口径）
RECALL_NOTE = (
    "Memory 是被记住的结论（快照），不替代事实核对；"
    "写入必经受控路径——User 记忆由用户确认，Agent 不得直写。"
    "跨 Workspace 仅 User / Environment 两层可见，Project 记忆只在所属项目内出现。"
)
