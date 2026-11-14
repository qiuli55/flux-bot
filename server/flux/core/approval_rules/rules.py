"""审批规则的类型、参数校验与中文默认描述。

这里是规则参数的**唯一校验入口**：REST 创建/修改、Bot 工具新增都调用
`normalize_rule_params`，保证落库的参数一定是 evaluator 能安全解释的形状。

安全边界：

- `command_pattern` 的正则有长度上限、量词上限，并拒绝反向引用与嵌套量词
  （典型灾难性回溯写法）；编译失败一律 400，不把坏规则写进库；
- `payment_amount_over` 不做汇率换算：规则币种与动作币种不同即视为不命中；
- 未知 type / 未知 custom key 不会被 evaluator 猜测匹配。
"""

from __future__ import annotations

import math
import re
from typing import Any

from flux.errors import BadRequestError

#: 支付金额超过阈值需要审批。
RULE_TYPE_PAYMENT_AMOUNT_OVER = "payment_amount_over"
#: 命令匹配 glob / regex 需要审批。
RULE_TYPE_COMMAND_PATTERN = "command_pattern"
#: 任务类型匹配需要审批（按 task_kind）。
RULE_TYPE_TASK_MATCH = "task_match"
#: 工具名匹配需要审批（按 tool_name）。
RULE_TYPE_TOOL_MATCH = "tool_match"
#: 出站消息需要审批（可按频道过滤）。
RULE_TYPE_OUTBOUND_MESSAGE = "outbound_message"
#: 预留的自定义键值规则。
RULE_TYPE_CUSTOM = "custom"

RULE_TYPES: tuple[str, ...] = (
    RULE_TYPE_PAYMENT_AMOUNT_OVER,
    RULE_TYPE_COMMAND_PATTERN,
    RULE_TYPE_TASK_MATCH,
    RULE_TYPE_TOOL_MATCH,
    RULE_TYPE_OUTBOUND_MESSAGE,
    RULE_TYPE_CUSTOM,
)

#: 支付规则省略币种时的默认值（规则与动作两侧都按它归一化）。
DEFAULT_CURRENCY = "CNY"
#: 出站规则可过滤的频道；省略 / all 表示不限频道。
OUTBOUND_CHANNELS: tuple[str, ...] = ("feishu", "wecom", "dingtalk")
CHANNEL_LABELS = {"feishu": "飞书", "wecom": "企业微信", "dingtalk": "钉钉"}

#: 命令模式 / 工具名 / 任务类型的长度上限。
MAX_PATTERN_CHARS = 200
#: 正则里允许的最大量词数量（超过视为过度复杂，直接拒绝）。
MAX_REGEX_QUANTIFIERS = 30
#: 自定义规则键名长度上限。
MAX_CUSTOM_KEY_CHARS = 64

_QUANTIFIER_RE = re.compile(r"(?<!\\)(?:[*+?]|\{\d+(?:,\d*)?\})")
_BACKREFERENCE_RE = re.compile(r"\\[1-9]")
#: 形如 `(a+)+` / `(a*){2,}` / `(.*)*` 的嵌套量词：经典灾难性回溯写法。
_NESTED_QUANTIFIER_RE = re.compile(
    r"\((?:\?:)?[^()]*?(?:[*+]|\{\d+(?:,\d*)?\})[^()]*?\)\s*(?:[*+]|\{\d+(?:,\d*)?\})"
)
_CURRENCY_RE = re.compile(r"^[A-Z]{3,8}$")


def _bad(message: str, *, field: str = "params") -> BadRequestError:
    return BadRequestError(message, details={"field": field})


def is_rule_type(value: object) -> bool:
    return isinstance(value, str) and value in RULE_TYPES


def normalize_currency(value: object, *, field: str = "params.currency") -> str:
    """币种统一成大写；未提供时用 CNY。不做任何汇率换算。"""
    if value is None:
        return DEFAULT_CURRENCY
    text = str(value).strip().upper()
    if not text:
        return DEFAULT_CURRENCY
    if not _CURRENCY_RE.match(text):
        raise _bad("currency 必须是 3~8 位字母的币种代码（如 CNY、USD）", field=field)
    return text


