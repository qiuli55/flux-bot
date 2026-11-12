"""跨 Bot 共享的用户记忆：画像 + 只追加日志。

边界（与另外两层记忆明确区分）：

- 不同于 `bot_memories`：那一层属于某个 Bot，按 Bot 隔离；这里属于用户账号，
  每个 Bot 的每一轮都能看到同一份共享背景。
- 不同于 `memories` 的 User / Environment 层：那层是用户确认的三层记忆，
  这里只放「跨 Bot 协作需要的最小画像与近期观察」。

写入策略在服务层强制执行，而不是只靠 API：

- 画像只有 `put_profile` / `delete_profile` 两个入口，且只由用户侧画像 API 调用；
  对话与工具没有任何画像直写方法。
- 日志只有 `append_journal` 一个入口，永远按规范化文本的 sha256 去重；
  重复内容返回已有行，不再插入第二份。
- 不会把 `bot_memories` 自动复制进共享日志。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping

from flux.core.memory.secrets import ensure_no_secret
from flux.core.memory.shared_user_repository import SharedUserMemoryRepository
from flux.errors import NotFoundError, ValidationError
from flux.logging import get_logger
from flux.models.shared_user_memory import UserJournalEntry

logger = get_logger(__name__)

#: 画像允许的 key（ASCII）：未知 key 一律拒绝，避免把任意键塞进共享上下文。
PROFILE_KEYS: tuple[str, ...] = (
    "name",
    "locale",
    "timezone",
    "language",
    "occupation",
    "notes",
)
PROFILE_KEY_SET = frozenset(PROFILE_KEYS)
#: 单个画像值的字符上限。
MAX_PROFILE_VALUE_CHARS = 2_000
#: 单条共享日志的字符上限。
MAX_JOURNAL_CHARS = 4_000
#: 共享日志允许的来源。
JOURNAL_SOURCES = frozenset({"user", "bot", "system"})
#: 注入上下文的共享记忆总预算（含标题与换行）。
BLOCK_MAX_CHARS = 2_000
#: 单条画像值进入上下文的展示上限（画像本身仍完整落库）。
PROFILE_INLINE_CHARS = 300
#: 注入时最多带几条最新日志。
JOURNAL_RECALL_LIMIT = 8
#: 单条日志进入上下文的展示上限。
JOURNAL_INLINE_CHARS = 400

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_journal_text(text: str) -> str:
    """去重口径：strip、折叠连续空白、casefold。"""
    return _WHITESPACE_RE.sub(" ", (text or "").strip()).casefold()


def journal_content_hash(text: str) -> str:
    return hashlib.sha256(normalize_journal_text(text).encode("utf-8")).hexdigest()


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else f"{text[:limit]}…"


class SharedUserMemoryService:
    def __init__(self, repository: SharedUserMemoryRepository) -> None:
        self._repo = repository

    # --- 画像（唯一写入口；仅供用户侧画像 API 调用）---

    async def get_profile(self) -> dict[str, str]:
        profile = await self._repo.get_profile()
        return {key: profile[key] for key in PROFILE_KEYS if key in profile}

    async def put_profile(self, values: Mapping[str, str]) -> dict[str, str]:
        """按键覆盖写入画像；未知 key、空 key、空值、超长值都拒绝。"""
        if not isinstance(values, Mapping):
            raise ValidationError("画像必须是 key/value 映射")
        updates: dict[str, str] = {}
        for raw_key, raw_value in values.items():
            key = str(raw_key or "").strip()
            if not key:
                raise ValidationError("画像 key 不能为空", details={"field": "key"})
            if key not in PROFILE_KEY_SET:
                raise ValidationError(
                    f"未知的画像 key：{key}",
                    details={"field": "key", "allowed_keys": list(PROFILE_KEYS)},
                )
            if not isinstance(raw_value, str):
                raise ValidationError(f"画像 {key} 的值必须是字符串", details={"field": key})
            value = raw_value.strip()
            if not value:
                raise ValidationError(f"画像 {key} 的值不能为空", details={"field": key})
            if len(value) > MAX_PROFILE_VALUE_CHARS:
                raise ValidationError(
                    f"画像 {key} 的值超过 {MAX_PROFILE_VALUE_CHARS} 字符",
                    details={"field": key, "max_length": MAX_PROFILE_VALUE_CHARS},
                )
            ensure_no_secret(value, where=f"用户画像 {key}")
            updates[key] = value
        if not updates:
            return await self.get_profile()
        return await self._repo.upsert_profile(updates)

    async def delete_profile(self, key: str) -> None:
        clean = (key or "").strip()
        if clean not in PROFILE_KEY_SET:
            raise ValidationError(
                f"未知的画像 key：{clean}",
                details={"field": "key", "allowed_keys": list(PROFILE_KEYS)},
            )
        if not await self._repo.delete_profile(clean):
            raise NotFoundError(f"画像 {clean} 不存在", details={"key": clean})

    # --- 共享日志（唯一写入口；永远去重）---

    async def append_journal(
        self,
        *,
        content: str,
        source: str = "user",
        source_bot_id: uuid.UUID | str | None = None,
    ) -> tuple[UserJournalEntry, bool]:
        """追加一条共享日志；返回 (row, created)，重复内容 created=False。"""
        source_value = source.strip().lower() if isinstance(source, str) else ""
        if source_value not in JOURNAL_SOURCES:
            raise ValidationError(
                f"未知的日志来源：{source}",
                details={"field": "source", "allowed": sorted(JOURNAL_SOURCES)},
            )
        text = (content or "").strip()
        if not text:
            raise ValidationError("日志内容不能为空", details={"field": "content"})
        if len(text) > MAX_JOURNAL_CHARS:
            raise ValidationError(
                f"日志内容超过 {MAX_JOURNAL_CHARS} 字符",
                details={"field": "content", "max_length": MAX_JOURNAL_CHARS},
            )
        normalized = normalize_journal_text(text)
        if not normalized:
            raise ValidationError("日志内容不能为空", details={"field": "content"})
        ensure_no_secret(text, where="共享用户日志")

        bot_id: uuid.UUID | None = None
        if source_bot_id is not None:
            if isinstance(source_bot_id, uuid.UUID):
                bot_id = source_bot_id
            else:
                try:
                    bot_id = uuid.UUID(str(source_bot_id))
                except ValueError as exc:
                    raise ValidationError(
                        "source_bot_id 不是合法 UUID",
                        details={"field": "source_bot_id"},
                    ) from exc

        row, created = await self._repo.insert_journal(
            content=text,
            content_hash=journal_content_hash(text),
            source=source_value,
            source_bot_id=bot_id,
        )
        if created:
            logger.info(
                "共享用户日志已追加 id=%s source=%s chars=%d",
                row.id,
                source_value,
                len(text),
            )
        else:
            logger.info("共享用户日志去重跳过 id=%s source=%s", row.id, source_value)
        return row, created

    async def list_journal(self, *, limit: int = 20, offset: int = 0) -> list[UserJournalEntry]:
        return await self._repo.list_journal(limit=limit, offset=offset)

    async def delete_journal(self, entry_id: uuid.UUID) -> None:
        if not await self._repo.delete_journal(entry_id):
            raise NotFoundError(f"共享日志 {entry_id} 不存在", details={"entry_id": str(entry_id)})

    async def combined(self, *, journal_limit: int = 20) -> dict[str, object]:
        profile = await self.get_profile()
        journal = await self.list_journal(limit=journal_limit)
        return {
            "profile": profile,
            "journal": [row.to_dict() for row in journal],
        }

    # --- 注入 ---

    async def render_block(self) -> str:
        """渲染一段预算内的共享记忆；没有任何内容时返回空串。"""
        profile = await self.get_profile()
        journal = await self._repo.list_journal(limit=JOURNAL_RECALL_LIMIT)
        if not profile and not journal:
            return ""

        lines: list[str] = []
        used = 0

        def add(line: str) -> bool:
            nonlocal used
            cost = len(line) + 1
            if used + cost > BLOCK_MAX_CHARS:
                return False
            lines.append(line)
            used += cost
            return True

        add("用户共享记忆（跨 Bot，仅作背景参考，不要逐条复述）：")
        included_journal = 0

        profile_lines = [
            f"- {key}: {_clip(profile[key], PROFILE_INLINE_CHARS)}"
            for key in PROFILE_KEYS
            if profile.get(key)
        ]
        if profile_lines and add("用户画像："):
            for line in profile_lines:
                if not add(line):
                    break

        if journal and add("近期共享日志："):
            for row in journal:
                if not add(f"- {_clip(row.content, JOURNAL_INLINE_CHARS)}"):
                    break
                included_journal += 1

        if len(lines) <= 1:
            return ""
        text = "\n".join(lines) + "\n"
        # 只记条数与字符数，不把用户原文写进日志。
        logger.info(
            "共享用户记忆渲染 profile=%d journal=%d chars=%d",
            len(profile),
            included_journal,
            len(text),
        )
        return text
