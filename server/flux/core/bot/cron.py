"""5 段 cron 解析（分 时 日 月 周），无第三方依赖。

Bot 的定时是「常驻进程按 next_run_at 取到期任务」，只需要两件事：
校验表达式合法、算出下一次触发时间。语义按 Vixie cron：
`*`、`a`、`a-b`、`*/n`、`a-b/n`、逗号列表；周几 0/7 都是周日；
「日」与「周」都受限时取并集（任一命中即触发）。

时区：cron 字段按配置的 IANA 时区（默认 UTC）的「本地墙钟」解释，结果统一
返回 aware UTC datetime。DST 切换按墙钟语义处理：春季不存在的本地时间跳过，
秋季重复的本地时间只按 fold=0 触发一次。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["CronSchedule", "parse_cron"]

#: (字段名, 最小值, 最大值)
_FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 7),
)

#: 最多向后找 8 年：覆盖 2 月 29 日这类闰年表达式
_MAX_DAYS = 366 * 8

_UTC = timezone.utc


def _coerce_tz(value: str | tzinfo | None) -> tzinfo:
    """把时区名（或 tzinfo）规范成可用于 datetime 的 tzinfo。"""
    if value is None:
        return _UTC
    if isinstance(value, str):
        name = value.strip() or "UTC"
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"未知时区：{name!r}") from exc
    return value


def _parse_field(spec: str, *, name: str, lo: int, hi: int) -> tuple[set[int], bool]:
    """把一个字段展开成取值集合，并返回它是否为通配（用于日/周的并集语义）。"""
    spec = spec.strip()
    if not spec:
        raise ValueError(f"cron 的 {name} 字段为空")
    values: set[int] = set()
    wildcard = False
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"cron 的 {name} 字段含空项：{spec}")
        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            try:
                step = int(step_text)
            except ValueError as exc:
                raise ValueError(f"cron 的 {name} 字段步长不是整数：{step_text}") from exc
            if step <= 0:
                raise ValueError(f"cron 的 {name} 字段步长必须为正数：{step_text}")
        if part == "*":
            start, end = lo, hi
            wildcard = wildcard or step == 1
        elif "-" in part:
            start_text, _, end_text = part.partition("-")
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise ValueError(f"cron 的 {name} 字段区间非法：{part}") from exc
        else:
            try:
                start = end = int(part)
            except ValueError as exc:
                raise ValueError(f"cron 的 {name} 字段含非法值：{part}") from exc
        if start < lo or end > hi or start > end:
            raise ValueError(f"cron 的 {name} 字段超出 {lo}-{hi}：{part}")
        values.update(range(start, end + 1, step))
    return values, wildcard


@dataclass(frozen=True)
class CronSchedule:
    """一条已校验的 cron 表达式（字段按 `tz` 的本地墙钟解释）。"""

    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_wildcard: bool
    weekday_wildcard: bool
    tz: tzinfo = field(default_factory=lambda: _UTC)

    def _day_matches(self, day: date) -> bool:
        day_ok = day.day in self.days
        # Python: 周一=0；cron: 周日=0 → 周一=1
        weekday_ok = ((day.weekday() + 1) % 7) in self.weekdays
        if not self.day_wildcard and not self.weekday_wildcard:
            # 两者都受限时按 Vixie cron 取并集
            return day_ok or weekday_ok
        return day_ok and weekday_ok

    def next_after(self, base: datetime, tz: str | tzinfo | None = None) -> datetime:
        """返回严格晚于 `base` 的下一次触发时刻（UTC、秒与微秒归零）。

        默认使用解析时绑定的时区；显式传 `tz` 可临时覆盖。计算在本地墙钟上
        逐日/逐时/逐分推进：不存在的本地时间（春季前跳）跳过，重复的本地时间
        （秋季回拨）只取 fold=0 的第一次，避免同一墙钟触发两次。
        """
        zone = _coerce_tz(tz) if tz is not None else self.tz
        if base.tzinfo is None:
            base = base.replace(tzinfo=_UTC)
        base_utc = base.astimezone(_UTC)
        local_base = base_utc.astimezone(zone)
        start_day = local_base.date()
        hours = sorted(self.hours)
        minutes = sorted(self.minutes)
        for offset in range(_MAX_DAYS + 1):
            day = start_day + timedelta(days=offset)
            if day.month not in self.months or not self._day_matches(day):
                continue
            for hour in hours:
                for minute in minutes:
                    local = datetime(
                        day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=0
                    )
                    # 春季前跳的本地时间不存在：往返转换后墙钟会变，直接跳过。
                    roundtrip = local.astimezone(_UTC).astimezone(zone)
                    if roundtrip.replace(tzinfo=None) != local.replace(tzinfo=None):
                        continue
                    moment = local.astimezone(_UTC)
                    if moment > base_utc:
                        return moment
        raise ValueError(f"cron 表达式在 8 年内没有触发时间：{self.expression}")


def parse_cron(expression: str, tz: str | tzinfo | None = None) -> CronSchedule:
    """解析并校验 5 段 cron；非法时抛 ValueError（调用方转 400）。

    `tz` 是 cron 字段解释所用的 IANA 时区（默认 UTC），会随 schedule 一起保存；
    之后调用 `next_after()` 无需再传。
    """
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError(f"cron 必须是 5 段（分 时 日 月 周），收到 {len(parts)} 段：{expression}")
    parsed = [
        _parse_field(part, name=name, lo=lo, hi=hi)
        for part, (name, lo, hi) in zip(parts, _FIELDS, strict=True)
    ]
    minutes, _ = parsed[0]
    hours, _ = parsed[1]
    days, day_wildcard = parsed[2]
    months, _ = parsed[3]
    weekdays, weekday_wildcard = parsed[4]
    # 7 与 0 同为周日
    weekdays = {0 if value == 7 else value for value in weekdays}
    return CronSchedule(
        expression=" ".join(parts),
        minutes=frozenset(minutes),
        hours=frozenset(hours),
        days=frozenset(days),
        months=frozenset(months),
        weekdays=frozenset(weekdays),
        day_wildcard=day_wildcard,
        weekday_wildcard=weekday_wildcard,
        tz=_coerce_tz(tz),
    )
