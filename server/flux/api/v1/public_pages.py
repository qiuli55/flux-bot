"""制品页面的公开读取入口（不经过 REST 鉴权）。

分享链接给的是没有本机登录态的人，因此这里只按不可猜测的 `share_token` 寻址：

- 只有 published 页面可读；draft / unpublished / 未知令牌统一 404，不暴露存在性。
- markdown 以 `text/plain` 原样返回（浏览器不会把它当 HTML 执行）。
- html 以 `text/html` 返回，并带保守 CSP：禁止脚本、禁止远程资源（图片除外），
  避免把公开页面变成脚本托管平台。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from flux.api.deps import get_container
from flux.container import Container
from flux.core.pages.service import CONTENT_TYPE_HTML

router = APIRouter(prefix="/public/pages", tags=["public-pages"])

#: HTML 页面的内容安全策略：默认全禁；只放行 https/data 图片与内联样式。
HTML_PAGE_CSP = "default-src 'none'; img-src https: data:; style-src 'unsafe-inline'"


@router.get("/{share_token}", response_class=Response)
async def read_public_page(
    share_token: str,
    container: Container = Depends(get_container),
) -> Response:
    """按分享令牌读取已发布页面；未发布或不存在都是 404。"""
    page, revision = await container.pages.public_page(share_token)

    if revision.content_type == CONTENT_TYPE_HTML:
        response: Response = HTMLResponse(revision.body)
        suffix = "html"
    else:
        # markdown 不渲染成 HTML：原文按 text/plain 返回，正文里的标签只是文字。
        response = PlainTextResponse(revision.body)
        suffix = "md"

    response.headers["Content-Security-Policy"] = HTML_PAGE_CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Disposition"] = f'inline; filename="{page.slug}.{suffix}"'
    response.headers["ETag"] = f'"{page.id}-r{revision.revision}"'
    response.headers["X-Artifact-Page-Revision"] = str(revision.revision)
    response.headers["Cache-Control"] = "no-cache"
    return response
