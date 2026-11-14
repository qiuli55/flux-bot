"""出站图片 / 文件的共用规则（频道无关）。

入站媒体走 `inbound_media`（下载 + 落盘 + 转写）；出站反过来：文件已经在
`attachment_store` 里（用户上传的附件，或 Bot 从自己的工作区登记的文件），审批通过后
再由各频道调用官方上传接口发出去。

这里只放三件事，避免三个频道各写一套：

- 附件记录的规范化与审批卡文案（文件名 / 类型 / 大小）；
- 各平台官方的类型与大小上限（超过上限必须在**上传之前**失败，并给中文错误）；
- Bot 在对话轮次里登记出站附件的 `attach_file` 工具（只登记，绝不发送）。

红线：本模块不发送、不上传；`attach_file` 也只把文件复制进 attachment_store 并
返回元数据，真正发送仍由频道在审批结果里完成。
"""

from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flux.core.attachment_store import (
    ATTACHMENT_MAX_FILES,
    UploadedFile,
    register_workspace_file,
    resolve_upload,
    size_label,
)
from flux.errors import ValidationError

if TYPE_CHECKING:
    from flux.container import Container
    from flux.models.bot import Bot

__all__ = [
    "ATTACH_FILE_TOOL_NAME",
    "ATTACH_FILE_TOOLS",
    "CHANNEL_MEDIA_LIMITS",
    "MediaLimits",
    "attachment_line",
    "attachment_meta",
    "attachment_record",
    "attachment_records",
    "attachment_reply_payload",
    "limits_for",
    "normalize_attachment_records",
    "render_outbound_detail",
    "resolve_attachment",
    "run_attach_file_tool",
    "validate_channel_attachment",
]

#: 审批卡里展示的正文摘录上限（与各频道原有的 1500 字保持一致）
DETAIL_TEXT_CHARS = 1500
#: 一条回复最多带几个出站附件（复用入站同值）
MAX_OUTBOUND_ATTACHMENTS = ATTACHMENT_MAX_FILES
#: 图片扩展名：mime 缺失时用它判断图片，最终仍按平台白名单校验
_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".ico"})
_SAFE_UPLOAD_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: `attach_file` 工具名：只登记「本轮要发给用户的文件」，不触发发送。
ATTACH_FILE_TOOL_NAME = "attach_file"

ATTACH_FILE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": ATTACH_FILE_TOOL_NAME,
            "description": (
                "把一张图片或一个文件登记为本轮回复的出站附件。只登记、不发送；"
                "用户批准出站审批卡后才会真正发到频道。"
                "path 必须是 Bot 工作区内的文件，upload_id 必须是已经上传的附件 id，二者选一。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Bot 工作区内的文件路径（相对或绝对，不能越出工作区）",
                    },
                    "upload_id": {
                        "type": "string",
                        "description": "已上传附件的 upload_id（与 path 二选一）",
                    },
                    "name": {
                        "type": "string",
                        "description": "可选的展示文件名；默认用工作区里的文件名",
                    },
                },
            },
        },
    }
]


@dataclass(frozen=True)
class MediaLimits:
    """一个平台官方的出站媒体上限。"""

    label: str
    image_max_bytes: int
    file_max_bytes: int
    image_mimes: frozenset[str]
    image_exts: frozenset[str]
    image_types_label: str

    @property
    def image_max_mb(self) -> int:
        return self.image_max_bytes // (1024 * 1024)

    @property
    def file_max_mb(self) -> int:
        return self.file_max_bytes // (1024 * 1024)


#: 官方上限（见各频道发送端注释里的文档来源）：
#: - 飞书：图片 10MB，文件 30MB（im/v1/images、im/v1/files）；
#: - 企业微信：图片 10MB，文件 20MB（cgi-bin/media/upload）；
#: - 钉钉：保守取图片 10MB、文件 20MB（robot/messageFiles/upload）。
CHANNEL_MEDIA_LIMITS: dict[str, MediaLimits] = {
    "feishu": MediaLimits(
        label="飞书",
        image_max_bytes=10 * 1024 * 1024,
        file_max_bytes=30 * 1024 * 1024,
        image_mimes=frozenset({"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}),
        image_exts=frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}),
        image_types_label="JPG/PNG/GIF/WEBP/BMP",
    ),
    "wecom": MediaLimits(
        label="企业微信",
        image_max_bytes=10 * 1024 * 1024,
        file_max_bytes=20 * 1024 * 1024,
        # 企业微信图片素材官方只接受 JPG / PNG。
        image_mimes=frozenset({"image/jpeg", "image/png"}),
        image_exts=frozenset({".jpg", ".jpeg", ".png"}),
        image_types_label="JPG/PNG",
    ),
    "dingtalk": MediaLimits(
        label="钉钉",
        image_max_bytes=10 * 1024 * 1024,
        file_max_bytes=20 * 1024 * 1024,
        image_mimes=frozenset({"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}),
        image_exts=frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}),
        image_types_label="JPG/PNG/GIF/WEBP/BMP",
    ),
}


