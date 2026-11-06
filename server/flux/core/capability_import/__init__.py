"""能力导入（批次③）：把本机现有 Agent / Skill / Connector 的外部形态

规范化成 Flux 标准对象，经轻量安全检查后导入注册表。

边界（D3 并存不合并）：
- 本包只做"发现 → 标准转换 → 安全检查 → 落注册表"，不执行任何能力；
- Agent 的安装事实仍以 `flux.core.agent_runtime.installation` 为真源，这里只读取；
- Connector 的运行时调用仍在 `flux.connectors`，这里只登记声明与权限需求。
"""

from flux.core.capability_import.standards import (
    FluxAgent,
    FluxConnector,
    FluxSkill,
    fingerprint,
    parse_spec,
)

__all__ = [
    "FluxAgent",
    "FluxConnector",
    "FluxSkill",
    "fingerprint",
    "parse_spec",
]
