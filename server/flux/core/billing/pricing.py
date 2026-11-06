"""官方 API 价目表（USD / 1M tokens）与费用估算。

口径（2026-10-26 核对官方定价页）：
- 单价一律按 **每百万 token**，币种随条目（OpenAI / Anthropic / Google / DeepSeek 为 USD，
  火山方舟为 CNY；统一换算成 USD 落账，换算率可配）。
- DeepSeek 有错峰价（官方：UTC 周一至周五 01:00-04:00、06:00-10:00 为峰时，
  其余时间为峰时价的一半）；`estimate_cost` 按 `occurred_at` 自动选峰谷。
- 缓存读/写缺价的条目回退到输入价（宁可高估，不凭空打折）。

这张表只是**估算**：runtime 自己上报了 cost（opencode 的 step-finish 会带）时以它为准。
价格会变，官方页面才是最终依据；`FLUX_BILLING_PRICES_FILE` 可覆盖/追加条目。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

#: 价目表基准日期（内置条目最近一次核对官方页面的日期）
PRICES_AS_OF = "2026-10-26"

#: DeepSeek 峰时（UTC）：小时区间左闭右开，仅周一至周五
_DEEPSEEK_PEAK_HOURS: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))


@dataclass(frozen=True, slots=True)
class PriceEntry:
    """一个模型（或通配模式）的官方单价，单位：每 100 万 token。"""

    model: str
    vendor: str
    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None
    currency: str = "USD"
    #: 非峰时价格系数（DeepSeek = 0.5）；None = 不分峰谷
    off_peak_multiplier: float | None = None
    source: str = ""
    note: str = ""

    def cache_read_price(self) -> float:
        return self.input if self.cache_read is None else self.cache_read

    def cache_write_price(self) -> float:
        return self.input if self.cache_write is None else self.cache_write

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "vendor": self.vendor,
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read_price(),
            "cache_write": self.cache_write_price(),
            "currency": self.currency,
            "off_peak_multiplier": self.off_peak_multiplier,
            "source": self.source,
            "note": self.note,
        }


_OPENAI_SRC = "https://openai.com/api/pricing/"
_ANTHROPIC_SRC = "https://platform.claude.com/docs/en/about-claude/pricing"
_GOOGLE_SRC = "https://ai.google.dev/gemini-api/docs/pricing"
_DEEPSEEK_SRC = "https://api-docs.deepseek.com/quick_start/pricing"
_VOLC_SRC = "https://www.volcengine.com/product/ark"


def _builtin() -> list[PriceEntry]:
    return [
        # --- OpenAI（标准处理，270K 上下文以内）---
        PriceEntry("gpt-5.5", "OpenAI", 5.0, 30.0, 0.5, 5.0, source=_OPENAI_SRC),
        PriceEntry("gpt-5.4", "OpenAI", 2.5, 15.0, 0.25, 2.5, source=_OPENAI_SRC),
        PriceEntry("gpt-5.4-mini", "OpenAI", 0.75, 4.5, 0.075, 0.75, source=_OPENAI_SRC),
        # --- Anthropic（5 分钟缓存写入价；1 小时写入更贵，未单列）---
        PriceEntry("claude-fable-5.1", "Anthropic", 10.0, 50.0, 0.25, 12.5, source=_ANTHROPIC_SRC),
        PriceEntry("claude-mythos-5.1", "Anthropic", 10.0, 50.0, 0.25, 12.5, source=_ANTHROPIC_SRC),
        PriceEntry("claude-opus-5.5", "Anthropic", 4.0, 20.0, 0.2, 5.0, source=_ANTHROPIC_SRC),
        PriceEntry("claude-sonnet-5.5", "Anthropic", 2.0, 10.0, 0.1, 2.5, source=_ANTHROPIC_SRC),
        PriceEntry("claude-haiku-5.5", "Anthropic", 0.1, 0.5, 0.01, 0.125, source=_ANTHROPIC_SRC),
        # --- Google Gemini（2026-11-30 前的标准价，之后翻倍；到期请更新）---
        PriceEntry("gemini-4-argon", "Google", 2.0, 10.0, 0.1, 2.0, source=_GOOGLE_SRC),
        PriceEntry("gemini-3.8-flash", "Google", 0.75, 3.75, 0.075, 0.75, source=_GOOGLE_SRC),
        # --- DeepSeek（峰时 USD；错峰 ×0.5）---
        PriceEntry(
            "deepseek-flash",
            "DeepSeek",
            0.3,
            1.2,
            0.006,
            0.3,
            off_peak_multiplier=0.5,
            source=_DEEPSEEK_SRC,
            note="峰时价；错峰 ×0.5（UTC 周一至周五 01-04、06-10 为峰时）",
        ),
        PriceEntry(
            "deepseek-v4-pro",
            "DeepSeek",
            1.32,
            3.96,
            0.044,
            1.32,
            off_peak_multiplier=0.5,
            source=_DEEPSEEK_SRC,
            note="峰时价；错峰 ×0.5",
        ),
        # --- 火山方舟（人民币/百万 token，落账时换算成 USD）---
        PriceEntry(
            "doubao-seed-evolving",
            "Volcengine",
            6.0,
            30.0,
            1.2,
            6.0,
            currency="CNY",
            source=_VOLC_SRC,
        ),
        PriceEntry(
            "doubao-seed",
            "Volcengine",
            6.0,
            30.0,
            1.2,
            6.0,
            currency="CNY",
            source=_VOLC_SRC,
            note="按 Doubao-Seed-Evolving 现行价目估算",
        ),
        # --- OpenCode Zen 免费模型 ---
        PriceEntry(
            "deepseek-v4.1-flash-free",
            "OpenCode",
            0.0,
            0.0,
            0.0,
            0.0,
            source="https://opencode.ai/docs/zen/",
            note="Zen 免费模型",
        ),
    ]


#: 旧模型 id → 现行价目条目
_ALIASES: dict[str, str] = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
    "deepseek-chat": "deepseek-flash",
    "deepseek-reasoner": "deepseek-v4-pro",
    "gpt-5.5-codex": "gpt-5.5",
    "claude-sonnet-5": "claude-sonnet-5.5",
    "claude-opus-5": "claude-opus-5.5",
}


def _family(model: str) -> str:
    """去掉 provider 前缀后的模型名（`opencode-go/gemini-4-argon` → `gemini-4-argon`）。"""
    value = (model or "").strip().lower()
    return value.split("/", 1)[1] if "/" in value else value


@dataclass(slots=True)
class PriceBook:
    """一份价目表：内置 + 自定义覆盖，按模型名解析。"""

    entries: list[PriceEntry] = field(default_factory=list)
    as_of: str = PRICES_AS_OF
    cny_per_usd: float = 7.2

    def find(self, model: str) -> PriceEntry | None:
        """精确 → 别名 → 通配，优先匹配裸模型名。"""
        full = (model or "").strip().lower()
        bare = _family(full)
        candidates = [bare, full]
        for name in candidates:
            alias = _ALIASES.get(name)
            if alias:
                candidates.append(alias)
        for name in candidates:
            for entry in self.entries:
                if entry.model.lower() == name:
                    return entry
        for name in candidates:
            best: PriceEntry | None = None
            for entry in self.entries:
                if (
                    any(ch in entry.model for ch in "*?[")
                    and fnmatch(name, entry.model.lower())
                    and (best is None or len(entry.model) > len(best.model))
                ):
                    best = entry
            if best is not None:
                return best
        # 带日期后缀的模型（`gpt-5.5-2026-01-01`）按最长前缀归到系列价目
        for name in candidates:
            best = None
            for entry in self.entries:
                if any(ch in entry.model for ch in "*?["):
                    continue
                if name.startswith(f"{entry.model.lower()}-") and (
                    best is None or len(entry.model) > len(best.model)
                ):
                    best = entry
            if best is not None:
                return best
        return None

    def to_usd(self, amount: float, currency: str) -> float:
        if currency.upper() == "CNY":
            rate = self.cny_per_usd if self.cny_per_usd > 0 else 7.2
            return amount / rate
        return amount

    def estimate_cost(
        self,
        model: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        occurred_at: datetime | None = None,
    ) -> float | None:
        """按官方单价估算一次调用的美元成本；没有价目条目返回 None。"""
        entry = self.find(model)
        if entry is None:
            return None
        multiplier = 1.0
        if entry.off_peak_multiplier is not None and not _is_peak(occurred_at):
            multiplier = entry.off_peak_multiplier
        raw = (
            input_tokens * entry.input
            + output_tokens * entry.output
            + cache_read_tokens * entry.cache_read_price()
            + cache_write_tokens * entry.cache_write_price()
        ) / 1_000_000.0
        return round(self.to_usd(raw, entry.currency) * multiplier, 6)

    def view(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "cny_per_usd": self.cny_per_usd,
            "prices": [entry.to_dict() for entry in self.entries],
        }


def _is_peak(occurred_at: datetime | None) -> bool:
    """DeepSeek 峰时判断（UTC 周一至周五的两个时段；中国节假日忽略）。"""
    moment = (occurred_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if moment.weekday() >= 5:
        return False
    return any(start <= moment.hour < end for start, end in _DEEPSEEK_PEAK_HOURS)


def _entry_from_dict(raw: dict[str, Any]) -> PriceEntry:
    model = str(raw.get("model") or "").strip()
    if not model:
        raise ValueError("价目条目缺少 model")
    currency = str(raw.get("currency") or "USD").strip().upper()
    if currency not in ("USD", "CNY"):
        raise ValueError(f"不支持的币种：{currency}")
    off_peak = raw.get("off_peak_multiplier")
    return PriceEntry(
        model=model,
        vendor=str(raw.get("vendor") or "").strip(),
        input=float(raw.get("input") or 0.0),
        output=float(raw.get("output") or 0.0),
        cache_read=_optional_float(raw.get("cache_read")),
        cache_write=_optional_float(raw.get("cache_write")),
        currency=currency,
        off_peak_multiplier=(
            float(off_peak)
            if isinstance(off_peak, (int, float)) and not isinstance(off_peak, bool)
            else None
        ),
        source=str(raw.get("source") or "").strip(),
        note=str(raw.get("note") or "").strip(),
    )


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def load_price_book(
    path: str | None,
    *,
    cny_per_usd: float = 7.2,
) -> PriceBook:
    """内置价目 + 自定义文件（同 model 覆盖；文件不存在 = 只用内置）。"""
    entries = _builtin()
    as_of = PRICES_AS_OF
    raw_path = (path or "").strip()
    if raw_path:
        file_path = Path(raw_path).expanduser()
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = None
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, dict):
            as_of = str(payload.get("as_of") or as_of)
            items = payload.get("prices")
            if isinstance(items, list):
                overrides: dict[str, PriceEntry] = {}
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    try:
                        entry = _entry_from_dict(item)
                    except (TypeError, ValueError):
                        continue
                    overrides[entry.model.lower()] = entry
                # 同 model 的自定义条目顶掉内置，其余保持
                entries = [overrides.pop(entry.model.lower(), entry) for entry in entries] + list(
                    overrides.values()
                )
    return PriceBook(entries=entries, as_of=as_of, cny_per_usd=cny_per_usd)


__all__ = [
    "PRICES_AS_OF",
    "PriceBook",
    "PriceEntry",
    "load_price_book",
]
