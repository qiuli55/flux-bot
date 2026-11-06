"""IDE 行内补全（AI_COMPLETION_SPEC）。

五个部件各管一段，API 层只按需组合：

- `context.CompletionContextBuilder`：L0 当前文件 / L1 邻接签名 / L2 项目摘要；
- `prefs.CompletionPrefsStore`：开关、模型偏好与「当前 Agent」的单文件落盘；
- `resolver.CompletionModelResolver`：把 model / key_id / agent_id 解析成可调用通道；
- `fim.CompletionService`：原生 FIM → chat 降级的流式生成门面；
- `stats.CompletionStatsStore`：接受率 / 首字延迟等本地埋点。
"""

from flux.core.ide_completion.context import CompletionContextBuilder
from flux.core.ide_completion.fim import CompletionService
from flux.core.ide_completion.prefs import (
    FOLLOW_AGENT,
    ActiveAgentSelection,
    CompletionPrefs,
    CompletionPrefsStore,
)
from flux.core.ide_completion.resolver import CompletionModelResolver, CompletionTarget
from flux.core.ide_completion.stats import CompletionStatsStore

__all__ = [
    "FOLLOW_AGENT",
    "ActiveAgentSelection",
    "CompletionContextBuilder",
    "CompletionModelResolver",
    "CompletionPrefs",
    "CompletionPrefsStore",
    "CompletionService",
    "CompletionStatsStore",
    "CompletionTarget",
]