def limits_for(channel: str) -> MediaLimits:
    limits = CHANNEL_MEDIA_LIMITS.get((channel or "").strip().lower())
    if limits is None:  # pragma: no cover - 调用方都是三个正式频道
        raise ValidationError(f"未知频道的出站媒体规则：{channel}", details={"channel": channel})
    return limits


def _guess_mime(name: str, mime: str = "") -> str:
    return (mime or "").strip() or (mimetypes.guess_type(name)[0] or "")


def _looks_like_image(mime: str, name: str) -> bool:
    if (mime or "").strip().lower().startswith("image/"):
        return True
    return Path(name or "").suffix.lower() in _IMAGE_EXTS


def attachment_kind(mime: str, name: str) -> str:
    """图片 / 文件二选一；无法确认是图片时按文件发送（由平台文件接口兜底）。"""
    return "image" if _looks_like_image(mime, name) else "file"


def attachment_record(uploaded: UploadedFile) -> dict[str, Any]:
    """把落盘附件折成可持久化的草稿附件记录（不含绝对路径）。"""
    mime = _guess_mime(uploaded.name, uploaded.mime)
    return {
        "upload_id": uploaded.id,
        "name": uploaded.name,
        "mime": mime,
        "size": int(uploaded.size),
        "kind": attachment_kind(mime, uploaded.name),
    }


def normalize_attachment_records(raw: object) -> list[dict[str, Any]]:
    """清洗来自消息 payload / 旧草稿的附件列表；只保留已知字段。

    `upload_id` 是发送时唯一的定位键，因此没有 `upload_id` 的条目直接丢弃。
    """
    if not isinstance(raw, list):
        return []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        upload_id = str(item.get("upload_id") or item.get("id") or "").strip()
        if not upload_id or upload_id in seen or not _SAFE_UPLOAD_ID.match(upload_id):
            continue
        name = str(item.get("name") or upload_id).strip() or upload_id
        mime = _guess_mime(name, str(item.get("mime") or ""))
        try:
            size = max(0, int(item.get("size") or 0))
        except (TypeError, ValueError):
            size = 0
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in {"image", "file"}:
            kind = attachment_kind(mime, name)
        records.append(
            {
                "upload_id": upload_id,
                "name": name,
                "mime": mime,
                "size": size,
                "kind": kind,
            }
        )
        seen.add(upload_id)
        if len(records) >= MAX_OUTBOUND_ATTACHMENTS:
            break
    return records


def attachment_records(raw: object) -> list[dict[str, Any]]:
    """草稿 / 审批 meta 使用的附件列表（`None` → 空列表）。"""
    return normalize_attachment_records(raw)


def attachment_line(record: dict[str, Any]) -> str:
    """审批卡里的一行附件说明：文件名 + 类型 + 人读大小。"""
    kind = "图片" if str(record.get("kind") or "") == "image" else "文件"
    name = str(record.get("name") or record.get("upload_id") or "未命名附件")
    mime = str(record.get("mime") or "未知类型")
    try:
        size = max(0, int(record.get("size") or 0))
    except (TypeError, ValueError):
        size = 0
    return f"{kind} {name}（{mime}，{size_label(size)}）"


