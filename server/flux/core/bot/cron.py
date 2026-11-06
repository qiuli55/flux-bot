"""5 段 cron 解析（分 时 日 月 周），无第三方依赖。

Bot 的定时是「常驻进程按 next_run_at 取到期任务」，只需要两件事：
校验表达式合法、算出下一次触发时间。语义按 Vixie cron：
`*`、`a`、`a-b`、`*/n`、`a-b/n`、逗号列表；周几 0/7 都是周日；
「日」与「周」都受限时取并集（任一命中即触发）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

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
    """一条已校验的 cron 表达式。"""

    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_wildcard: bool
    weekday_wildcard: bool

    def _day_matches(self, day: date) -> bool:
        day_ok = day.day in self.days
        # Python: 周一=0；cron: 周日=0 → 周一=1
        weekday_ok = ((day.weekday() + 1) % 7) in self.weekdays
        if not self.day_wildcard and not self.weekday_wildcard:
            # 两者都受限时按 Vixie cron 取并集
            return day_ok or weekday_ok
        return day_ok and weekday_ok

    def next_after(self, base: datetime) -> datetime:
        """返回严格晚于 `base` 的下一次触发时刻（UTC、秒与微秒归零）。"""
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        cursor = base.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(
            minutes=1
        )
        hours = sorted(self.hours)
        minutes = sorted(self.minutes)
        for _ in range(_MAX_DAYS + 1):
            day = cursor.date()
            if day.month in self.months and self._day_matches(day):
                for hour in hours:
                    if hour < cursor.hour:
                        continue
                    for minute in minutes:
                        if hour == cursor.hour and minute < cursor.minute:
                            continue
                        return datetime(
                            day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc
                        )
            cursor = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1)
        raise ValueError(f"cron 表达式在 8 年内没有触发时间：{self.expression}")


def parse_cron(expression: str) -> CronSchedule:
    """解析并校验 5 段 cron；非法时抛 ValueError（调用方转 400）。"""
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
    )
