"""套餐用量的跨设备归集。

每台设备各自记本地账本（`usage_records`），这里负责两件事：
1. **增量上传**：按游标把本机新增的已归因明细推到账号云（云端按 `record_uid` 幂等）；
2. **拉取合计**：把「套餐 × 账号 × 窗口」的云端汇总拉回来，供视图显示「全部设备」。

鉴权与用户记忆一致，用设备令牌（`/etc/flux-device-credentials`）而不是账号云登录态。
任何网络 / 鉴权失败都只记日志：归集是展示增强，不能影响本地计费与任务执行。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from flux.cloud.memory_sync import DeviceCredentials, load_device_credentials
from flux.core.billing.repository import BillingRepository
from flux.logging import get_logger
from flux.models.usage import UsageRecord

if TYPE_CHECKING:
    from flux.config import Settings

logger = get_logger(__name__)

#: 单次 HTTP 超时（秒）
_TIMEOUT = 20.0
#: 后台归集周期（秒）：账本是增量数据，5 分钟足够跨设备收敛
SYNC_INTERVAL_SECONDS = 300.0
#: 单批推送上限（与云端 `BillingUsageBatchIn.items` 的 max_length 对齐）
_PUSH_BATCH = 200
#: 单批汇总查询上限（与云端 `BillingSummaryBatchIn.queries` 的 max_length 对齐）
_SUMMARY_BATCH = 64
#: 游标向前重叠多少：容忍设备间时钟误差，重复部分由云端幂等去重
_OVERLAP = timedelta(minutes=10)


def _as_utc(value: datetime) -> datetime:
    """SQLite 读回会丢 tzinfo，统一按 UTC 归一。"""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class CloudBillingSync:
    """本地账本 → 账号云的单向增量上传 + 云端汇总拉取。"""

    def __init__(
        self,
        settings: Settings,
        repo: BillingRepository,
        *,
        credentials_path: Path | None = None,
        cursor_path: Path | None = None,
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._credentials_path = credentials_path or settings.device_credentials_path
        self._cursor_path = cursor_path or settings.billing_sync_path
        self._last_error: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self._settings.billing_cloud_sync)

    @property
    def last_error(self) -> str:
        return self._last_error

    def credentials(self) -> DeviceCredentials | None:
        if not self.enabled:
            return None
        return load_device_credentials(self._credentials_path)

    # --- 上传 ---

    async def push(self) -> dict[str, Any]:
        """把游标之后（含 10 分钟重叠）的明细分批推到云端，返回统计。"""
        creds = self.credentials()
        if creds is None:
            return {"ok": False, "error": "未配置设备凭据或已关闭套餐云归集", "pushed": 0}
        cursor = self._load_cursor()
        since = cursor - _OVERLAP if cursor is not None else None
        pushed = skipped = 0
        seen: set[str] = set()
        while True:
            rows = await self._repo.records_after(since, limit=_PUSH_BATCH)
            # 同一时刻的记录可能超过一批：本轮的 seen 保证游标停在同一时刻也不会空转
            fresh = [row for row in rows if str(row.id) not in seen]
            if not fresh:
                break
            for row in fresh:
                seen.add(str(row.id))
            try:
                result = await self._request(
                    "POST",
                    f"/api/devices/{creds.device_id}/billing/usage",
                    creds,
                    body={"items": [self._payload(row) for row in fresh]},
                )
            except Exception as exc:  # noqa: BLE001 - 网络异常只记账，等下一轮
                self._last_error = str(exc)
                logger.warning("推送套餐用量到云端失败：%s", exc)
                return {"ok": False, "error": str(exc), "pushed": pushed}
            pushed += int(result.get("accepted") or 0)
            skipped += int(result.get("skipped") or 0)
            since = _as_utc(rows[-1].occurred_at)
            if len(rows) < _PUSH_BATCH:
                break
        if since is not None:
            self._save_cursor(since)
        self._last_error = ""
        return {
            "ok": True,
            "pushed": pushed,
            "skipped": skipped,
            "cursor": since.isoformat() if since else None,
        }

    # --- 拉取 ---

    async def accounts(self) -> dict[str, list[str]] | None:
        """账号下所有设备用过的「套餐 → 账号指纹」，用于补齐本机没见过的账号。"""
        creds = self.credentials()
        if creds is None:
            return None
        try:
            data = await self._request(
                "GET", f"/api/devices/{creds.device_id}/billing/accounts", creds
            )
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            logger.warning("拉取云端套餐账号失败：%s", exc)
            return None
        if not isinstance(data, list):
            self._last_error = "云端账号列表格式不正确"
            return None
        grouped: dict[str, list[str]] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            plan_key = str(item.get("plan_key") or "")
            if not plan_key:
                continue
            grouped.setdefault(plan_key, []).append(str(item.get("account_key") or ""))
        self._last_error = ""
        return grouped

    async def summary(self, queries: list[dict[str, Any]]) -> dict[str, dict[str, Any]] | None:
        """按「套餐 × 账号 × 窗口起点」批量汇总账号下所有设备的用量。"""
        creds = self.credentials()
        if creds is None:
            return None
        result: dict[str, dict[str, Any]] = {}
        for start in range(0, len(queries), _SUMMARY_BATCH):
            batch = queries[start : start + _SUMMARY_BATCH]
            try:
                data = await self._request(
                    "POST",
                    f"/api/devices/{creds.device_id}/billing/summary",
                    creds,
                    body={"queries": batch},
                )
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                logger.warning("拉取云端用量汇总失败：%s", exc)
                return None
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                self._last_error = "云端汇总格式不正确"
                return None
            for item in items:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key") or "")
                if key:
                    result[key] = item
        self._last_error = ""
        return result

    # --- 内部 ---

    def _payload(self, row: UsageRecord) -> dict[str, Any]:
        occurred = _as_utc(row.occurred_at)
        return {
            "uid": str(row.id),
            "plan_key": row.plan_key or "",
            "account_key": row.account_key or "",
            "provider": row.provider or "",
            "model": row.model or "",
            "input_tokens": int(row.input_tokens or 0),
            "output_tokens": int(row.output_tokens or 0),
            "cache_read_tokens": int(row.cache_read_tokens or 0),
            "cache_write_tokens": int(row.cache_write_tokens or 0),
            "total_tokens": int(row.total_tokens or 0),
            "cost": float(row.cost or 0.0),
            "occurred_at": occurred.isoformat(),
        }

    def _load_cursor(self) -> datetime | None:
        try:
            raw = self._cursor_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("读取套餐同步游标失败 %s: %s", self._cursor_path, exc)
            return None
        try:
            value = str(json.loads(raw).get("last_at") or "")
            return _as_utc(datetime.fromisoformat(value)) if value else None
        except (ValueError, AttributeError, TypeError):
            return None

    def _save_cursor(self, last_at: datetime) -> None:
        try:
            self._cursor_path.parent.mkdir(parents=True, exist_ok=True)
            self._cursor_path.write_text(
                json.dumps({"last_at": last_at.isoformat()}), encoding="utf-8"
            )
        except OSError as exc:  # 游标写不进去只影响下次多推一遍，云端幂等兜底
            logger.warning("写入套餐同步游标失败 %s: %s", self._cursor_path, exc)

    async def _request(
        self,
        method: str,
        path: str,
        creds: DeviceCredentials,
        *,
        body: dict | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {creds.token}"}
        base = self._settings.cloud_base_url.rstrip("/")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.request(
                method, f"{base}{path}", headers=headers, json=body, params=params
            )
        if response.status_code >= 400:
            raise RuntimeError(f"账号云返回 {response.status_code}：{response.text[:200]}")
        if not response.content:
            return {}
        return response.json()


__all__ = ["CloudBillingSync", "SYNC_INTERVAL_SECONDS"]
