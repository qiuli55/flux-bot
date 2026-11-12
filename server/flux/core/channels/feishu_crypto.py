"""飞书事件回调的共享加解密工具。

飞书开放平台在配置了 Encrypt Key 后会这样投递事件：

- 请求体是 ``{"encrypt": "<base64>"}``；
- 解密密钥不是 Encrypt Key 本身，而是 ``sha256(Encrypt Key)`` 得到的 32 字节；
- base64 解码后前 16 字节是 IV，其余是 AES-256-CBC 密文，明文带 PKCS7 填充；
- 解密后再按普通 JSON 处理。

签名规则（配置了 Encrypt Key 时启用）：对
``X-Lark-Request-Timestamp + X-Lark-Request-Nonce + Encrypt Key + 原始请求体``
求 SHA-256，与 ``X-Lark-Signature`` 比较。原始请求体必须是收到的字节，
不能是重新序列化后的 JSON——字段顺序或空格不同都会导致签名对不上。

这里只做纯函数，不读配置、不发请求，便于 Supervisor 与正式 Bot 频道共用。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from typing import Any

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

__all__ = [
    "FeishuCryptoError",
    "compute_signature",
    "decrypt_payload",
    "encrypt_payload",
    "verify_signature",
]

#: CBC 分组长度（AES 固定 16 字节，也是 IV 的长度）
_BLOCK_BYTES = 16
#: PKCS7 填充位数（以 bit 计）
_PADDING_BITS = _BLOCK_BYTES * 8


class FeishuCryptoError(ValueError):
    """回调加解密/签名数据不合法；调用方据此 fail-closed。"""


def decrypt_payload(encrypt_key: str, encrypted: str) -> dict[str, Any]:
    """按飞书约定解密事件体，返回解析后的 JSON 对象。

    任何一步不合法（空密钥、非法 base64、长度不对、填充错误、不是 JSON 对象）
    都抛 `FeishuCryptoError`，绝不返回一段"看着还行"的内容。
    """
    key = (encrypt_key or "").strip()
    if not key:
        raise FeishuCryptoError("解密飞书回调需要非空的 Encrypt Key")
    if not isinstance(encrypted, str) or not encrypted:
        raise FeishuCryptoError("加密回调缺少 encrypt 字段")

    try:
        blob = base64.b64decode(encrypted, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise FeishuCryptoError("飞书加密回调不是合法 base64") from exc
    if len(blob) <= _BLOCK_BYTES or len(blob) % _BLOCK_BYTES != 0:
        raise FeishuCryptoError("飞书加密回调长度不合法")

    iv, ciphertext = blob[:_BLOCK_BYTES], blob[_BLOCK_BYTES:]
    aes_key = hashlib.sha256(key.encode("utf-8")).digest()
    decryptor = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()

    unpadder = padding.PKCS7(_PADDING_BITS).unpadder()
    try:
        plaintext = unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        raise FeishuCryptoError("飞书加密回调填充不合法") from exc

    try:
        data = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FeishuCryptoError("飞书加密回调明文不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise FeishuCryptoError("飞书加密回调明文不是 JSON 对象")
    return data


def encrypt_payload(encrypt_key: str, payload: dict[str, Any]) -> str:
    """按同一套算法加密（测试与本地联调用），返回 base64 字符串。"""
    key = (encrypt_key or "").strip()
    if not key:
        raise FeishuCryptoError("加密飞书回调需要非空的 Encrypt Key")
    aes_key = hashlib.sha256(key.encode("utf-8")).digest()
    iv = os.urandom(_BLOCK_BYTES)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    padder = padding.PKCS7(_PADDING_BITS).padder()
    padded = padder.update(body) + padder.finalize()
    encryptor = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(iv + ciphertext).decode("ascii")


def compute_signature(encrypt_key: str, timestamp: str, nonce: str, body: bytes) -> str:
    """计算飞书回调签名：sha256(timestamp + nonce + encrypt_key + 原始请求体)。"""
    material = (timestamp + nonce + (encrypt_key or "")).encode("utf-8") + body
    return hashlib.sha256(material).hexdigest()


def verify_signature(
    *,
    encrypt_key: str,
    timestamp: str | None,
    nonce: str | None,
    body: bytes,
    signature: str | None,
) -> bool:
    """常量时间比较签名；任一参数缺失都返回 False（fail-closed）。"""
    if not encrypt_key or not timestamp or not nonce or not signature:
        return False
    expected = compute_signature(encrypt_key, timestamp, nonce, body)
    return hmac.compare_digest(expected, signature)
