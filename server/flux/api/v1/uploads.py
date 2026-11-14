"""聊天附件上传与鉴权读取（手机端「+」传文件 / 图片加载器回源走这里）。

上传用裸请求体（`application/octet-stream`）+ 查询参数传文件名，不依赖
python-multipart：文件最大 50MB，分块落盘，请求体超限时直接 4xx 并删掉半成品。

上传先于任务存在——新对话可以先把文件加进来，发送时再把 upload_id 交给任务消息。
每个附件在 `<uploads_dir>/<upload_id>/meta.json` 里记录归属的 Flux 云用户；
`GET /uploads/{upload_id}` 只允许归属人读取，跨用户与不存在同一口径（404），
不确认 id 是否存在。Phase 2 之前没有归属人的老附件，只有在当前用户自己的
任务消息里引用过时才放行（见 `TaskRepository.references_upload`）。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse

from flux.api.auth import get_current_cloud_user_id
from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.core.attachment_store import (
    check_servable,
    resolve_upload,
    safe_content_type,
    save_upload,
    serve_filename,
)
from flux.core.user_scope import normalize_cloud_user_id
from flux.errors import NotFoundError

router = APIRouter(prefix="/uploads", tags=["uploads"])


def _missing() -> NotFoundError:
    """不区分"不存在"与"无权访问"：避免把 upload_id 变成存在性探针。"""
    return NotFoundError("附件不存在或无权访问")


async def _can_read(
    container: Container,
    *,
    upload_id: str,
    owner_cloud_user_id: str | None,
    cloud_user_id: str | None,
) -> bool:
    current = normalize_cloud_user_id(cloud_user_id)
    if current is None:
        return False
    owner = normalize_cloud_user_id(owner_cloud_user_id)
    if owner is not None:
        return owner == current
    # 老附件没有归属人：只在当前用户自己的任务消息引用过时放行。
    return await container.task_repo.references_upload(
        upload_id=upload_id,
        cloud_user_id=current,
        owner_cloud_user_id=container.settings.owner_cloud_user_id,
    )


@router.post("")
async def create_upload(
    request: Request,
    name: str = Query(..., min_length=1, max_length=200, description="原始文件名"),
    mime: str = Query(default="", max_length=200, description="客户端给的 MIME，可留空"),
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    """收一个附件：返回 upload_id 与元数据（不含服务端绝对路径）。"""
    uploaded = await save_upload(
        container.settings,
        name=name,
        mime=mime,
        chunks=request.stream(),
        cloud_user_id=cloud_user_id,
    )
    return ok(uploaded.to_dict())


@router.get("/{upload_id}")
async def get_upload(
    upload_id: str,
    download: bool = Query(default=False, description="true 时按附件下载，否则尽量内联"),
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> FileResponse:
    """按 upload_id 取回附件本体；只有归属人（或老附件的合法引用人）能读。"""
    uploaded = resolve_upload(container.settings, upload_id)
    if uploaded is None or not await _can_read(
        container,
        upload_id=uploaded.id,
        owner_cloud_user_id=uploaded.cloud_user_id,
        cloud_user_id=cloud_user_id,
    ):
        raise _missing()
    # 授权之后再检查大小 / 文件状态：未授权用户看到的永远是 404。
    check_servable(uploaded)
    media_type, inline_safe = safe_content_type(uploaded.name, uploaded.mime)
    disposition = "inline" if inline_safe and not download else "attachment"
    filename = serve_filename(uploaded.name)
    return FileResponse(
        uploaded.path,
        media_type=media_type,
        headers={
            # RFC 5987：中文文件名只走 filename*，不拼进 ASCII 部分。
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename, safe='')}",
            # 不按内容猜类型执行；配合上面的 octet-stream 降级。
            "X-Content-Type-Options": "nosniff",
            # 私有缓存：只允许最终用户的客户端缓存，代理不共享。
            "Cache-Control": "private, max-age=3600",
        },
    )
