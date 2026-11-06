"""聊天附件上传（手机端「+」传文件走这里）。

用裸请求体（`application/octet-stream`）+ 查询参数传文件名，不依赖 python-multipart：
文件最大 50MB，分块落盘，请求体超限时直接 4xx 并删掉半成品。

上传先于任务存在——新对话可以先把文件加进来，发送时再把 upload_id 交给任务消息。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.core.attachment_store import save_upload

router = APIRouter(prefix="/uploads", tags=["uploads"])


@router.post("")
async def create_upload(
    request: Request,
    name: str = Query(..., min_length=1, max_length=200, description="原始文件名"),
    mime: str = Query(default="", max_length=200, description="客户端给的 MIME，可留空"),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """收一个附件：返回 upload_id 与给 Agent 读的绝对路径。"""
    uploaded = await save_upload(container.settings, name=name, mime=mime, chunks=request.stream())
    return ok(uploaded.to_dict())
