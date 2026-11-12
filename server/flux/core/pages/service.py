"""制品页面的业务规则。

职责边界：

- 校验与规范化在这里强制执行（标题长度、正文字节上限、内容类型、slug 字符集），
  不只靠 API 层的 pydantic 模型——其它调用方（未来的 Bot 工具、批量脚本）也必须
  走同一套规则。
- 版本只追加：`update` 永远新增一行 revision，旧行不改；`current_revision` 只增。
- 公开读取只认 `share_token` 且只放行 published；draft / unpublished / 未知令牌
  统一 404，不区分原因，避免泄露页面是否存在。
- 日志只记 id / slug / token / 版本 / 字节数，不记正文。
"""

from __future__ import annotations

import re
import secrets
import uuid

from flux.core.pages.repository import ArtifactPageRepository
from flux.enums import ArtifactPageStatus
from flux.errors import NotFoundError, ValidationError
from flux.logging import get_logger
from flux.models.artifact_page import ArtifactPage, ArtifactPageRevision

logger = get_logger(__name__)

#: 单页正文上限（按 UTF-8 字节计）。超过就不落库，避免公开链接变成文件托管。
MAX_BODY_BYTES = 256 * 1024
#: 标题字符上限（数据库列同为 200）。
MAX_TITLE_CHARS = 200
#: slug 字符上限（数据库列 64）。
MAX_SLUG_CHARS = 64
#: slug 只允许小写字母、数字与连字符，且以字母或数字开头。
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

#: 允许的内容类型（存库用规范 MIME 字符串）。
CONTENT_TYPE_MARKDOWN = "text/markdown"
CONTENT_TYPE_HTML = "text/html"
CONTENT_TYPES = frozenset({CONTENT_TYPE_MARKDOWN, CONTENT_TYPE_HTML})
#: 便捷别名：Bot 写 "markdown" / "html" 也接受，落库统一成 MIME。
_CONTENT_TYPE_ALIASES = {
    "markdown": CONTENT_TYPE_MARKDOWN,
    "md": CONTENT_TYPE_MARKDOWN,
    "text": CONTENT_TYPE_MARKDOWN,
    "text/markdown": CONTENT_TYPE_MARKDOWN,
    "html": CONTENT_TYPE_HTML,
    "text/html": CONTENT_TYPE_HTML,
}

#: 公开令牌长度：24 字节随机数（token_urlsafe 后 32 字符），不可猜测。
_SHARE_TOKEN_BYTES = 24


def normalize_content_type(value: str | None) -> str:
    """把别名收敛成规范 MIME；未知类型直接 422。"""
    if value is None:
        return CONTENT_TYPE_MARKDOWN
    key = str(value).strip().lower()
    normalized = _CONTENT_TYPE_ALIASES.get(key)
    if normalized is None:
        raise ValidationError(
            f"不支持的内容类型：{value}",
            details={"field": "content_type", "allowed": sorted(CONTENT_TYPES)},
        )
    return normalized


def _new_share_token() -> str:
    return secrets.token_urlsafe(_SHARE_TOKEN_BYTES)


def _new_slug() -> str:
    return f"page-{secrets.token_hex(6)}"