def _normalize_pattern(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _bad(f"{field} 必须是非空字符串", field=field)
    text = value.strip()
    if len(text) > MAX_PATTERN_CHARS:
        raise _bad(
            f"{field} 最多 {MAX_PATTERN_CHARS} 个字符",
            field=field,
        )
    return text


def _validate_regex(pattern: str) -> None:
    """拒绝过长 / 过度复杂 / 会编译失败的正则；全部按 400 处理。"""
    if len(pattern) > MAX_PATTERN_CHARS:
        raise _bad(f"正则模式最多 {MAX_PATTERN_CHARS} 个字符", field="params.pattern")
    if _BACKREFERENCE_RE.search(pattern):
        raise _bad("正则模式不支持反向引用（\\1 等）", field="params.pattern")
    if len(_QUANTIFIER_RE.findall(pattern)) > MAX_REGEX_QUANTIFIERS:
        raise _bad(
            f"正则模式的量词不能超过 {MAX_REGEX_QUANTIFIERS} 个，请拆成更简单的规则",
            field="params.pattern",
        )
    if _NESTED_QUANTIFIER_RE.search(pattern):
        raise _bad(
            "正则模式包含嵌套量词，可能造成灾难性回溯，请改用更简单的写法",
            field="params.pattern",
        )
    try:
        re.compile(pattern)
    except re.error as exc:
        raise _bad(f"正则模式不合法：{exc}", field="params.pattern") from exc


def _normalize_amount(value: object) -> float:
    if isinstance(value, bool):
        raise _bad("amount 必须是数字", field="params.amount")
    if isinstance(value, (int, float)):
        amount = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            amount = float(value.strip())
        except ValueError as exc:
            raise _bad("amount 必须是数字", field="params.amount") from exc
    else:
        raise _bad("amount 必须是数字", field="params.amount")
    if not math.isfinite(amount) or amount < 0:
        raise _bad("amount 必须是有限的非负数", field="params.amount")
    return amount


def normalize_rule_params(rule_type: str, params: Any) -> dict[str, Any]:
    """校验并归一化规则参数；非法一律抛 400（BadRequestError）。"""
    if not is_rule_type(rule_type):
        raise _bad(
            f"未知的审批规则类型：{rule_type}；可选值：{'、'.join(RULE_TYPES)}",
            field="type",
        )
    if not isinstance(params, dict):
        raise _bad("params 必须是对象", field="params")

    if rule_type == RULE_TYPE_PAYMENT_AMOUNT_OVER:
        if "amount" not in params:
            raise _bad("支付规则缺少 amount", field="params.amount")
        return {
            "amount": _normalize_amount(params.get("amount")),
            "currency": normalize_currency(params.get("currency")),
        }

    if rule_type == RULE_TYPE_COMMAND_PATTERN:
        pattern = _normalize_pattern(params.get("pattern"), field="params.pattern")
        style = str(params.get("style") or "glob").strip().lower()
        if style not in ("glob", "regex"):
            raise _bad("style 只能是 glob 或 regex", field="params.style")
        if style == "regex":
            _validate_regex(pattern)
        return {"pattern": pattern, "style": style}

    if rule_type in (RULE_TYPE_TASK_MATCH, RULE_TYPE_TOOL_MATCH):
        normalized: dict[str, str] = {}
        for key in ("tool_name", "task_kind"):
            raw = params.get(key)
            if raw is None:
                continue
            normalized[key] = _normalize_pattern(raw, field=f"params.{key}")
        if not normalized:
            raise _bad("task_match / tool_match 至少需要 tool_name 或 task_kind 之一")
        return normalized

    if rule_type == RULE_TYPE_OUTBOUND_MESSAGE:
        raw_channel = params.get("channel")
        if raw_channel is None:
            return {"channel": None}
        channel = str(raw_channel).strip().lower()
        if channel in ("", "all", "*"):
            return {"channel": None}
        if channel not in OUTBOUND_CHANNELS:
            raise _bad(
                f"channel 只能是 {'、'.join(OUTBOUND_CHANNELS)} 或 all",
                field="params.channel",
            )
        return {"channel": channel}

    # custom：精确约定 {key: str, value: <JSON 标量>}；evaluator 只按 key/value
    # 精确相等匹配，未知 key 一律不命中（fail-closed）。
    key = params.get("key")
    if not isinstance(key, str) or not key.strip():
        raise _bad("custom 规则需要非空的 key", field="params.key")
    key = key.strip()
    if len(key) > MAX_CUSTOM_KEY_CHARS:
        raise _bad(f"custom 规则的 key 最多 {MAX_CUSTOM_KEY_CHARS} 个字符", field="params.key")
    if "value" not in params:
        raise _bad("custom 规则需要 value", field="params.value")
    return {"key": key, "value": params.get("value")}


def _amount_text(amount: float) -> str:
    return str(int(amount)) if float(amount).is_integer() else str(amount)


def default_description(rule_type: str, params: dict[str, Any]) -> str:
    """按已归一化的参数生成一句中文说明（App 设置页直接展示）。"""
    if rule_type == RULE_TYPE_PAYMENT_AMOUNT_OVER:
        return (
            f"支付金额超过 {_amount_text(params.get('amount', 0))} "
            f"{params.get('currency', DEFAULT_CURRENCY)} 时需要我确认"
        )
    if rule_type == RULE_TYPE_COMMAND_PATTERN:
        style = "正则" if params.get("style") == "regex" else "通配符"
        return f"执行匹配{style} {params.get('pattern', '')} 的命令时需要我确认"
    if rule_type == RULE_TYPE_TOOL_MATCH:
        tool = params.get("tool_name")
        if tool:
            return f"调用工具 {tool} 时需要我确认"
        return f"执行任务类型 {params.get('task_kind', '')} 时需要我确认"
    if rule_type == RULE_TYPE_TASK_MATCH:
        kind = params.get("task_kind")
        if kind:
            return f"执行任务类型 {kind} 时需要我确认"
        return f"调用工具 {params.get('tool_name', '')} 时需要我确认"
    if rule_type == RULE_TYPE_OUTBOUND_MESSAGE:
        channel = params.get("channel")
        if channel:
            label = CHANNEL_LABELS.get(channel, channel)
            return f"通过{label}发送出站消息时需要我确认"
        return "发送任何出站消息时需要我确认"
    return f"命中自定义规则 {params.get('key', '')} 时需要我确认"


__all__ = [
    "CHANNEL_LABELS",
    "DEFAULT_CURRENCY",
    "MAX_PATTERN_CHARS",
    "MAX_REGEX_QUANTIFIERS",
    "OUTBOUND_CHANNELS",
    "RULE_TYPES",
    "RULE_TYPE_COMMAND_PATTERN",
    "RULE_TYPE_CUSTOM",
    "RULE_TYPE_OUTBOUND_MESSAGE",
    "RULE_TYPE_PAYMENT_AMOUNT_OVER",
    "RULE_TYPE_TASK_MATCH",
    "RULE_TYPE_TOOL_MATCH",
    "default_description",
    "is_rule_type",
    "normalize_currency",
    "normalize_rule_params",
]
