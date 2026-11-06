"""成本计算（主规格 §15.4）。

只做算术，不内置任何价格表——单价必须由配置显式提供，避免写入未核实的金额。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 避免与 model_gateway.base 形成循环导入
    from flux.core.model_gateway.base import ModelPricing


def compute_cost(input_tokens: int, output_tokens: int, pricing: ModelPricing) -> float:
    """按每 1000 token 单价计算费用。"""
    total = (input_tokens / 1000) * pricing.input_per_1k
    total += (output_tokens / 1000) * pricing.output_per_1k
    return round(total, 6)
