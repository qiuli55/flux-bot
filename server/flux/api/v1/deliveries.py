"""AI 下发文件的读取（手机端文件卡片点开预览 / 下载走这里）。

清单与文件本身都由 `delivery_store` 落盘：`GET /deliveries/{id}` 给元数据，
`GET /deliveries/{id}/files/{index}` 直接回文件流——图片/PDF/音视频走内联预览，
其余类型加 `?download=true` 按附件下载（手机端会交给系统应用打开）。

每个 delivery 在 `meta.json` 里记录归属的 Flux 云用户；两个 GET 都只允许归属人
读取。跨用户、未登录与不存在同一口径（404），不确认 id 是否存在，避免把
delivery_id 变成存在性探针。Phase 之前没有归属人的老下发归主用户
（`FLUX_OWNER_CLOUD_USER_ID`），与仓库其它按人隔离的规则一致。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from flux.api.auth import get_current_cloud_user_id
from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.core.delivery_store import Delivery, resolve_delivery
from flux.core.user_scope import owns
from flux.errors import NotFoundError

router = APIRouter(prefix="/deliveries", tags=["deliveries"])


def _missing() -> NotFoundError:
    """不区分"不存在"与"无权访问"：避免把 delivery_id 变成存在性探针。"""
    return NotFoundError("文件不存在或已过期")


def _can_read(container: Container, delivery: Delivery, cloud_user_id: str | None) -> bool:
    """归属判定：本人的下发，或（仅主用户）没有归属人的历史下发。"""
    return owns(
        delivery.cloud_user_id,
        cloud_user_id,
        container.settings.owner_cloud_user_id,
    )


@router.get("/{delivery_id}")
async def get_delivery(
    delivery_id: str,
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> dict[str, object]:
    delivery = resolve_delivery(container.settings, delivery_id)
    if delivery is None or not _can_read(container, delivery, cloud_user_id):
        raise _missing()
    return ok(delivery.to_dict())


@router.get("/{delivery_id}/files/{index}")
async def get_delivery_file(
    delivery_id: str,
    index: int,
    download: bool = Query(default=False, description="true 时按附件下载，否则尽量内联"),
    container: Container = Depends(get_container),
    cloud_user_id: str | None = Depends(get_current_cloud_user_id),
) -> FileResponse:
    delivery = resolve_delivery(container.settings, delivery_id)
    if delivery is None or not _can_read(container, delivery, cloud_user_id):
        raise _missing()
    item = next((entry for entry in delivery.files if entry.index == index), None)
    if item is None:
        raise _missing()
    disposition = "attachment" if download else "inline"
    return FileResponse(
        item.path,
        media_type=item.mime or "application/octet-stream",
        headers={
            # RFC 5987：中文文件名走 filename*，不拼进 ASCII 部分
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(item.name)}",
            # 不按内容猜类型执行（与上传读取保持同一口径）。
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=3600",
        },
    )