def attachment_meta(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """审批 meta 里的附件摘要：只放展示与核对所需字段，不放路径 / 文件内容。"""
    return [
        {
            "upload_id": str(record.get("upload_id") or ""),
            "name": str(record.get("name") or ""),
            "kind": str(record.get("kind") or "file"),
            "mime": str(record.get("mime") or ""),
            "size": int(record.get("size") or 0),
            "size_label": size_label(int(record.get("size") or 0)),
        }
        for record in records
    ]


def _excerpt(text: str) -> str:
    if len(text) <= DETAIL_TEXT_CHARS:
        return text
    return text[:DETAIL_TEXT_CHARS] + "…"


def render_outbound_detail(preamble: str, body: str, records: list[dict[str, Any]]) -> str:
    """审批卡详情：一段目标说明 + （可选）正文摘录 + （可选）附件清单。"""
    sections = [(preamble or "").strip()]
    text = (body or "").strip()
    if text:
        sections.append(_excerpt(text))
    if records:
        lines = ["附件："] + [f"- {attachment_line(record)}" for record in records]
        sections.append("\n".join(lines))
    return "\n\n".join(section for section in sections if section)


def resolve_attachment(settings: Any, record: dict[str, Any]) -> UploadedFile:
    """按 upload_id 还原附件；进程重启后仍从 attachment_store 读同一个文件。"""
    upload_id = str(record.get("upload_id") or "").strip()
    name = str(record.get("name") or upload_id)
    uploaded = resolve_upload(settings, upload_id)
    if uploaded is None:
        raise ValidationError(
            f"出站附件已不存在或已被清理：{name}", details={"upload_id": upload_id}
        )
    return uploaded


def validate_channel_attachment(
    channel: str,
    *,
    name: str,
    mime: str,
    size: int,
    kind: str,
) -> None:
    """按平台官方上限校验；必须在上传之前调用，失败只给中文错误不发请求。"""
    limits = limits_for(channel)
    if kind == "image":
        if size > limits.image_max_bytes:
            raise ValidationError(
                f"该图片超过{limits.label}出站上限 {limits.image_max_mb}MB",
                details={"name": name, "size": size, "limit": limits.image_max_bytes},
            )
        ext = Path(name).suffix.lower()
        normalized_mime = (mime or "").strip().lower()
        if normalized_mime not in limits.image_mimes and ext not in limits.image_exts:
            raise ValidationError(
                f"{limits.label}出站图片仅支持 {limits.image_types_label} 格式（当前：{name}）",
                details={"name": name, "mime": mime},
            )
        return
    if size > limits.file_max_bytes:
        raise ValidationError(
            f"该文件超过{limits.label}出站上限 {limits.file_max_mb}MB",
            details={"name": name, "size": size, "limit": limits.file_max_bytes},
        )


async def run_attach_file_tool(
    container: Container,
    bot: Bot,
    arguments: dict[str, Any],
    targets: list[UploadedFile],
) -> dict[str, Any]:
    """`attach_file` 的实现：校验来源、登记到 attachment_store、记在本轮列表里。

    两种来源二选一：
    - `upload_id`：用户已经上传的附件，直接复用；
    - `path`：Bot 工作区里的文件，先做根目录 / 软链校验，再复制进 attachment_store。

    只登记，不上传平台、不发送。路径或附件非法时抛 `ValidationError`（中文），
    由工具循环回灌给模型，不会中断本轮。
    """
    upload_id = str(arguments.get("upload_id") or "").strip()
    raw_path = str(arguments.get("path") or "").strip()
    display_name = str(arguments.get("name") or "").strip()
    if bool(upload_id) == bool(raw_path):
        raise ValidationError("attach_file 需要且只能提供 path 或 upload_id 之一")
    if len(targets) >= MAX_OUTBOUND_ATTACHMENTS:
        raise ValidationError(f"一条回复最多带 {MAX_OUTBOUND_ATTACHMENTS} 个附件")

    settings = container.settings
    uploaded: UploadedFile | None
    if upload_id:
        uploaded = resolve_upload(settings, upload_id)
        if uploaded is None:
            raise ValidationError(f"附件不存在或已被清理：{upload_id}")
    else:
        root = Path(container.bots.workspace_path(bot))
        uploaded = register_workspace_file(settings, root, raw_path, name=display_name or None)

    record = attachment_record(uploaded)
    if any(item.id == uploaded.id for item in targets):
        return {**record, "size_label": size_label(record["size"]), "registered": True}
    targets.append(uploaded)
    return {
        **record,
        "size_label": size_label(record["size"]),
        "registered": True,
        "note": "已登记为出站附件；用户批准后才会发送",
    }


def attachment_reply_payload(files: list[UploadedFile]) -> list[dict[str, Any]]:
    """回复消息 payload 里的附件摘要（`_track_turn` 据此生成草稿）。"""
    return [attachment_record(item) for item in files]
