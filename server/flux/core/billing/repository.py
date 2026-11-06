"""计费账本与官方额度快照的持久化。

写入只追加（usage_records 是明细账，不做更新）；读取以「套餐 × 窗口起点」聚合，
不建物化表——窗口只是时间切片，随时可以从明细重算。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.models.task import Task
from flux.models.usage import UsageRecord, UsageSnapshot


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


class BillingRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # --- 明细账 ---

    async def add_record(
        self,
        *,
        provider: str,
        model: str,
        plan_key: str | None,
        account_key: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        cost: float | None = None,
        run_id: str | None = None,
        task_id: str | uuid.UUID | None = None,
        agent_id: str | uuid.UUID | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        total = int(input_tokens) + int(output_tokens)
        row = UsageRecord(
            provider=provider[:32],
            model=model[:128],
            plan_key=plan_key,
            account_key=(account_key or "")[:16],
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            cache_read_tokens=int(cache_read_tokens),
            cache_write_tokens=int(cache_write_tokens),
            total_tokens=total,
            cost=float(cost) if cost is not None else None,
            run_id=run_id,
            task_id=_parse_uuid(task_id),
            agent_id=_parse_uuid(agent_id),
            occurred_at=occurred_at or _utcnow(),
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()

    async def totals(
        self,
        *,
        plan_key: str | None,
        since: datetime,
        account_key: str | None = None,
    ) -> dict[str, float]:
        """某套餐（可选某账号）自 `since` 起的本地用量合计。

        `account_key=None` 表示不按账号过滤（整套餐合计）。
        """
        statement = select(
            func.count(UsageRecord.id),
            func.coalesce(func.sum(UsageRecord.input_tokens), 0),
            func.coalesce(func.sum(UsageRecord.output_tokens), 0),
            func.coalesce(func.sum(UsageRecord.cache_read_tokens), 0),
            func.coalesce(func.sum(UsageRecord.cache_write_tokens), 0),
            func.coalesce(func.sum(UsageRecord.total_tokens), 0),
            func.coalesce(func.sum(UsageRecord.cost), 0.0),
        ).where(UsageRecord.occurred_at >= since)
        if plan_key:
            statement = statement.where(UsageRecord.plan_key == plan_key)
        if account_key is not None:
            statement = statement.where(UsageRecord.account_key == account_key)
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one()
        return {
            "records": int(row[0] or 0),
            "input_tokens": int(row[1] or 0),
            "output_tokens": int(row[2] or 0),
            "cache_read_tokens": int(row[3] or 0),
            "cache_write_tokens": int(row[4] or 0),
            "total_tokens": int(row[5] or 0),
            "cost": round(float(row[6] or 0.0), 6),
        }

    async def tokens_by_bot_since(self, since: datetime) -> dict[uuid.UUID, int]:
        """按 Bot 聚合 `since` 之后的 token 用量。

        `usage_records` 没有 `bot_id`，只能经 `tasks.bot_id` 归因（Bot 每次开工都落一条任务）。
        直接读明细账而不是累加计数，重算幂等，也不会因为漏记/重记而漂移。
        """
        statement = (
            select(Task.bot_id, func.coalesce(func.sum(UsageRecord.total_tokens), 0))
            .join(UsageRecord, UsageRecord.task_id == Task.id)
            .where(Task.bot_id.is_not(None), UsageRecord.occurred_at >= since)
            .group_by(Task.bot_id)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        return {row[0]: int(row[1] or 0) for row in rows if row[0] is not None}

    async def record_times(
        self,
        *,
        plan_key: str | None,
        since: datetime,
        account_key: str | None = None,
    ) -> list[datetime]:
        """某套餐自 `since` 起的调用时刻（只取时间，用于判断区间内 Flux 是否在跑）。"""
        statement = select(UsageRecord.occurred_at).where(UsageRecord.occurred_at >= since)
        if plan_key:
            statement = statement.where(UsageRecord.plan_key == plan_key)
        if account_key is not None:
            statement = statement.where(UsageRecord.account_key == account_key)
        statement = statement.order_by(UsageRecord.occurred_at)
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def record_accounts(self, *, plan_key: str, since: datetime) -> list[str]:
        """某套餐自 `since` 起出现过的账号指纹（账本口径）。"""
        statement = (
            select(UsageRecord.account_key)
            .where(
                UsageRecord.plan_key == plan_key,
                UsageRecord.occurred_at >= since,
                UsageRecord.account_key != "",
            )
            .distinct()
        )
        async with self._session_factory() as session:
            return [str(value) for value in await session.scalars(statement)]

    async def records_by_run(self, run_id: str, *, limit: int = 200) -> list[UsageRecord]:
        statement = (
            select(UsageRecord)
            .where(UsageRecord.run_id == run_id)
            .order_by(UsageRecord.occurred_at)
            .limit(max(limit, 1))
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def records_after(self, since: datetime | None, *, limit: int = 200) -> list[UsageRecord]:
        """增量同步用：按 `occurred_at` 升序取 `since` 之后（含）的已归因明细。

        只同步 `plan_key` 非空的记录——未归因的调用没有套餐意义，云端也不需要。
        调用方按批推进 `since`（取本批最后一条的时刻），重复窗口交给云端幂等兜底。
        """
        statement = select(UsageRecord).where(UsageRecord.plan_key.is_not(None))
        if since is not None:
            statement = statement.where(UsageRecord.occurred_at >= since)
        statement = statement.order_by(UsageRecord.occurred_at, UsageRecord.id).limit(max(limit, 1))
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    # --- 官方快照 ---

    async def add_snapshot(
        self,
        *,
        plan_key: str,
        account_key: str = "",
        window_key: str,
        used: float | None = None,
        quota: float | None = None,
        percent: float | None = None,
        unit: str = "percent",
        status: str = "ok",
        resets_at: datetime | None = None,
        source: str = "",
        error: str | None = None,
        fetched_at: datetime | None = None,
    ) -> None:
        row = UsageSnapshot(
            plan_key=plan_key,
            account_key=(account_key or "")[:16],
            window_key=window_key,
            used=used,
            quota=quota,
            percent=percent,
            unit=unit,
            status=status,
            resets_at=resets_at,
            source=source,
            error=error,
            fetched_at=fetched_at or _utcnow(),
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()

    async def snapshots_since(
        self,
        *,
        plan_key: str,
        window_key: str,
        since: datetime,
        account_key: str | None = None,
    ) -> list[dict]:
        """某窗口自 `since` 起的成功快照（按采集时刻正序），用于外部消耗差分。"""
        statement = select(UsageSnapshot).where(
            UsageSnapshot.plan_key == plan_key,
            UsageSnapshot.window_key == window_key,
            UsageSnapshot.fetched_at >= since,
            UsageSnapshot.percent.is_not(None),
            UsageSnapshot.error.is_(None),
        )
        if account_key is not None:
            statement = statement.where(UsageSnapshot.account_key == account_key)
        statement = statement.order_by(UsageSnapshot.fetched_at)
        async with self._session_factory() as session:
            rows = list(await session.scalars(statement))
        return [
            {
                "account_key": row.account_key,
                "percent": float(row.percent or 0.0),
                "fetched_at": row.fetched_at,
                "resets_at": row.resets_at,
            }
            for row in rows
        ]

    async def latest_snapshots(self, plan_key: str, *, lookback_days: int = 2) -> list[dict]:
        """每个「账号 × 窗口」取最新一条（按 fetched_at 倒序去重）。"""
        since = _utcnow() - timedelta(days=max(lookback_days, 1))
        statement = (
            select(UsageSnapshot)
            .where(UsageSnapshot.plan_key == plan_key, UsageSnapshot.fetched_at >= since)
            .order_by(UsageSnapshot.fetched_at.desc())
        )
        async with self._session_factory() as session:
            rows = list(await session.scalars(statement))
        latest: dict[tuple[str, str], dict] = {}
        for row in rows:
            key = (row.account_key or "", row.window_key)
            if key in latest:
                continue
            latest[key] = row.to_dict()
        return list(latest.values())


__all__ = ["BillingRepository"]
