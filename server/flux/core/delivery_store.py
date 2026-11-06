"""AI → 用户的文件下发落盘与还原（与 `attachment_store` 方向相反）。

Agent 在任务里产出的成品（图片 / PDF / 文档 / 表格 / 压缩包 / 音视频）由
`file.deliver` 工具一次性收进 `<deliveries_dir>/<delivery_id>/`，随后写一条
`kind="file_card"` 的助手消息。消息 payload 只带 delivery_id、清单与归类，
手机端据此渲染堆叠卡片，按 `GET /deliveries/{delivery_id}/files/{index}` 取文件。

和上传一样只存文件系统、不进数据库：卡片消息本身是唯一索引，目录丢了就是
"文件已过期"，消息照常显示但点开报不存在。
"""

from __future__ import annotations

import json
import mimetypes
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flux.config import Settings
from flux.core.attachment_store import safe_name, size_label
from flux.errors import ValidationError
from flux.logging import get_logger

logger = get_logger(__name__)

#: 单个下发文件上限（手机端预览大文件也没意义，和上传保持同一量级）
DELIVERY_MAX_BYTES = 50 * 1024 * 1024
#: 一次下发最多几个文件（堆叠卡片按这个数量留位）
DELIVERY_MAX_FILES = 9

#: 扩展名 → 预览归类。手机端按 kind 选预览方式（图片位图 / PDF 渲染 / 文本滚动…）。
_KIND_BY_EXT: dict[str, frozenset[str]] = {
    "image": frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp", "heic", "heif", "svg", "ico"}),
    "pdf": frozenset({"pdf"}),
    "text": frozenset(
        {
            "txt",
            "md",
            "markdown",
            "log",
            "csv",
            "tsv",
            "json",
            "yaml",
            "yml",
            "xml",
            "toml",
            "ini",
            "py",
            "js",
            "ts",
            "tsx",
            "jsx",
            "kt",
            "java",
            "go",
            "rs",
            "c",
            "h",
            "cpp",
            "hpp",
            "cs",
            "rb",
            "php",
            "sh",
            "bash",
            "sql",
            "html",
            "htm",
            "css",
            "scss",
            "swift",
            "dart",
            "lua",
            "r",
            "ipynb",
            "diff",
            "patch",
        }
    ),
    "video": frozenset({"mp4", "mov", "m4v", "webm", "mkv", "avi", "3gp"}),
    "audio": frozenset({"mp3", "wav", "m4a", "aac", "ogg", "flac", "opus", "amr"}),
    "office": frozenset({"doc", "docx", "xls", "xlsx", "ppt", "pptx", "pages", "numbers", "key"}),
    "archive": frozenset({"zip", "tar", "gz", "tgz", "bz2", "xz", "7z", "rar"}),
}


@dataclass(frozen=True)
class DeliveryFile:
    """下发清单里的一项；`path` 是服务端实际落盘位置，不下发给客户端。"""

    index: int
    name: str
    size: int
    mime: str
    kind: str
    path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "size": self.size,
            "size_label": size_label(self.size),
            "mime": self.mime,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class Delivery:
    """一次下发（一条聊天卡片消息对应一个 delivery）。"""

    id: str
    note: str
    created_at: str
    files: tuple[DeliveryFile, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivery_id": self.id,
            "note": self.note,
            "created_at": self.created_at,
            "files": [item.to_dict() for item in self.files],
        }


