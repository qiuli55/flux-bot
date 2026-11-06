"""内容寻址原文库测试（FLUX_CONTEXT_MECHANISM §3.5 / §8.4）。

三条底线：

1. **去重**：同一段原文只落一行，`put` 两次拿到同一个 blob；
2. **无损**：取回来的字节与放进去的**逐字节相同**（含二进制与多字节文本）——
   这是 I2「原文满保真」在存储层的兑现；
3. **压得动**：长文本的实际占用显著小于原文，且 `codec` 记的是**实际**用的编码。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from flux.config import Settings
from flux.core.context import ContentStore
from flux.db.session import create_engine, create_session_factory
from flux.enums import BlobCodec
from flux.errors import NotFoundError


@pytest.fixture()
async def store(settings: Settings, db_schema: None) -> AsyncIterator[ContentStore]:
    """用完显式 dispose：避免 aiosqlite 的连接线程活过事件循环。"""
    engine = create_engine(settings.database_url)
    try:
        yield ContentStore(create_session_factory(engine))
    finally:
        await engine.dispose()


async def test_put_dedupes_by_content(store: ContentStore) -> None:
    payload = b"the same bytes, twice"
    first = await store.put(payload)
    second = await store.put(payload)
    assert first.id == second.id
    assert first.sha256 == second.sha256


async def test_roundtrip_is_lossless(store: ContentStore) -> None:
    payload = ("def handler():\n    return 42\n" * 500).encode()
    blob = await store.put(payload)
    assert blob.size_raw == len(payload)
    assert await store.has(blob.sha256)
    assert await store.get(blob.sha256) == payload


async def test_binary_roundtrip_is_lossless(store: ContentStore) -> None:
    payload = bytes(range(256)) * 100
    blob = await store.put(payload)
    assert await store.get(blob.sha256) == payload


async def test_long_repetitive_text_is_compressed(store: ContentStore) -> None:
    payload = b"pytest tests/test_auth.py " * 2000
    blob = await store.put(payload)
    assert blob.codec in {BlobCodec.ZSTD.value, BlobCodec.LZMA.value}
    assert blob.size_stored < blob.size_raw // 4


async def test_short_payload_stored_raw(store: ContentStore) -> None:
    """过短的输入压缩头开销摊不平，直存 raw——不为了"看起来压过"反而变大。"""
    blob = await store.put(b"hi")
    assert blob.codec == BlobCodec.RAW.value
    assert blob.size_stored == blob.size_raw == 2


async def test_get_missing_blob_raises(store: ContentStore) -> None:
    with pytest.raises(NotFoundError):
        await store.get("0" * 64)
