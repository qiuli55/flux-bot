"""账号级用户记忆的云端同步。

云端（B 机 `flux.qiuli55.top`）是用户记忆的权威存储：本机把本地新增的记忆推上去，
再定期拉回账号下的全量快照合并进本地缓存，换设备登录同一账号就能读到同一份记忆。

鉴权用设备令牌（`/etc/flux-device-credentials`，由 flux-device-agent 写入），
而不是「本机是否登录了账号云」——生产 A 机正是后者未登录、前者已就绪。
任何网络/鉴权失败都只记日志：记忆是辅助信息，不能拖垮任务执行。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from flux.core.memory.service import MemoryService
from flux.enums import MemoryLayer
from flux.logging import get_logger
from flux.models.memory import MemoryEntry

if TYPE_CHECKING:
    from flux.config import Settings

logger = get_logger(__name__)

#: 单次同步的 HTTP 超时（秒）
_TIMEOUT = 20.0
#: 后台同步周期（秒）：记忆不是实时协作数据，15 分钟足够跨设备收敛
SYNC_INTERVAL_SECONDS = 900.0
#: 批量推送上限（与云端 batch schema 的 max_length 对齐）
_PUSH_BATCH = 20


@dataclass(frozen=True, slots=True)
class DeviceCredentials:
    device_id: str
    token: str


def load_device_credentials(path: Path) -> DeviceCredentials | None:
    """解析 `KEY=VALUE` 形式的设备凭据；不存在或字段不全返回 None。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("读取设备凭据失败 %s: %s", path, exc)
        return None
    values: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    device_id = values.get("FLUX_DEVICE_ID", "")
    token = values.get("FLUX_DEVICE_TOKEN", "")
    if not device_id or not token:
        return None
    return DeviceCredentials(device_id=device_id, token=token)