class ArtifactPageService:
    def __init__(self, repository: ArtifactPageRepository) -> None:
        self._repo = repository

    # --- 校验 ---

    @staticmethod
    def _clean_title(value: str | None) -> str:
        title = (value or "").strip()
        if not title:
            raise ValidationError("标题不能为空", details={"field": "title"})
        if len(title) > MAX_TITLE_CHARS:
            raise ValidationError(
                f"标题超过 {MAX_TITLE_CHARS} 字符",
                details={"field": "title", "max_length": MAX_TITLE_CHARS},
            )
        return title

    @staticmethod
    def _clean_body(value: str | None) -> str:
        if not isinstance(value, str):
            raise ValidationError("正文必须是字符串", details={"field": "body"})
        size = len(value.encode("utf-8"))
        if size > MAX_BODY_BYTES:
            raise ValidationError(
                f"正文超过 {MAX_BODY_BYTES} 字节",
                details={"field": "body", "max_bytes": MAX_BODY_BYTES, "bytes": size},
            )
        return value

    @staticmethod
    def _clean_slug(value: str | None) -> str:
        if value is None:
            return _new_slug()
        slug = str(value).strip().lower()
        if not SLUG_PATTERN.match(slug):
            raise ValidationError(
                "slug 只能包含小写字母、数字与连字符，且以字母或数字开头",
                details={"field": "slug", "max_length": MAX_SLUG_CHARS},
            )
        return slug

    @staticmethod
    def _clean_status(value: str | None) -> str | None:
        if value is None:
            return None
        status = str(value).strip().lower()
        allowed = {item.value for item in ArtifactPageStatus}
        if status not in allowed:
            raise ValidationError(
                f"未知的页面状态：{value}",
                details={"field": "status", "allowed": sorted(allowed)},
            )
        return status

    # --- 管理面 ---

    async def create(
        self,
        *,
        title: str,
        body: str,
        content_type: str | None = None,
        bot_id: uuid.UUID | None = None,
        slug: str | None = None,
        publish: bool = False,
    ) -> ArtifactPage:
        clean_title = self._clean_title(title)
        clean_body = self._clean_body(body)
        clean_type = normalize_content_type(content_type)
        if bot_id is not None and not await self._repo.bot_exists(bot_id):
            raise NotFoundError(f"Bot {bot_id} 不存在", details={"bot_id": str(bot_id)})
        status = ArtifactPageStatus.PUBLISHED.value if publish else ArtifactPageStatus.DRAFT.value
        page = await self._repo.create(
            title=clean_title,
            body=clean_body,
            content_type=clean_type,
            slug=self._clean_slug(slug),
            share_token=_new_share_token(),
            bot_id=bot_id,
            status=status,
        )
        logger.info(
            "创建制品页面 page=%s slug=%s revision=%s bytes=%s status=%s",
            page.id,
            page.slug,
            page.current_revision,
            len(clean_body.encode("utf-8")),
            page.status,
        )
        return page

    async def list(
        self,
        *,
        bot_id: uuid.UUID | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ArtifactPage]:
        return await self._repo.list(
            bot_id=bot_id,
            status=self._clean_status(status),
            limit=limit,
            offset=offset,
        )

    async def require(self, page_id: uuid.UUID) -> ArtifactPage:
        page = await self._repo.get(page_id)
        if page is None:
            raise NotFoundError(f"页面 {page_id} 不存在", details={"page_id": str(page_id)})
        return page

    async def detail(self, page_id: uuid.UUID) -> dict[str, object]:
        """详情 = 页面元数据 + 当前版本正文。"""
        page, revision = await self.current(page_id)
        data: dict[str, object] = page.to_dict()
        data["revision"] = revision.revision
        data["body"] = revision.body
        data["body_bytes"] = revision.body_bytes()
        return data

    async def current(self, page_id: uuid.UUID) -> tuple[ArtifactPage, ArtifactPageRevision]:
        page = await self.require(page_id)
        revision = await self._repo.get_revision(page.id, page.current_revision)
        if revision is None:
            raise NotFoundError("页面没有可用版本", details={"page_id": str(page_id)})
        return page, revision

    async def update(
        self,
        page_id: uuid.UUID,
        *,
        title: str | None = None,
        body: str | None = None,
        content_type: str | None = None,
    ) -> ArtifactPage:
        """保存新版本。字段都没给或内容完全没变时不动版本号。"""
        page, current = await self.current(page_id)
        clean_title = self._clean_title(title) if title is not None else None
        clean_body = self._clean_body(body) if body is not None else None
        clean_type = normalize_content_type(content_type) if content_type is not None else None

        merged_title = clean_title if clean_title is not None else page.title
        merged_body = clean_body if clean_body is not None else current.body
        merged_type = clean_type if clean_type is not None else page.content_type
        if (
            merged_title == page.title
            and merged_body == current.body
            and merged_type == page.content_type
        ):
            logger.info(
                "制品页面内容未变化，不新增版本 page=%s slug=%s revision=%s",
                page.id,
                page.slug,
                page.current_revision,
            )
            return page

        updated, revision = await self._repo.add_revision(
            page.id,
            title=merged_title,
            body=merged_body,
            content_type=merged_type,
            author_bot_id=page.bot_id,
        )
        logger.info(
            "新增制品页面版本 page=%s slug=%s revision=%s bytes=%s",
            updated.id,
            updated.slug,
            revision.revision,
            revision.body_bytes(),
        )
        return updated

    async def publish(self, page_id: uuid.UUID) -> ArtifactPage:
        page, _ = await self.current(page_id)
        updated = await self._repo.set_status(page.id, ArtifactPageStatus.PUBLISHED.value)
        logger.info("发布制品页面 page=%s slug=%s", updated.id, updated.slug)
        return updated

    async def unpublish(self, page_id: uuid.UUID) -> ArtifactPage:
        page = await self.require(page_id)
        updated = await self._repo.set_status(page.id, ArtifactPageStatus.UNPUBLISHED.value)
        logger.info("下线制品页面 page=%s slug=%s", updated.id, updated.slug)
        return updated

    async def list_revisions(self, page_id: uuid.UUID) -> list[ArtifactPageRevision]:
        page = await self.require(page_id)
        return await self._repo.list_revisions(page.id)

    async def revision(self, page_id: uuid.UUID, revision: int) -> ArtifactPageRevision:
        page = await self.require(page_id)
        row = await self._repo.get_revision(page.id, revision)
        if row is None:
            raise NotFoundError(
                f"页面 {page_id} 没有第 {revision} 版",
                details={"page_id": str(page_id), "revision": revision},
            )
        return row

    async def delete(self, page_id: uuid.UUID) -> None:
        if not await self._repo.delete(page_id):
            raise NotFoundError(f"页面 {page_id} 不存在", details={"page_id": str(page_id)})
        logger.info("删除制品页面 page=%s", page_id)

    # --- 公开面 ---

    async def public_page(self, share_token: str) -> tuple[ArtifactPage, ArtifactPageRevision]:
        """公开分享：只有 published 可读，其余情况统一 404。"""
        token = (share_token or "").strip()
        page = await self._repo.get_by_token(token) if token else None
        if page is None or page.status != ArtifactPageStatus.PUBLISHED.value:
            raise NotFoundError("页面不存在", details={"share_token": token})
        revision = await self._repo.get_revision(page.id, page.current_revision)
        if revision is None:
            raise NotFoundError("页面不存在", details={"share_token": token})
        logger.info(
            "公开读取制品页面 page=%s token=%s revision=%s bytes=%s",
            page.id,
            token,
            revision.revision,
            revision.body_bytes(),
        )
        return page, revision
