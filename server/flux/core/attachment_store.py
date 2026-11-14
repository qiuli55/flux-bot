"""聊天附件（用户从手机上传的文件）的落盘与还原。

附件只存文件系统、不进数据库：上传发生在任务创建之前（新对话可以先把文件加进来），
那时还没有 task_id 可挂。每个附件一个目录 `<uploads_dir>/<upload_id>/`，里面是
`meta.json` 与实体文件；任务消息只记 upload_id，读到 id 就能还原出绝对路径。

Agent 拿到的就是这个绝对路径——它用原生读文件能力/终端直接看，不需要再走 Flux MCP。
"""

from __future__ import annotations

import json
import mimetypes
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flux.config import Settings
from flux.core.user_scope import normalize_cloud_user_id, owns
from flux.core.virtual_workspace.path_guard import resolve_within_root
from flux.errors import ValidationError
from flux.logging import get_logger

logger = get_logger(__name__)

#: 单个附件上限（手机端同值校验；服务端不依赖客户端自证）
ATTACHMENT_MAX_BYTES = 50 * 1024 * 1024
#: 一条消息最多带几个附件
ATTACHMENT_MAX_FILES = 5
#: 落盘文件名长度上限：手机相册的名字经常非常长，截断后仍保留扩展名
_NAME_MAX_CHARS = 120
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")


#: upload_id 只允许目录名安全的字符；与出站附件记录共用同一套保守规则。
_SAFE_UPLOAD_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: 回源时能接受的 MIME 形状（挡掉头部注入 / 参数污染）。
_MIME_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")
#: 只有这些类型允许手机内联预览；其余一律按 octet-stream + 下载处理。
_INLINE_MIME_PREFIXES = ("image/", "audio/", "video/")
_INLINE_MIME_EXACT = frozenset({"application/pdf", "text/plain"})
#: 能内联但会执行脚本 / 加载外部资源的类型，强制按附件下载。
_ACTIVE_MIMES = frozenset({"image/svg+xml"})


@dataclass(frozen=True)
class UploadedFile:
    """一个已落盘的附件；`path` 是给 Agent 读的绝对路径，绝不下发给客户端。"""

    id: str
    name: str
    size: int
    mime: str
    path: str
    #: 归属的 Flux 云用户；None = Phase 2 之前写入的老附件（按主用户兼容）。
    cloud_user_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """客户端可见的元数据：不含宿主绝对路径、不含归属人内部标识。"""
        return {
            "id": self.id,
            "name": self.name,
            "size": self.size,
            "mime": self.mime,
        }

    def to_internal_dict(self) -> dict[str, Any]:
        """落库 / Agent 指令用的元数据：带绝对路径（只在服务端内部流转）。

        `TaskMessage.to_dict()` 在返回客户端前会剥掉 `files` / `attachments`
        里的 `path`，所以这里可以安全地保留路径供 Agent 原生读文件。
        """
        return {**self.to_dict(), "path": self.path}

    def meta_dict(self) -> dict[str, Any]:
        """写进 `meta.json` 的完整记录（含路径与归属人）。"""
        data = self.to_internal_dict()
        if self.cloud_user_id:
            data["cloud_user_id"] = self.cloud_user_id
        return data


