"""脱敏映射表的值加密（FLUX_CONTEXT_MECHANISM §10.1）。

映射表要**单独加密**：它存的正是"从正文里换下来的原文"，一旦跟着打码后的正文一起
裸奔，打码就白做了。这里用标准库自己拼一个够用且可审计的信封，不引入 `cryptography`
（部署环境不保证装了它；本仓库对可选依赖一律走降级，先例见 `core/context/store.py`）：

- 密钥派生：`FLUX_REDACTION_KEY` → `sha256`，再用 HMAC 带不同标签派生 `k_enc` / `k_mac`；
- 加密：HMAC-SHA256 当 PRF 生成 CTR 密钥流，与明文异或（流密码，无填充、长度不变）；
- 完整性：encrypt-then-MAC——先算 HMAC-SHA256 覆盖 `magic || nonce || 密文`，再拼接。

信封布局：`b"FLUX1" || nonce(16) || ciphertext || tag(32)`。`open` 先验签再解密，
标签不符即抛错——**篡改密文或换错密钥都不会得到一段"看着还行"的明文**。
"""

from __future__ import annotations

import hashlib
import hmac
import os

_MAGIC = b"FLUX1"
_NONCE_BYTES = 16
_TAG_BYTES = 32
#: SHA-256 摘要长度，也是密钥流每次推进的字节数
_BLOCK_BYTES = 32
_ENC_LABEL = b"flux-redaction-enc"
_MAC_LABEL = b"flux-redaction-mac"


class SecretBox:
    """一个密钥的信封加解密；密钥为空即拒绝构造（调用方据此决定 fail-closed）。"""

    def __init__(self, key: str) -> None:
        if not key:
            raise ValueError("SecretBox 需要一个非空密钥")
        material = hashlib.sha256(key.encode("utf-8")).digest()
        self._k_enc = hmac.new(material, _ENC_LABEL, hashlib.sha256).digest()
        self._k_mac = hmac.new(material, _MAC_LABEL, hashlib.sha256).digest()

    def seal(self, plaintext: bytes, *, nonce: bytes | None = None) -> bytes:
        """加密 + 认证。`nonce` 只给测试用；生产一律随机（同一密钥下重放 nonce 会泄密钥流）。"""
        nonce = os.urandom(_NONCE_BYTES) if nonce is None else nonce
        if len(nonce) != _NONCE_BYTES:
            raise ValueError(f"nonce 必须是 {_NONCE_BYTES} 字节")
        body = _MAGIC + nonce + _keystream(self._k_enc, nonce, plaintext)
        return body + _tag(self._k_mac, body)

    def open(self, sealed: bytes) -> bytes:
        """验签 + 解密；信封不合法或标签不符一律抛 `ValueError`。"""
        if len(sealed) < len(_MAGIC) + _NONCE_BYTES + _TAG_BYTES:
            raise ValueError("密文长度不足以构成信封")
        body, tag = sealed[:-_TAG_BYTES], sealed[-_TAG_BYTES:]
        if not body.startswith(_MAGIC):
            raise ValueError("密文信封头不认识")
        if not hmac.compare_digest(_tag(self._k_mac, body), tag):
            raise ValueError("密文认证失败（被篡改或密钥不对）")
        start = len(_MAGIC) + _NONCE_BYTES
        return _keystream(self._k_enc, body[len(_MAGIC) : start], body[start:])


def _keystream(key: bytes, nonce: bytes, data: bytes) -> bytes:
    """HMAC-SHA256 作 PRF 的 CTR 密钥流；加解密是同一个函数（异或对称）。"""
    out = bytearray()
    for counter, offset in enumerate(range(0, len(data), _BLOCK_BYTES)):
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        chunk = data[offset : offset + _BLOCK_BYTES]
        out.extend(byte ^ mask for byte, mask in zip(chunk, block, strict=False))
    return bytes(out)


def _tag(key: bytes, body: bytes) -> bytes:
    return hmac.new(key, body, hashlib.sha256).digest()
