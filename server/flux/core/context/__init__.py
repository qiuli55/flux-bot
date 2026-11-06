"""上下文机制（FLUX_CONTEXT_MECHANISM）的落地代码。

- `ContentStore`：内容寻址原文库，去重 + 无损压缩（§3.5 / §8.4）。
- `ContextStepRepository`：step 事件日志的读写入口 + 反向索引查询（§3.3 / §7.2）。
- `ContextLifecycle`：按 turn / run 级联删除，含孤儿 blob 回收（§8.2）。
- `build_handoff` / `HandoffBrief`：交接文档生成（§8.3 / §9 九节）。
- `catalog_line` / `render_catalog`：目录行渲染，agent 进入冷库的唯一入口（§3.4 / §6.4）。
- `extract_tokens` / `normalize_token`：反向索引词条抽取与归一化（§7.2）。
- `fact_spine`：事实脊柱——自带证据的 step 收敛成逐代原样透传的行（§4.3）。
- `VerificationLedger` / `grade`：验证账本与信任分级——让信任能长回来（§4.4）。
- `redact` / `RedactionStore`：入站脱敏与加密映射表——原文的"全"不等于"裸"（§10.1）。
- `Tier` / `tier_of` / `tier_rank`：热 / 温 / 冷分层与装配优先级——渐进替换（§6.5）。
- `FeedMetrics`：投喂度量（压缩率 / 必留项命中率 / 重建一致率，§5.6）。
- `BackfillObserver`：回补率观测——压过头时应回退装配等级的信号（§6.7）。
"""

from flux.core.context.catalog import catalog_line, render_catalog
from flux.core.context.handoff import HandoffBrief, build_handoff
from flux.core.context.index import extract_tokens, normalize_token
from flux.core.context.lifecycle import ContextLifecycle
from flux.core.context.metrics import (
    BackfillObserver,
    BackfillPolicy,
    BackfillStats,
    FeedMetrics,
)
from flux.core.context.redact import RedactionMatch, RedactionResult, RedactionStore, redact
from flux.core.context.repository import ContextStepRepository
from flux.core.context.secretbox import SecretBox
from flux.core.context.spine import fact_spine
from flux.core.context.store import ContentStore
from flux.core.context.tiers import Tier, TierPolicy, tier_of, tier_rank
from flux.core.context.trust import TrustPolicy, TrustStats, VerificationLedger, grade

__all__ = [
    "BackfillObserver",
    "BackfillPolicy",
    "BackfillStats",
    "ContentStore",
    "ContextLifecycle",
    "ContextStepRepository",
    "FeedMetrics",
    "HandoffBrief",
    "RedactionMatch",
    "RedactionResult",
    "RedactionStore",
    "SecretBox",
    "Tier",
    "TierPolicy",
    "TrustPolicy",
    "TrustStats",
    "VerificationLedger",
    "build_handoff",
    "catalog_line",
    "extract_tokens",
    "fact_spine",
    "grade",
    "normalize_token",
    "redact",
    "render_catalog",
    "tier_of",
    "tier_rank",
]