def uploads_root(settings: Settings) -> Path:
    root = Path(settings.uploads_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_name(name: str) -> str:
    """只留一层安全的文件名：去目录成分、去掉 Windows 非法字符，并保留扩展名。"""
    base = Path(str(name or "").replace("\\", "/")).name.strip() or "file"
    cleaned = _UNSAFE_CHARS.sub("_", base).strip("._") or "file"
    if len(cleaned) <= _NAME_MAX_CHARS:
        return cleaned
    suffix = Path(cleaned).suffix[:16]
    return cleaned[: _NAME_MAX_CHARS - len(suffix)] + suffix


async def save_upload(
    settings: Settings,
    *,
    name: str,
    mime: str,
    chunks: AsyncIterator[bytes],
    cloud_user_id: str | None = None,
) -> UploadedFile:
    """把上传流写进独立目录；超限或写失败时删掉半成品，不留垃圾。

    `cloud_user_id` 是这次上传的归属人（手机端为当前登录人，IM 入站为绑定人）；
    记在 `meta.json` 里，读取时据此做按人鉴权。
    """
    upload_id = uuid.uuid4().hex
    owner = normalize_cloud_user_id(cloud_user_id)
    folder = uploads_root(settings) / upload_id
    folder.mkdir(parents=True, exist_ok=False)
    target = folder / safe_name(name)
    size = 0
    try:
        with target.open("wb") as handle:
            async for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > ATTACHMENT_MAX_BYTES:
                    raise ValidationError(
                        f"单个附件不能超过 {ATTACHMENT_MAX_BYTES // (1024 * 1024)}MB",
                        details={"name": name, "limit": ATTACHMENT_MAX_BYTES},
                    )
                handle.write(chunk)
        if size == 0:
            raise ValidationError("附件内容为空", details={"name": name})
        uploaded = UploadedFile(
            id=upload_id,
            name=target.name,
            size=size,
            mime=mime or "",
            path=str(target),
            cloud_user_id=owner,
        )
        (folder / "meta.json").write_text(
            json.dumps(uploaded.meta_dict(), ensure_ascii=False), encoding="utf-8"
        )
        return uploaded
    except Exception:
        for path in (target, folder / "meta.json"):
            path.unlink(missing_ok=True)
        folder.rmdir()
        raise


def save_local_file(
    settings: Settings,
    source: Path,
    *,
    name: str | None = None,
    mime: str = "",
    cloud_user_id: str | None = None,
) -> UploadedFile:
    """把一个已在磁盘上的文件复制进 attachment_store（同步，供出站附件用）。

    与 `save_upload` 同一套目录结构与上限：Bot 从工作区登记文件时也走这里，
    平台发送端永远只拿到 attachment_store 里的副本，不直接读用户工作区路径。
    """
    try:
        source_stat = source.stat()
    except OSError as exc:
        raise ValidationError(
            f"找不到要发送的文件：{source}", details={"path": str(source)}
        ) from exc
    if not source.is_file():
        raise ValidationError(f"要发送的路径不是文件：{source}", details={"path": str(source)})
    if source_stat.st_size == 0:
        raise ValidationError("附件内容为空", details={"name": name or source.name})
    if source_stat.st_size > ATTACHMENT_MAX_BYTES:
        raise ValidationError(
            f"单个附件不能超过 {ATTACHMENT_MAX_BYTES // (1024 * 1024)}MB",
            details={"name": name or source.name, "limit": ATTACHMENT_MAX_BYTES},
        )

    upload_id = uuid.uuid4().hex
    owner = normalize_cloud_user_id(cloud_user_id)
    folder = uploads_root(settings) / upload_id
    folder.mkdir(parents=True, exist_ok=False)
    target = folder / safe_name(name or source.name)
    size = 0
    try:
        with source.open("rb") as src, target.open("wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > ATTACHMENT_MAX_BYTES:
                    raise ValidationError(
                        f"单个附件不能超过 {ATTACHMENT_MAX_BYTES // (1024 * 1024)}MB",
                        details={"name": target.name, "limit": ATTACHMENT_MAX_BYTES},
                    )
                dst.write(chunk)
        if size == 0:
            raise ValidationError("附件内容为空", details={"name": target.name})
        uploaded = UploadedFile(
            id=upload_id,
            name=target.name,
            size=size,
            mime=(mime or mimetypes.guess_type(target.name)[0] or ""),
            path=str(target),
            cloud_user_id=owner,
        )
        (folder / "meta.json").write_text(
            json.dumps(uploaded.meta_dict(), ensure_ascii=False), encoding="utf-8"
        )
        return uploaded
    except Exception:
        for path in (target, folder / "meta.json"):
            path.unlink(missing_ok=True)
        folder.rmdir()
        raise


def register_workspace_file(
    settings: Settings,
    root: str | Path,
    raw_path: str,
    *,
    name: str | None = None,
    cloud_user_id: str | None = None,
) -> UploadedFile:
    """校验一个 Bot 工作区内的文件并复制进 attachment_store。

    路径安全是硬红线：绝对路径必须落在 `root` 内，相对路径不允许 `..`，沿途任何
    软链都 fail-closed（复用 `resolve_within_root`）。通过后再按 50MB 上限复制。
    """
    raw = (raw_path or "").strip()
    if not raw or "\x00" in raw:
        raise ValidationError("附件路径不能为空", details={"path": raw})
    root_path = Path(root).expanduser()
    root_path.mkdir(parents=True, exist_ok=True)
    root_path = root_path.resolve()

    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            relative = candidate.resolve().relative_to(root_path)
        except ValueError as exc:
            raise ValidationError(
                "文件必须位于 Bot 工作区内，已拒绝该路径",
                details={"path": raw},
            ) from exc
    else:
        if any(part == ".." for part in candidate.parts):
            raise ValidationError("附件路径不能包含 ..", details={"path": raw})
        relative = candidate

    resolved = resolve_within_root(root_path, relative)
    if not resolved.is_file():
        raise ValidationError(f"工作区内找不到文件：{raw}", details={"path": str(relative)})
    target_name = safe_name(name or resolved.name)
    return save_local_file(
        settings,
        resolved,
        name=target_name,
        mime=mimetypes.guess_type(target_name)[0] or "",
        cloud_user_id=cloud_user_id,
    )


def resolve_upload(settings: Settings, upload_id: str) -> UploadedFile | None:
    """按 id 还原附件；目录或元数据缺失时返回 None（老消息里的附件可能已被清理）。

    路径安全是硬边界：`meta.json` 里的 `path` 必须落在该 upload 自己的目录内；
    被篡改成 `/etc/passwd`、软链到别处或 id 与目录名不一致都按"不存在"处理。
    这里不做大小校验——Agent 读文件走的是同一条还原路径，大小上限只在 HTTP
    回源时检查（见 `check_servable`）。
    """
    if not isinstance(upload_id, str) or not _SAFE_UPLOAD_ID.fullmatch(upload_id):
        return None
    folder = uploads_root(settings) / upload_id
    meta = folder / "meta.json"
    if not meta.is_file():
        return None
    try:
        raw = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("附件元数据损坏 upload=%s", upload_id)
        return None
    if not isinstance(raw, dict):
        return None
    stored_id = str(raw.get("id") or upload_id)
    if stored_id != upload_id:
        logger.warning("附件 id 与目录不一致 upload=%s stored=%s", upload_id, stored_id)
        return None
    try:
        folder_resolved = folder.resolve()
        path = Path(str(raw.get("path") or ""))
        if not path.is_absolute():
            path = folder / path
        resolved = path.resolve()
        resolved.relative_to(folder_resolved)
    except (OSError, ValueError):
        logger.warning("附件路径越界 upload=%s", upload_id)
        return None
    if not resolved.is_file():
        return None
    try:
        size = int(raw.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return UploadedFile(
        id=upload_id,
        name=str(raw.get("name") or resolved.name),
        size=max(0, size) or resolved.stat().st_size,
        mime=str(raw.get("mime") or ""),
        path=str(resolved),
        cloud_user_id=normalize_cloud_user_id(raw.get("cloud_user_id")),
    )


def check_servable(uploaded: UploadedFile) -> None:
    """HTTP 回源的最后一组护栏：文件仍在、非空、不超过单附件上限。

    授权检查必须在调用本函数**之前**完成，避免未授权用户靠 422/404 的差异
    探测某个 upload_id 是否存在。
    """
    path = Path(uploaded.path)
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValidationError("附件不存在或已被清理", details={"upload_id": uploaded.id}) from exc
    if not path.is_file() or stat.st_size <= 0:
        raise ValidationError("附件不存在或已被清理", details={"upload_id": uploaded.id})
    if stat.st_size > ATTACHMENT_MAX_BYTES:
        raise ValidationError(
            f"附件超过可读取的最大大小（{ATTACHMENT_MAX_BYTES // (1024 * 1024)}MB）",
            details={"upload_id": uploaded.id, "limit": ATTACHMENT_MAX_BYTES},
        )


def safe_content_type(name: str, mime: str) -> tuple[str, bool]:
    """返回 (Content-Type, 是否允许内联)。

    MIME 只作为数据使用：形状不合法、或不是可安全内联的类型时，统一降级成
    `application/octet-stream` 并要求下载。`X-Content-Type-Options: nosniff`
    由调用方一并设置，避免浏览器/客户端按内容猜类型执行。
    """
    raw = (mime or "").split(";", 1)[0].strip().lower()
    if not raw or not _MIME_SHAPE.fullmatch(raw):
        raw = (mimetypes.guess_type(name)[0] or "").lower()
    if not raw or not _MIME_SHAPE.fullmatch(raw):
        return "application/octet-stream", False
    if raw in _ACTIVE_MIMES:
        return "application/octet-stream", False
    inline = raw in _INLINE_MIME_EXACT or raw.startswith(_INLINE_MIME_PREFIXES)
    # 不能内联的类型统一按二进制下载：不把 text/html、脚本、XML 等交给客户端
    # 当网页执行（即便客户端忽略了 disposition）。
    return (raw, True) if inline else ("application/octet-stream", False)


def serve_filename(name: str) -> str:
    """回源时展示的文件名：去掉控制字符，空值兜底为 file。"""
    cleaned = "".join(
        ch for ch in str(name or "") if ch not in "\r\n\x00" and ord(ch) >= 32
    ).strip()
    return cleaned or "file"


def resolve_uploads(
    settings: Settings,
    upload_ids: list[str],
    *,
    cloud_user_id: str | None = None,
    owner_cloud_user_id: str | None = None,
) -> list[UploadedFile]:
    """批量还原（保持请求顺序）；有任何一个不存在就整体失败，避免半套附件。

    传入 `cloud_user_id`（或配置了主用户）时按人鉴权：别人的附件与不存在同一
    口径（不泄露 id 是否存在）；Phase 2 之前的无主附件只归主用户。
    """
    unique = [value for value in dict.fromkeys(str(item) for item in upload_ids) if value]
    if len(unique) > ATTACHMENT_MAX_FILES:
        raise ValidationError(
            f"一条消息最多带 {ATTACHMENT_MAX_FILES} 个附件",
            details={"count": len(unique), "limit": ATTACHMENT_MAX_FILES},
        )
    current = normalize_cloud_user_id(cloud_user_id) or normalize_cloud_user_id(owner_cloud_user_id)
    files = []
    for upload_id in unique:
        uploaded = resolve_upload(settings, upload_id)
        if uploaded is None or (
            current is not None and not owns(uploaded.cloud_user_id, current, owner_cloud_user_id)
        ):
            raise ValidationError(
                f"附件不存在或已被清理：{upload_id}", details={"upload_id": upload_id}
            )
        files.append(uploaded)
    return files


def size_label(size: int) -> str:
    """附件大小的人读写法（给指令里的清单用）。"""
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


__all__ = [
    "ATTACHMENT_MAX_BYTES",
    "ATTACHMENT_MAX_FILES",
    "UploadedFile",
    "check_servable",
    "register_workspace_file",
    "resolve_upload",
    "resolve_uploads",
    "safe_content_type",
    "safe_name",
    "save_local_file",
    "save_upload",
    "serve_filename",
    "size_label",
    "uploads_root",
]
