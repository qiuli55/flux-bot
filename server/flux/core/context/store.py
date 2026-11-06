"""内容寻址原文库（FLUX_CONTEXT_MECHANISM §3.5 / §8.4）。

同一段原文只存一份：`put()` 先算 sha256，命中即复用，未命中才落库并压缩。
去重与压缩都**不改动原文**，取出时逐字节可还原——I3 说的"不压缩"是语义层的，
与这里的存储层编码不冲突。

编码选择（`codec` 列记下**实际**用的，解码一律按它取值，不看长度猜）：

- 优先 `zstandard`（文本 3–8×，日志 10×+）；
- 环境缺 `zstandard` 时退到标准库 `lzma`（同属无损）；
- 原文过短或压不动（压缩后不减反增）则直存 `raw`。
"""

from __future__ import annotations

import hashlib
import lzma
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.enums import BlobCodec
from flux.errors import NotFoundError
from flux.models.context import ContextBlob

try:  # pragma: no cover - 取决于部署环境是否装了可选依赖
    import zstandard as _zstd
except ImportError:  # pragma: no cover
    _zstd = None

#: 小于此字节数不值得压缩（压缩头开销摊不平，越压越大）
_MIN_COMPRESS_BYTES = 64
#: zstd 级别：3 是速度与压缩率的默认折中
_ZSTD_LEVEL = 3


class ContentStore:
    """内容寻址原文库：`put` 去重落库、`get` 按 sha256 取回原文。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def put(self, data: bytes) -> ContextBlob:
        """把一段原文放进库，返回它的 blob 行（同一段原文永远只存一份）。"""
        digest = hashlib.sha256(data).hexdigest()
        async with self._session_factory() as db:
            existing = await self._find(db, digest)
            if existing is not None:
                return existing

            codec, stored = self._encode(data)
            blob = ContextBlob(
                id=uuid.uuid4(),
                sha256=digest,
                codec=codec.value,
                size_raw=len(data),
                size_stored=len(stored),
                data=stored,
            )
            db.add(blob)
            try:
                await db.commit()
            except IntegrityError:
                # 并发调用（多个 MCP 请求同时写同一段原文）撞上唯一索引：
                # 别人已经写进去了，直接把它读回来即可，语义上完全等价。
                await db.rollback()
                winner = await self._find(db, digest)
                if winner is None:  # pragma: no cover - 唯一索引冲突后不该读不到
                    raise
                return winner
            return blob

    async def get(self, sha256: str) -> bytes:
        """按 sha256 取回原文；不存在则报错。"""
        async with self._session_factory() as db:
            blob = await self._find(db, sha256)
            if blob is None:
                raise NotFoundError(f"原文 {sha256} 不在内容寻址库中", details={"sha256": sha256})
            return self._decode(blob.codec, blob.data)

    async def has(self, sha256: str) -> bool:
        async with self._session_factory() as db:
            return await self._find(db, sha256) is not None

    @staticmethod
    async def _find(db: AsyncSession, sha256: str) -> ContextBlob | None:
        return await db.scalar(select(ContextBlob).where(ContextBlob.sha256 == sha256))

    @staticmethod
    def _encode(data: bytes) -> tuple[BlobCodec, bytes]:
        if len(data) < _MIN_COMPRESS_BYTES:
            return BlobCodec.RAW, data
        if _zstd is not None:
            compressed = _zstd.ZstdCompressor(level=_ZSTD_LEVEL).compress(data)
            if len(compressed) < len(data):
                return BlobCodec.ZSTD, compressed
            return BlobCodec.RAW, data
        compressed = lzma.compress(data, format=lzma.FORMAT_XZ, preset=6)
        if len(compressed) < len(data):
            return BlobCodec.LZMA, compressed
        return BlobCodec.RAW, data

    @staticmethod
    def _decode(codec: str, stored: bytes) -> bytes:
        if codec == BlobCodec.ZSTD.value:
            if _zstd is None:  # pragma: no cover - 只在换了环境读旧库时触发
                raise RuntimeError("库中存在 zstd 编码的 blob，但当前环境缺少 zstandard")
            return _zstd.ZstdDecompressor().decompress(stored)
        if codec == BlobCodec.LZMA.value:
            return lzma.decompress(stored)
        return stored