class CloudMemorySync:
    """把本地 User Memory 与账号云双向同步。"""

    def __init__(
        self,
        settings: Settings,
        memory: MemoryService,
        *,
        credentials_path: Path | None = None,
    ) -> None:
        self._settings = settings
        self._memory = memory
        self._credentials_path = credentials_path or settings.device_credentials_path
        self._last_error: str = ""
        #: 最近一次拉取是否成功；设置页据此显示「已连接」而不是「有凭据」
        self._last_ok: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self._settings.memory_cloud_sync)

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def last_ok(self) -> bool:
        return self._last_ok

    def credentials(self) -> DeviceCredentials | None:
        if not self.enabled:
            return None
        return load_device_credentials(self._credentials_path)

    async def sync_once(self) -> dict[str, Any]:
        """先补推本地未上云的条目，再拉取云端快照。返回统计，失败时带上 error。"""
        creds = self.credentials()
        if creds is None:
            return {"ok": False, "error": "未配置设备凭据或已关闭云同步"}
        pushed = await self.push_pending()
        applied = await self.pull()
        if applied is None:
            return {"ok": False, "error": self._last_error or "云端记忆拉取失败", "pushed": pushed}
        return {"ok": True, "pushed": pushed, **applied}

    async def start(self, interval: float = SYNC_INTERVAL_SECONDS) -> None:
        """后台循环：启动先跑一轮，之后按周期同步；取消时安静退出。"""
        while True:
            try:
                await self.sync_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 同步失败只记账，等下一轮
                logger.warning("用户记忆云同步失败", exc_info=True)
            await asyncio.sleep(interval)

    async def pull(self) -> dict[str, int] | None:
        creds = self.credentials()
        if creds is None:
            return None
        try:
            items = await self._request("GET", f"/api/devices/{creds.device_id}/memory", creds)
        except Exception as exc:  # noqa: BLE001 - 网络异常不能影响本地使用
            self._last_error = str(exc)
            self._last_ok = False
            logger.warning("拉取账号级用户记忆失败：%s", exc)
            return None
        if not isinstance(items, list):
            self._last_error = "云端返回格式不正确"
            self._last_ok = False
            return None
        result = await self._memory.apply_cloud_snapshot(
            [item for item in items if isinstance(item, dict)]
        )
        self._last_error = ""
        self._last_ok = True
        if any(result.values()):
            logger.info(
                "用户记忆已从云端合并 inserted=%s updated=%s removed=%s",
                result["inserted"],
                result["updated"],
                result["removed"],
            )
        return result

    async def push_entry(self, entry: MemoryEntry) -> MemoryEntry | None:
        """推送一条本地记忆并回填 cloud_id；已有 cloud_id 或失败时返回 None。"""
        if str((entry.meta or {}).get("cloud_id") or ""):
            return None
        pushed = await self.push_entries([entry])
        return pushed.get(str(entry.id))

    async def push_entries(self, entries: list[MemoryEntry]) -> dict[str, MemoryEntry]:
        """批量推送，返回 本地 id → 更新后的条目。"""
        creds = self.credentials()
        if creds is None or not entries:
            return {}
        result: dict[str, MemoryEntry] = {}
        for start in range(0, len(entries), _PUSH_BATCH):
            batch = entries[start : start + _PUSH_BATCH]
            # 上次「本地改了但云端没改成」留下的旧行先删掉，避免同一内容出现两条
            for entry in batch:
                stale = str((entry.meta or {}).get("stale_cloud_id") or "")
                if stale:
                    await self.delete_remote(stale)
            payload = {
                "items": [
                    {
                        "content": entry.content,
                        "source": entry.source or "auto",
                        "meta": {
                            k: v
                            for k, v in (entry.meta or {}).items()
                            if k not in {"cloud_id", "stale_cloud_id"}
                        },
                    }
                    for entry in batch
                ]
            }
            try:
                response = await self._request(
                    "POST", f"/api/devices/{creds.device_id}/memory", creds, body=payload
                )
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                logger.warning("推送用户记忆到云端失败：%s", exc)
                continue
            items = response.get("items") if isinstance(response, dict) else None
            if not isinstance(items, list) or len(items) != len(batch):
                logger.warning("云端推送返回的条目数不匹配，跳过本轮回填")
                continue
            for entry, item in zip(batch, items, strict=False):
                cloud_id = str(item.get("id") or "")
                if not cloud_id:
                    continue
                meta = {
                    k: v
                    for k, v in (entry.meta or {}).items()
                    if k not in {"cloud_id", "stale_cloud_id"}
                }
                meta["cloud_id"] = cloud_id
                try:
                    updated = await self._memory.update_entry_meta(entry.id, meta)
                except Exception:  # noqa: BLE001
                    logger.warning("回填 cloud_id 失败 memory=%s", entry.id, exc_info=True)
                    continue
                result[str(entry.id)] = updated
            self._last_error = ""
        return result

    async def push_pending(self) -> int:
        """把本地还没有 cloud_id 的 User 记忆补推上去，返回推送条数。"""
        entries = await self._memory.list_entries(layer=MemoryLayer.USER)
        pending = [entry for entry in entries if not str((entry.meta or {}).get("cloud_id") or "")]
        if not pending:
            return 0
        pushed = await self.push_entries(pending)
        return len(pushed)

    async def update_remote(self, cloud_id: str, entry: MemoryEntry) -> bool:
        """把本地修改同步到云端；失败返回 False（调用方退化为待补推）。"""
        creds = self.credentials()
        if creds is None or not cloud_id:
            return False
        try:
            await self._request(
                "PUT",
                f"/api/devices/{creds.device_id}/memory/{cloud_id}",
                creds,
                body={
                    "content": entry.content,
                    "source": entry.source or "user",
                    "meta": {
                        k: v
                        for k, v in (entry.meta or {}).items()
                        if k not in {"cloud_id", "stale_cloud_id"}
                    },
                },
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("修改云端记忆失败 cloud_id=%s: %s", cloud_id, exc)
            return False

    async def delete_remote(self, cloud_id: str) -> bool:
        """删除云端一条记忆；失败不影响本地删除（调用方继续）。"""
        creds = self.credentials()
        if creds is None or not cloud_id:
            return False
        try:
            await self._request(
                "DELETE", f"/api/devices/{creds.device_id}/memory/{cloud_id}", creds
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除云端记忆失败 cloud_id=%s: %s", cloud_id, exc)
            return False

    async def _request(
        self,
        method: str,
        path: str,
        creds: DeviceCredentials,
        *,
        body: dict | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {creds.token}"}
        base = self._settings.cloud_base_url.rstrip("/")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.request(method, f"{base}{path}", headers=headers, json=body)
        if response.status_code >= 400:
            raise RuntimeError(f"账号云返回 {response.status_code}：{response.text[:200]}")
        if not response.content:
            return {}
        return response.json()


__all__ = [
    "CloudMemorySync",
    "DeviceCredentials",
    "SYNC_INTERVAL_SECONDS",
    "load_device_credentials",
]