def deliveries_root(settings: Settings) -> Path:
    root = Path(settings.deliveries_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def kind_for(name: str, mime: str = "") -> str:
    """按扩展名归类；没有扩展名时退回 MIME 的顶层类型。"""
    ext = Path(str(name or "")).suffix.lower().lstrip(".")
    for kind, suffixes in _KIND_BY_EXT.items():
        if ext in suffixes:
            return kind
    top = (mime or "").split("/", 1)[0].strip().lower()
    if top in {"image", "video", "audio", "text"}:
        return top
    return "other"


def mime_for(name: str, fallback: str = "") -> str:
    return mimetypes.guess_type(name)[0] or fallback or "application/octet-stream"


def create_delivery(
    settings: Settings,
    *,
    sources: list[tuple[str, Path]],
    note: str = "",
) -> Delivery:
    """把 `(显示名, 源文件)` 复制进一个 delivery 目录；任何一步失败都不留半套。"""
    if not sources:
        raise ValidationError("没有可下发的文件", details={"count": 0})
    if len(sources) > DELIVERY_MAX_FILES:
        raise ValidationError(
            f"一次最多下发 {DELIVERY_MAX_FILES} 个文件",
            details={"count": len(sources), "limit": DELIVERY_MAX_FILES},
        )

    delivery_id = uuid.uuid4().hex
    folder = deliveries_root(settings) / delivery_id
    folder.mkdir(parents=True, exist_ok=False)
    files: list[DeliveryFile] = []
    try:
        for index, (display_name, source) in enumerate(sources):
            if not source.is_file():
                raise ValidationError(
                    f"文件不存在或不是普通文件：{display_name}",
                    details={"name": display_name},
                )
            size = source.stat().st_size
            if size <= 0:
                raise ValidationError("文件内容为空", details={"name": display_name})
            if size > DELIVERY_MAX_BYTES:
                raise ValidationError(
                    f"单个文件不能超过 {DELIVERY_MAX_BYTES // (1024 * 1024)}MB",
                    details={"name": display_name, "limit": DELIVERY_MAX_BYTES},
                )
            stored = folder / f"{index:02d}-{safe_name(display_name)}"
            stored.write_bytes(source.read_bytes())
            mime = mime_for(stored.name)
            files.append(
                DeliveryFile(
                    index=index,
                    name=safe_name(display_name),
                    size=size,
                    mime=mime,
                    kind=kind_for(display_name, mime),
                    path=str(stored),
                )
            )
        delivery = Delivery(
            id=delivery_id,
            note=note or "",
            created_at=datetime.now(timezone.utc).isoformat(),
            files=tuple(files),
        )
        (folder / "meta.json").write_text(
            json.dumps(
                {
                    "id": delivery.id,
                    "note": delivery.note,
                    "created_at": delivery.created_at,
                    "files": [
                        {
                            "index": item.index,
                            "name": item.name,
                            "size": item.size,
                            "mime": item.mime,
                            "kind": item.kind,
                            "path": item.path,
                        }
                        for item in delivery.files
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return delivery
    except Exception:
        for path in folder.glob("*"):
            path.unlink(missing_ok=True)
        folder.rmdir()
        raise


def resolve_delivery(settings: Settings, delivery_id: str) -> Delivery | None:
    """按 id 还原下发清单；目录/元数据缺失或文件已被清理时返回 None。"""
    if not delivery_id or "/" in delivery_id or "\\" in delivery_id:
        return None
    folder = deliveries_root(settings) / delivery_id
    meta = folder / "meta.json"
    if not meta.is_file():
        return None
    try:
        raw = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("下发元数据损坏 delivery=%s", delivery_id)
        return None
    items: list[DeliveryFile] = []
    for entry in raw.get("files") or []:
        try:
            path = Path(str(entry.get("path") or ""))
            index = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        if not path.is_file():
            continue
        name = str(entry.get("name") or path.name)
        mime = str(entry.get("mime") or mime_for(name))
        items.append(
            DeliveryFile(
                index=index,
                name=name,
                size=int(entry.get("size") or path.stat().st_size),
                mime=mime,
                kind=str(entry.get("kind") or kind_for(name, mime)),
                path=str(path),
            )
        )
    if not items:
        return None
    return Delivery(
        id=str(raw.get("id") or delivery_id),
        note=str(raw.get("note") or ""),
        created_at=str(raw.get("created_at") or ""),
        files=tuple(sorted(items, key=lambda item: item.index)),
    )


def resolve_delivery_file(settings: Settings, delivery_id: str, index: int) -> DeliveryFile | None:
    delivery = resolve_delivery(settings, delivery_id)
    if delivery is None:
        return None
    for item in delivery.files:
        if item.index == index:
            return item
    return None


__all__ = [
    "DELIVERY_MAX_BYTES",
    "DELIVERY_MAX_FILES",
    "Delivery",
    "DeliveryFile",
    "create_delivery",
    "deliveries_root",
    "kind_for",
    "mime_for",
    "resolve_delivery",
    "resolve_delivery_file",
]
