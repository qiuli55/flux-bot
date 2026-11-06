"""动态投喂预算（FLUX_CONTEXT_MECHANISM §6.1–6.3）。

§6.1 的公式：

    可投喂预算 = 目标模型上下文窗口 × 安全系数
               − 预留输出
               − 系统 / 工具定义占用
               − 必留项体积

这里只落**算术**：窗口与各项占用都是显式常量，换模型时改常量即可，
不再散落 `50` / `24_000` 之类的硬编码（§11 的缺口）。

§6.2 的三分区与 §6.3 的三等级是同一件事的两个视角，分别用 `Zone` / `Level` 表达：
**分区**是策略（能不能压），**等级**是形态（实际长什么样）。

| 分区 | 策略 |
| --- | --- |
| `P0` 必留区 | 先扣预算，**一条不减**（§5.4 永不压缩清单） |
| `P1` 可压区 | 装得下就原文，装不下才走 §5.3 流水线编码 |
| `P2` 可取区 | 只留 `ref`，按需 `fetch` |

| 等级 | 形态 |
| --- | --- |
| `L1` 原文 | 逐字原文 |
| `L2` 编码 | 符号表编码 / 结构抽取后的文本 |
| `L3` 仅 ref | `step_id` + 目录行 + 事实最小锚 |
"""

from __future__ import annotations

from dataclasses import dataclass

from flux.enums import StrEnum

# ---------------------------------------------------------------------------
# §6.1 预算常量
# ---------------------------------------------------------------------------

#: 目标模型上下文窗口（字符）。取一个当前主流模型的保守量，换模型时改这里。
DEFAULT_CONTEXT_WINDOW = 200_000
#: 安全系数：留出一半空间吸收 token/字符换算误差与突发内容
SAFETY_FACTOR = 0.5
#: 预留输出：不管输入多少，都得给模型的回复留位置
RESERVED_OUTPUT_CHARS = 40_000
#: 系统提示 + 工具定义等固定占用
SYSTEM_TOOL_OVERHEAD_CHARS = 36_000


def _ceiling(window: int, safety: float, reserved_output: int, system_tools: int) -> int:
    """§6.1 前三项之差：还没扣必留项之前的可投喂上限。"""
    return int(window * safety) - reserved_output - system_tools


#: 默认可投喂预算：`200_000 × 0.5 − 40_000 − 36_000 = 24_000`。
#: 保持与历史硬编码同值，使既有调用方的行为不变，但数值改由公式推导而来。
DEFAULT_BUDGET_CHARS = _ceiling(
    DEFAULT_CONTEXT_WINDOW, SAFETY_FACTOR, RESERVED_OUTPUT_CHARS, SYSTEM_TOOL_OVERHEAD_CHARS
)


@dataclass(frozen=True)
class FeedBudget:
    """一次投喂的预算账（§6.1）。`required` 是必留区体积，由调用方在装配时填入。"""

    window: int = DEFAULT_CONTEXT_WINDOW
    safety: float = SAFETY_FACTOR
    reserved_output: int = RESERVED_OUTPUT_CHARS
    system_tools: int = SYSTEM_TOOL_OVERHEAD_CHARS
    #: 必留区总体积（P0），先扣预算、一条不减
    required: int = 0

    @property
    def ceiling(self) -> int:
        """扣掉预留输出与系统占用后，可用于全部条目的上限。"""
        return _ceiling(self.window, self.safety, self.reserved_output, self.system_tools)

    @property
    def available(self) -> int:
        """必留项占完之后，分给可压区 / 可取区的余量。可能为 0，但不会为负。"""
        return max(0, self.ceiling - self.required)

    @property
    def total(self) -> int:
        """必留区 + 余量：本次投喂的字符总量。"""
        return self.required + self.available


# ---------------------------------------------------------------------------
# §6.2 / §6.3 分区与等级
# ---------------------------------------------------------------------------


class Zone(StrEnum):
    """三分区（§6.2）：决定一条 entry 能不能被裁剪。"""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class Level(StrEnum):
    """三等级（§6.3）：决定一条 entry 实际以什么形态进投喂。"""

    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


# ---------------------------------------------------------------------------
# 对话历史的预算折算（替代散落的固定值 50，§11）
# ---------------------------------------------------------------------------

#: 一条对话历史消息的估算字符数：把字符预算折算成"能带几条"
ESTIMATED_MESSAGE_CHARS = 512
#: 对话历史在可投喂预算里的占比；其余留给任务 / Brain / 提案等工程事实
HISTORY_BUDGET_SHARE = 0.4


def history_message_limit(
    *,
    budget: int = DEFAULT_BUDGET_CHARS,
    share: float = HISTORY_BUDGET_SHARE,
    message_chars: int = ESTIMATED_MESSAGE_CHARS,
) -> int:
    """把字符预算折算成"最多带几条历史消息"（§6.1 的落地，替换固定值 50）。

    这是个**有界**的估算：真实消息长度不可预知，与其拍一个与预算无关的固定条数，
    不如让条数随预算一起变——预算调大就多带，调小就少带。至少带 1 条，
    保证调用方永远能拿到"最近一条"这个最小上下文。
    """
    if message_chars <= 0:
        raise ValueError("message_chars 必须是正整数")
    ratio = min(max(share, 0.0), 1.0)
    return max(1, int(budget * ratio) // message_chars)
