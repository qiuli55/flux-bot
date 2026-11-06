"""对话上下文的云端备份。

隐私输入卡片里填的内容只进本地密钥库、不进对话；除此之外的对话上下文
（用户/助手的文本、摘要、交接，以及安全输入卡片本身——卡片只有标题与说明）这里负责
增量推到账号云，换设备登录同一账号后也能回看。

与套餐用量归集同一范式：设备令牌鉴权、批量推送、失败只记日志，不影响本地对话。
待传队列直接落在 `task_messages.cloud_uploaded_at`（NULL = 未处理），不另建游标文件；
云端按 (设备, message_uid) 幂等，重复推送只会得到 skipped。

本地再守一道密钥红线：用户手滑贴进聊天里的私钥/供应商令牌不离开本机——
这类消息直接标记为已处理（= 主动跳过），既不上传也不每轮重扫。
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from flux.cloud.memory_sync import DeviceCredentials, load_device_credentials
from flux.core.task_engine.repository import TaskRepository
from flux.logging import get_logger
from flux.models.task import Task, TaskMessage

if TYPE_CHECKING:
    from flux.config import Settings

logger = get_logger(__name__)

#: 单次 HTTP 超时（秒）
_TIMEOUT = 20.0
#: 后台推送周期（秒）：对话上下文不是实时协作数据，2 分钟足够跨设备收敛
SYNC_INTERVAL_SECONDS = 120.0
#: 单批推送上限（与云端 `ContextBatchIn.items` 的 max_length 对齐）
_PUSH_BATCH = 200
#: 单条消息正文上限（与云端 schema 的 max_length 对齐）；超长截断后再传
MAX_CONTENT_CHARS = 20000
#: 可上传的消息类型。`secure_input_result` 不在内：回执里带 key_id / key_hint，
#: 属于隐私输入的落地痕迹，只留在本机；`secure_input` 卡片本身不含明文，可以传。
CONTEXT_UPLOAD_KINDS: tuple[str, ...] = ("text", "summary", "handoff", "secure_input")

#: 明确的密钥形状：只在"看起来就是凭据"时跳传，避免误伤正常的技术讨论
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abpsr]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


def _as_utc(value: datetime) -> datetime:
    """SQLite 读回会丢 tzinfo，统一按 UTC 归一。"""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def looks_secret(text: str) -> bool:
    """正文里是否含有可识别的密钥形状（命中即不离开本机）。"""
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


class CloudContextSync:
    """本地对话消息 → 账号云的单向增量备份。"""

    def __init__(
        self,
        settings: Settings,
        repo: TaskRepository,
        *,
        credentials_path: Path | None = None,
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._credentials_path = credentials_path or settings.device_credentials_path
        self._last_error: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self._settings.context_cloud_sync)

    @property
    def last_error(self) -> str:
        return self._last_error

    def credentials(self) -> DeviceCredentials | None:
        if not self.enabled:
            return None
        return load_device_credentials(self._credentials_path)

    async def push(self) -> dict[str, Any]:
        """把尚未处理的对话消息分批推到云端，返回统计。

        任何网络/鉴权失败都只记 `last_error` 并返回，队列保持原样等下一轮重试。
        """
        creds = self.credentials()
        if creds is None:
            return {
                "ok": False,
                "error": "未配置设备凭据或已关闭上下文云同步",
                "pushed": 0,
                "skipped": 0,
            }
        pushed = skipped = 0
        while True:
            rows = await self._repo.pending_context_messages(
                CONTEXT_UPLOAD_KINDS, limit=_PUSH_BATCH
            )
            if not rows:
                break
            batch: list[tuple[TaskMessage, Task]] = []
            withheld: list[Any] = []
            for message, task in rows:
                if looks_secret(message.content):
                    withheld.append(message.id)
                else:
                    batch.append((message, task))
            processed_at = datetime.now(timezone.utc)
            if batch:
                try:
                    result = await self._request(
                        "POST",
                        f"/api/devices/{creds.device_id}/context/messages",
                        creds,
                        body={"items": [self._payload(m, t) for m, t in batch]},
                    )
                except Exception as exc:  # noqa: BLE001 - 网络异常只记账，等下一轮
                    self._last_error = str(exc)
                    logger.warning("推送对话上下文到云端失败：%s", exc)
                    return {"ok": False, "error": str(exc), "pushed": pushed, "skipped": skipped}
                pushed += int(result.get("accepted") or 0)
                skipped += int(result.get("skipped") or 0)
                await self._repo.mark_context_uploaded([m.id for m, _ in batch], at=processed_at)
            if withheld:
                # 命中红线：不上传，但同样标记为已处理，避免每轮重复扫描同一批
                await self._repo.mark_context_uploaded(withheld, at=processed_at)
                skipped += len(withheld)
            if len(rows) < _PUSH_BATCH:
                break
        self._last_error = ""
        return {"ok": True, "pushed": pushed, "skipped": skipped}

    async def start(self, interval: float = SYNC_INTERVAL_SECONDS) -> None:
        """后台循环：启动先跑一轮，之后按周期推送；取消时安静退出。"""
        while True:
            try:
                await self.push()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 同步失败只记账，等下一轮
                logger.warning("对话上下文云同步失败", exc_info=True)
            await asyncio.sleep(interval)

    # --- 内部 ---

    def _payload(self, message: TaskMessage, task: Task) -> dict[str, Any]:
        created_at = _as_utc(message.created_at).isoformat() if message.created_at else None
        task_created_at = _as_utc(task.created_at).isoformat() if task.created_at else None
        return {
            "message_uid": str(message.id),
            "task_uid": str(task.id),
            "task_description": (task.description or "")[:512],
            "task_status": task.status,
            "task_model": task.model or "",
            "task_created_at": task_created_at,
            "seq": int(message.seq),
            "role": message.role,
            "kind": message.kind,
            "content": message.content[:MAX_CONTENT_CHARS],
            "payload": message.payload,
            "created_at": created_at,
        }

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
    "CONTEXT_UPLOAD_KINDS",
    "CloudContextSync",
    "MAX_CONTENT_CHARS",
    "SYNC_INTERVAL_SECONDS",
    "looks_secret",
]
