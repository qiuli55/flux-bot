"""AI 下发文件的读取（手机端文件卡片点开预览 / 下载走这里）。

清单与文件本身都由 `delivery_store` 落盘：`GET /deliveries/{id}` 给元数据，
`GET /deliveries/{id}/files/{index}` 直接回文件流——图片/PDF/音视频走内联预览，
其余类型加 `?download=true` 按附件下载（手机端会交给系统应用打开）。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.core.delivery_store import resolve_delivery, resolve_delivery_file
from flux.errors import NotFoundError

router = APIRouter(prefix="/deliveries", tags=["deliveries"])


def _missing(delivery_id: str) -> NotFoundError:
    return NotFoundError("文件不存在或已过期", details={"delivery_id": delivery_id})


@router.get("/{delivery_id}")
async def get_delivery(
    delivery_id: str,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    delivery = resolve_delivery(container.settings, delivery_id)
    if delivery is None:
        raise _missing(delivery_id)
    return ok(delivery.to_dict())


@router.get("/{delivery_id}/files/{index}")
async def get_delivery_file(
    delivery_id: str,
    index: int,
    download: bool = Query(default=False, description="true 时按附件下载，否则尽量内联"),
    container: Container = Depends(get_container),
) -> FileResponse:
    item = resolve_delivery_file(container.settings, delivery_id, index)
    if item is None:
        raise _missing(delivery_id)
    disposition = "attachment" if download else "inline"
    return FileResponse(
        item.path,
        media_type=item.mime or "application/octet-stream",
        headers={
            # RFC 5987：中文文件名走 filename*，不拼进 ASCII 部分
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(item.name)}",
            "Cache-Control": "private, max-age=3600",
        },
    )
