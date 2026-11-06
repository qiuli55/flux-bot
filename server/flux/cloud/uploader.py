"""把「上一轮运行的日志」自动上传到 Flux 账号云。

为什么要这样设计：

- 每次 Flux 启动都会在 `log_dir` 下新开一份 `run-<启动时间>.log`（见 flux.logging），
  所以「上一轮运行的日志」= 上一次启动写下的那份文件，天然有始有终；
- `runs.json` 是这份目录的清单（最多保留 `_MAX_ENTRIES` 条），每条记录是否已上传。
  本次启动先把自己的日志登记进去（`uploaded: false`），再去祖先里挑**最近一条**
  尚未上传的日志上传——这样既不会重复上传，也不会把"正在写"的这份传出去；
- 任何失败（没登录、网络抖动、云端报错）都只记日志：日志上传是辅助能力，
  绝不能因为它让 Flux 本体启动失败或卡住。
"""

from __future__ import annotations

import json
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from flux.cloud.session import CloudClient, CloudError, load_session, save_session
from flux.config import Settings
from flux.logging import get_logger, run_log_file, run_started_at
from flux.version import VERSION

logger = get_logger(__name__)

_MANIFEST_NAME = "runs.json"
_MAX_ENTRIES = 5


def _iso(value: datetime | None) -> str:
    return "" if value is None else value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _manifest_path(log_dir: Path) -> Path:
    return log_dir / _MANIFEST_NAME


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("读取运行日志清单失败 %s: %s", path, exc)
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("运行日志清单不是合法 JSON：%s", path)
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _save_manifest(path: Path, entries: list[dict[str, Any]]) -> None:
    try:
        path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("写入运行日志清单失败 %s: %s", path, exc)


def _pending_entry(entries: list[dict[str, Any]], log_dir: Path) -> dict[str, Any] | None:
    """最近一条尚未上传、且文件仍在的日志（不含本次启动刚登记的那条）。"""
    for item in reversed(entries[:-1]):
        if item.get("uploaded"):
            continue
        name = str(item.get("file") or "")
        if name and (log_dir / name).is_file():
            return item
    return None


async def upload_previous_run(settings: Settings) -> None:
    """启动时调用。吞掉一切异常——后台任务不能把启动流程带崩。"""
    log_dir = settings.log_dir_path
    if log_dir is None:
        return
    try:
        await _upload(settings, log_dir)
    except Exception:
        logger.warning("自动上传运行日志失败", exc_info=True)


async def _upload(settings: Settings, log_dir: Path) -> None:
    manifest = _manifest_path(log_dir)
    entries = _load_manifest(manifest)

    current = run_log_file()
    if current is not None:
        previous = entries[-1] if entries else None
        entries.append(
            {
                "file": current.name,
                "started_at": _iso(run_started_at()),
                "prev_started_at": str(previous.get("started_at") or "") if previous else "",
                "uploaded": False,
            }
        )
        entries = entries[-_MAX_ENTRIES:]
        _save_manifest(manifest, entries)

    pending = _pending_entry(entries, log_dir)
    if pending is None:
        return

    session_path = settings.cloud_session_path
    session = load_session(session_path)
    if session is None:
        logger.info("本机未登录账号云，跳过本轮日志自动上传")
        return

    client = CloudClient(settings.cloud_base_url)
    if session.expired:
        try:
            session = await client.refresh(session.refresh_token)
        except (CloudError, httpx.HTTPError) as exc:
            logger.warning("续期账号云会话失败：%s", exc)
            return
        save_session(session_path, session)

    path = log_dir / str(pending["file"])
    try:
        await client.upload_log(
            session,
            path=path,
            run_started_at=str(pending.get("started_at") or ""),
            prev_run_started_at=str(pending.get("prev_started_at") or ""),
            client_version=VERSION,
            os_info=f"{platform.system()} {platform.release()} ({platform.machine()})",
            note="自动上传：Flux 上一轮运行日志",
        )
    except (CloudError, httpx.HTTPError) as exc:
        logger.warning("上传运行日志 %s 失败：%s", path.name, exc)
        return

    pending["uploaded"] = True
    _save_manifest(manifest, entries)
    logger.info("已自动上传上一轮运行日志 %s", path.name)
