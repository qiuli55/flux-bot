"""频道入站媒体：官方接口下载 → 复用 uploads 存储 → 交给 Bot turn。

三个正式频道（飞书 / 企业微信 / 钉钉）的媒体下载形态不同，但落盘口径必须与
手机端上传完全一致：直接走 `save_upload`，沿用 50MB 上限、半成品清理与
`safe_name`，不另起一套并行存储。

语音转写做成可插拔接口 `Transcriber`：平台回调自带 recognition 字段时优先用
平台结果；没有时交给注入的转写器；未配置转写器也**不丢消息**，用固定中文兜底
让 Bot 照常建一轮带音频附件的对话。
"""

from __future__ import annotations

import json
import mimetypes
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import unquote

import httpx

from flux.config import Settings
from flux.core.attachment_store import UploadedFile, safe_name, save_upload
from flux.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "DownloadedMedia",
    "Transcriber",
    "VOICE_ASR_FAILED_TEXT",
    "VOICE_NO_ASR_TEXT",
    "content_type",
    "default_media_name",
    "filename_from_disposition",
    "media_from_response",
    "pick_media_name",
    "safe_error",
    "save_media",
    "transcribe_audio",
]

#: 没有配置语音识别时的固定兜底文案（不静默丢弃语音）
VOICE_NO_ASR_TEXT = "语音已收到，但未配置语音识别，无法转写。"
#: 配置了转写器但调用失败时的兜底文案
VOICE_ASR_FAILED_TEXT = "语音已收到，但语音识别失败，无法转写。"

#: 日志/异常里可能带出的凭据查询参数（企业微信的 access_token 等）
_SECRET_IN_TEXT = re.compile(
    r"((?:access_token|corpsecret|app_?secret|appSecret|token)=)[^&\s\"']+",
    re.IGNORECASE,
)
#: Content-Disposition 里的文件名（优先 RFC 5987 的 filename*）
_FILENAME_STAR = re.compile(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", re.IGNORECASE)
_FILENAME_PLAIN = re.compile(r'filename\s*=\s*"?([^";]+)"?', re.IGNORECASE)


@dataclass(frozen=True)
class DownloadedMedia:
    """一段尚未落盘的平台媒体流。

    `name` 是平台给出的首选文件名（可能为空，例如飞书只返回 Content-Disposition）；
    调用方结合消息体里的文件名与 MIME 决定最终名字。
    """

    name: str
    mime: str
    chunks: AsyncIterator[bytes]


class Transcriber(Protocol):
    """可插拔语音转写接口：测试和部署都可以注入自己的实现。"""

    async def transcribe(self, *, path: str, name: str, mime: str) -> str: ...


def content_type(response: httpx.Response) -> str:
    """HTTP Content-Type 去掉 charset 等参数，空值按 octet-stream 处理。"""
    value = response.headers.get("content-type", "")
    mime = value.split(";", 1)[0].strip().lower()
    return mime or "application/octet-stream"


def filename_from_disposition(value: str | None) -> str:
    """从 Content-Disposition 提取文件名；解析不了就返回空串。"""
    if not value:
        return ""
    match = _FILENAME_STAR.search(value)
    if match:
        return unquote(match.group(1).strip().strip('"'))
    match = _FILENAME_PLAIN.search(value)
    if match:
        return unquote(match.group(1).strip())
    return ""


def default_media_name(*, mime: str, stem: str, ext: str = "") -> str:
    """没有平台文件名时，用 MIME / 平台格式推一个安全名字（如 voice.amr）。"""
    guessed = ""
    if mime and mime != "application/octet-stream":
        guessed = mimetypes.guess_extension(mime) or ""
    if guessed == ".jpe":
        guessed = ".jpg"
    return safe_name(f"{stem}{guessed or ext}")


def pick_media_name(*, preferred: str, downloaded: str, mime: str, stem: str, ext: str = "") -> str:
    """平台消息体里的名字优先，其次 Content-Disposition，最后按 MIME/格式推导。"""
    name = (preferred or "").strip() or (downloaded or "").strip()
    if not name:
        return default_media_name(mime=mime, stem=stem, ext=ext)
    return safe_name(name)


def _bytes_chunks(raw: bytes) -> AsyncIterator[bytes]:
    async def _iter() -> AsyncIterator[bytes]:
        if raw:
            yield raw

    return _iter()


def _stream_response(response: httpx.Response) -> AsyncIterator[bytes]:
    async def _iter() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            await response.aclose()

    return _iter()


async def media_from_response(
    response: httpx.Response,
    *,
    label: str,
    default_name: str = "",
) -> DownloadedMedia:
    """把官方媒体响应包成 DownloadedMedia；JSON 错误体转成清晰的 RuntimeError。

    企业微信 / 飞书的媒体接口在失败时会返回 `{errcode/errmsg}` 或 `{code/msg}`，
    不能当文件落盘。只有 content-type 明确是 JSON、或短文本响应疑似 JSON 时才
    读进内存判断，其余情况保持流式。
    """
    if response.status_code >= 400:
        await response.aclose()
        raise RuntimeError(f"{label}失败：HTTP {response.status_code}")

    mime = content_type(response)
    length = response.headers.get("content-length") or ""
    looks_json = "json" in mime
    if not looks_json and mime.startswith("text/") and length.isdigit():
        # 平台错误体通常很短；长文本仍按文件流处理，避免把大文件读进内存。
        looks_json = int(length) < 4096

    disposition_name = filename_from_disposition(response.headers.get("content-disposition"))
    name = disposition_name or default_name
    if not looks_json:
        return DownloadedMedia(name=name, mime=mime, chunks=_stream_response(response))

    raw = await response.aread()
    await response.aclose()
    data: object = None
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        data = None
    if isinstance(data, dict):
        errcode = data.get("errcode")
        code = data.get("code")
        if errcode not in (None, 0) or code not in (None, 0):
            message = data.get("errmsg") or data.get("msg") or ""
            raise RuntimeError(f"{label}失败：errcode={errcode} code={code} {message}"[:500])
    # 不是错误包：按原始字节落盘（例如用户真的发来一个 JSON 文件）。
    return DownloadedMedia(name=name, mime=mime, chunks=_bytes_chunks(raw))


async def save_media(
    settings: Settings,
    media: DownloadedMedia,
    *,
    cloud_user_id: str | None = None,
) -> UploadedFile:
    """延用手机上传同一条落盘链路（50MB 上限 + 半成品清理 + 按人归属）。"""
    return await save_upload(
        settings,
        name=safe_name(media.name or "file"),
        mime=media.mime,
        chunks=media.chunks,
        cloud_user_id=cloud_user_id,
    )


async def transcribe_audio(
    transcriber: Transcriber | None,
    uploaded: UploadedFile,
    *,
    fallback: str = VOICE_NO_ASR_TEXT,
) -> str:
    """调用可插拔转写器；未配置或失败都回中文兜底，绝不丢这一轮。"""
    if transcriber is None:
        return fallback
    try:
        text = await transcriber.transcribe(
            path=uploaded.path, name=uploaded.name, mime=uploaded.mime
        )
    except Exception as exc:  # noqa: BLE001 - 转写失败降级，不打断对话
        logger.warning("语音转写失败 upload=%s error=%s", uploaded.id, safe_error(exc))
        return VOICE_ASR_FAILED_TEXT
    text = (text or "").strip()
    return text or VOICE_ASR_FAILED_TEXT


def safe_error(exc: BaseException) -> str:
    """异常文本脱敏后再写日志：URL 查询串里可能带 access_token。"""
    return _SECRET_IN_TEXT.sub(r"\1***", str(exc))[:300]
