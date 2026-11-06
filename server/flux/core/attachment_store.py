"""聊天附件（用户从手机上传的文件）的落盘与还原。

附件只存文件系统、不进数据库：上传发生在任务创建之前（新对话可以先把文件加进来），
那时还没有 task_id 可挂。每个附件一个目录 `<uploads_dir>/<upload_id>/`，里面是
`meta.json` 与实体文件；任务消息只记 upload_id，读到 id 就能还原出绝对路径。

Agent 拿到的就是这个绝对路径——它用原生读文件能力/终端直接看，不需要再走 Flux MCP。
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flux.config import Settings
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


@dataclass(frozen=True)
class UploadedFile:
    """一个已落盘的附件；`path` 是给 Agent 读的绝对路径。"""

    id: str
    name: str
    size: int
    mime: str
    path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "size": self.size,
            "mime": self.mime,
            "path": self.path,
        }


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
    settings: Settings, *, name: str, mime: str, chunks: AsyncIterator[bytes]
) -> UploadedFile:
    """把上传流写进独立目录；超限或写失败时删掉半成品，不留垃圾。"""
    upload_id = uuid.uuid4().hex
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
            id=upload_id, name=target.name, size=size, mime=mime or "", path=str(target)
        )
        (folder / "meta.json").write_text(
            json.dumps(uploaded.to_dict(), ensure_ascii=False), encoding="utf-8"
        )
        return uploaded
    except Exception:
        for path in (target, folder / "meta.json"):
            path.unlink(missing_ok=True)
        folder.rmdir()
        raise


def resolve_upload(settings: Settings, upload_id: str) -> UploadedFile | None:
    """按 id 还原附件；目录或元数据缺失时返回 None（老消息里的附件可能已被清理）。"""
    if not upload_id or "/" in upload_id or "\\" in upload_id:
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
    path = Path(str(raw.get("path") or ""))
    if not path.is_file():
        return None
    return UploadedFile(
        id=str(raw.get("id") or upload_id),
        name=str(raw.get("name") or path.name),
        size=int(raw.get("size") or 0),
        mime=str(raw.get("mime") or ""),
        path=str(path),
    )


def resolve_uploads(settings: Settings, upload_ids: list[str]) -> list[UploadedFile]:
    """批量还原（保持请求顺序）；有任何一个不存在就整体失败，避免半套附件。"""
    unique = [value for value in dict.fromkeys(str(item) for item in upload_ids) if value]
    if len(unique) > ATTACHMENT_MAX_FILES:
        raise ValidationError(
            f"一条消息最多带 {ATTACHMENT_MAX_FILES} 个附件",
            details={"count": len(unique), "limit": ATTACHMENT_MAX_FILES},
        )
    files = []
    for upload_id in unique:
        uploaded = resolve_upload(settings, upload_id)
        if uploaded is None:
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
    "resolve_upload",
    "resolve_uploads",
    "safe_name",
    "save_upload",
    "size_label",
    "uploads_root",
]
