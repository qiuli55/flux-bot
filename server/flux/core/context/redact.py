"""入站脱敏与加密映射表（FLUX_CONTEXT_MECHANISM §10.1）。

原文全存 Flux 一处，好处是单一真相源，代价是**爆炸半径 = 全部**；而工具输出里夹带
token / key / 连接串 / 内网地址是常态（`env`、`curl -v`、日志、`.npmrc`）。故存档前
先过一层打码：正则 + 熵检测，**零模型依赖**（§13-7：打码不得依赖模型）。

三条契约：

1. **打码是纯函数**（`redact`）：给定文本，返回打码后的文本与一份"占位符 → 原文"清单；
2. **留痕单独加密**（`RedactionStore` 走 `SecretBox`）：换下来的原文进映射表，且单独加密
   ——否则它跟着打码后的正文一起裸奔，打码就白做了；
3. **随会话删除**（§8.2）：映射表本身敏感，删会话时必须一并清掉，故带 `run_id` / `turn_id`。

要点：

- 打码是**单向**的：宁少还原，不多泄露。故熵阈值刻意定在 4.5（纯 hex 上限才 4.0），
  自家 sha256 / 事实脊柱凭证一律不被误伤，而随机 base64（约 5–6）稳稳越线。
- 无密钥（`FLUX_REDACTION_KEY` 留空）时 **fail-closed**：正文照常打码，只是映射**不落库**
  ——最坏后果是"查不到原文"，而不是"明文躺在库里"。
- 占位符只在 **turn 内**唯一，故 `resolve` 必须带 `turn_id`。

入站契约（本文档即调用方要照做的顺序）：``result = redact(text)`` →
``content_store.put(result.text.encode())`` → ``redaction_store.record(...)`` → step 落库。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.core.context.secretbox import SecretBox
from flux.enums import RedactionKind
from flux.models.context import ContextRedaction

#: 首个占位符（同类首见）：`[REDACTED:key]`
_PLACEHOLDER = "[REDACTED:{kind}]"
#: 后续占位符（同类第 N 个）：`[REDACTED:key:2]`
_PLACEHOLDER_N = "[REDACTED:{kind}:{index}]"
#: 短于此长度的候选一律不当密钥（降低误伤）
_MIN_SECRET_CHARS = 32
#: 香农熵阈值（bit/char）。纯 hex 上限 4.0 → 不误伤自家 sha256；随机 base64 约 5–6 → 命中
_MIN_ENTROPY = 4.5
#: 十六进制字符集：整串都是它 → 是哈希/摘要，不是密钥
_HEX_CHARS = frozenset("0123456789abcdefABCDEF")

#: `sk-…` 一类的带前缀密钥
_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b")
#: PEM 私钥 / 证书块（跨行，DOTALL 让 `.` 也吃换行）
_PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----", re.DOTALL)
#: `Bearer …` 一类令牌（含前缀整体替换）
_TOKEN_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._\-+/=]{8,}")
#: `user:pass@host`：只打码认证段，保留 `@host`
_CRED_RE = re.compile(r"(?P<cred>[A-Za-z0-9._%+\-]+:[^\s:@/]+)(?P<host>@[A-Za-z0-9.\-]+(?::\d+)?)")
#: 熵检测候选：无前缀的长 token（hex / base64 / urlsafe）
_CANDIDATE_RE = re.compile(r"[A-Za-z0-9+/=_\-]{32,}")


@dataclass(frozen=True)
class RedactionMatch:
    """一次替换：占位符、类别、被换下来的**原文**（进加密映射表的那份）。"""

    placeholder: str
    kind: RedactionKind
    original: str


@dataclass(frozen=True)
class RedactionResult:
    """打码结果：替换后的文本 + 全部替换清单（顺序即发现顺序）。"""

    text: str
    matches: tuple[RedactionMatch, ...] = field(default=())

    @property
    def changed(self) -> bool:
        return bool(self.matches)


def redact(text: str) -> RedactionResult:
    """把文本里的密钥 / 令牌 / 凭证打码成占位符，返回结果与替换清单（§10.1）。

    替换顺序即优先级：KEY → PEM → TOKEN → CRED → 熵检测兜底。同一类别里的**同一原文**
    复用同一占位符（映射因此始终 1:1）；不同原文按类递增编号。
    """
    matches: list[RedactionMatch] = []
    seen: dict[RedactionKind, dict[str, str]] = {}

    def placeholder_for(kind: RedactionKind, original: str) -> str:
        bucket = seen.setdefault(kind, {})
        existing = bucket.get(original)
        if existing is not None:
            return existing
        index = len(bucket) + 1
        placeholder = (
            _PLACEHOLDER.format(kind=kind.value)
            if index == 1
            else _PLACEHOLDER_N.format(kind=kind.value, index=index)
        )
        bucket[original] = placeholder
        matches.append(RedactionMatch(placeholder=placeholder, kind=kind, original=original))
        return placeholder

    def whole(kind: RedactionKind) -> Callable[[re.Match[str]], str]:
        def _sub(match: re.Match[str]) -> str:
            return placeholder_for(kind, match.group(0))

        return _sub

    def _cred_sub(match: re.Match[str]) -> str:
        placeholder = placeholder_for(RedactionKind.CRED, match.group("cred"))
        return f"{placeholder}{match.group('host')}"

    def _entropy_sub(match: re.Match[str]) -> str:
        token = match.group(0)
        if not _looks_like_secret(token):
            return token
        # 无前缀的密钥归到 KEY（"key"是兜底的密钥标记）
        return placeholder_for(RedactionKind.KEY, token)

    text = _KEY_RE.sub(whole(RedactionKind.KEY), text)
    text = _PEM_RE.sub(whole(RedactionKind.PEM), text)
    text = _TOKEN_RE.sub(whole(RedactionKind.TOKEN), text)
    text = _CRED_RE.sub(_cred_sub, text)
    text = _CANDIDATE_RE.sub(_entropy_sub, text)
    return RedactionResult(text=text, matches=tuple(matches))


def _looks_like_secret(token: str) -> bool:
    """熵检测兜底：够长、非纯数字、非纯 hex，且香农熵越线，才当密钥。"""
    if len(token) < _MIN_SECRET_CHARS:
        return False
    if token.isdigit():
        return False
    # 整串都是十六进制 → 是哈希/摘要（自家 sha256、事实脊柱凭证正在此列），不误伤
    if set(token) <= _HEX_CHARS:
        return False
    return _entropy(token) >= _MIN_ENTROPY


def _entropy(token: str) -> float:
    """香农熵（bit/char）：`-Σ (n/N) log2(n/N)`。"""
    counts = Counter(token)
    total = len(token)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


class RedactionStore:
    """脱敏映射表的加密读写（§10.1）——占位符 → 原文，值单独加密。

    无密钥时不落库（fail-closed）：`enabled` 为假，`record` 返回 0，`resolve` 返回 None。
    删除走 `ContextLifecycle` 的级联（按 `turn_id` / `run_id`），本类不管删除。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        key: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._box = SecretBox(key) if key else None

    @property
    def enabled(self) -> bool:
        """是否配了密钥——没配则映射不落库（正文仍照常打码）。"""
        return self._box is not None

    async def record(self, *, run_id: str, turn_id: str, matches: Sequence[RedactionMatch]) -> int:
        """把这次打码换下来的原文加密落库，返回写入行数；无密钥或空清单返回 0。"""
        box = self._box
        if box is None or not matches:
            return 0
        async with self._session_factory() as db:
            for match in matches:
                db.add(
                    ContextRedaction(
                        run_id=run_id,
                        turn_id=turn_id,
                        kind=match.kind.value,
                        placeholder=match.placeholder,
                        ciphertext=box.seal(match.original.encode("utf-8")),
                    )
                )
            await db.commit()
            return len(matches)

    async def resolve(self, turn_id: str, placeholder: str) -> str | None:
        """回填单个占位符的原文；查不到或未启用返回 None（占位符只在 turn 内唯一）。"""
        box = self._box
        if box is None:
            return None
        async with self._session_factory() as db:
            row = await db.scalar(
                select(ContextRedaction).where(
                    ContextRedaction.turn_id == turn_id,
                    ContextRedaction.placeholder == placeholder,
                )
            )
            if row is None:
                return None
            return box.open(row.ciphertext).decode("utf-8")

    async def mapping_of(self, turn_id: str) -> dict[str, str]:
        """该 turn 的全部「占位符 → 原文」；未启用返回空表。"""
        box = self._box
        if box is None:
            return {}
        async with self._session_factory() as db:
            rows = (
                await db.scalars(
                    select(ContextRedaction).where(ContextRedaction.turn_id == turn_id)
                )
            ).all()
            return {row.placeholder: box.open(row.ciphertext).decode("utf-8") for row in rows}
