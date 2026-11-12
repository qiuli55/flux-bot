"""钉钉事件订阅（HTTP 推送）的验签与加解密工具。

官方方案（open-dingtalk/DingTalk-Callback-Crypto 与「HTTP回调概述」）：

- 后台配置 43 位 EncodingAESKey；实际 AES 密钥是 ``Base64Decode(aes_key + "=")``；
- 签名 ``msg_signature = sha1("".join(sorted([token, timestamp, nonce, encrypt])))``；
- AES-256-CBC，IV 取密钥前 16 字节，**PKCS7 块大小是 32 字节**（不是 AES 的 16）；
- 明文布局 ``random(16B) + msg_len(4B，网络字节序) + msg + ownerKey``；
- 应用级事件订阅的 ownerKey 是 AppKey；
- 处理成功后必须返回 ``{"msg_signature", "timeStamp", "nonce", "encrypt"}``，
  其中 ``encrypt`` 是字符串 ``"success"`` 的密文。

这里只做纯函数，不读配置、不发请求，便于测试与复用；任何一步不合法都抛
`DingTalkCryptoError`，调用方据此 fail-closed。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from typing import Any

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

__all__ = [
    "DingTalkCryptoError",
    "build_encrypted_response",
    "compute_signature",
    "decode_aes_key",
    "decrypt_event",
    "decrypt_message",
    "encrypt_message",
    "verify_signature",
]

#: 官方 EncodingAESKey 固定 43 位
AES_KEY_CHARS = 43
#: 明文前缀的随机串长度
_RANDOM_BYTES = 16
#: 网络字节序的消息长度字段
_LENGTH_BYTES = 4
#: 官方 PKCS7 块大小是 32 字节（不是 AES 分组的 16）
_BLOCK_BYTES = 32
#: PKCS7 填充位数（以 bit 计）
_PADDING_BITS = _BLOCK_BYTES * 8


class DingTalkCryptoError(ValueError):
    """回调验签/加解密数据不合法；调用方据此 fail-closed。"""


def decode_aes_key(aes_key: str) -> bytes:
    """把 43 位 EncodingAESKey 还原成 32 字节 AES 密钥。"""
    key = (aes_key or "").strip()
    if len(key) != AES_KEY_CHARS:
        raise DingTalkCryptoError("钉钉 EncodingAESKey 必须是 43 位")
    try:
        raw = base64.b64decode(key + "=", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise DingTalkCryptoError("钉钉 EncodingAESKey 不是合法 base64") from exc
    if len(raw) != 32:
        raise DingTalkCryptoError("钉钉 EncodingAESKey 解码后不是 32 字节")
    return raw


def compute_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """按官方规则计算签名：四个字符串字典序排序后拼接再 SHA-1。"""
    parts = [token, timestamp, nonce, encrypt]
    material = "".join(sorted(parts))
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


def verify_signature(
    *,
    token: str | None,
    timestamp: str | None,
    nonce: str | None,
    encrypt: str | None,
    signature: str | None,
) -> bool:
    """常量时间比较签名；任一参数缺失都返回 False（fail-closed）。"""
    if not token or not timestamp or not nonce or not encrypt or not signature:
        return False
    expected = compute_signature(token, timestamp, nonce, encrypt)
    # 按字节比较，避免调用方传入非 ASCII 字符串时 compare_digest 抛 TypeError。
    return hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8"))


def decrypt_message(
    *,
    token: str,
    aes_key: str,
    owner_key: str,
    signature: str | None,
    timestamp: str | None,
    nonce: str | None,
    encrypt: str | None,
) -> str:
    """验签后解密，返回明文；ownerKey 不匹配同样视为不合法。"""
    if not verify_signature(
        token=token,
        timestamp=timestamp,
        nonce=nonce,
        encrypt=encrypt,
        signature=signature,
    ):
        raise DingTalkCryptoError("钉钉回调签名校验失败")
    if not encrypt:
        raise DingTalkCryptoError("钉钉回调缺少 encrypt 字段")
    owner = (owner_key or "").strip()
    if not owner:
        raise DingTalkCryptoError("解密钉钉回调需要非空的 ownerKey（应用级事件为 AppKey）")

    key = decode_aes_key(aes_key)
    try:
        blob = base64.b64decode(encrypt, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise DingTalkCryptoError("钉钉加密回调不是合法 base64") from exc
    if not blob or len(blob) % _BLOCK_BYTES != 0:
        raise DingTalkCryptoError("钉钉加密回调长度不合法")

    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    padded = decryptor.update(blob) + decryptor.finalize()

    unpad = padded[-1] if padded else 0
    if not 1 <= unpad <= _BLOCK_BYTES:
        raise DingTalkCryptoError("钉钉加密回调填充不合法")
    if padded[-unpad:] != bytes([unpad]) * unpad:
        raise DingTalkCryptoError("钉钉加密回调填充不合法")
    body = padded[:-unpad]
    if len(body) < _RANDOM_BYTES + _LENGTH_BYTES:
        raise DingTalkCryptoError("钉钉加密回调明文过短")

    (msg_len,) = struct.unpack(">I", body[_RANDOM_BYTES : _RANDOM_BYTES + _LENGTH_BYTES])
    msg_start = _RANDOM_BYTES + _LENGTH_BYTES
    msg_end = msg_start + msg_len
    if msg_end > len(body):
        raise DingTalkCryptoError("钉钉加密回调的消息长度不合法")

    try:
        message = body[msg_start:msg_end].decode("utf-8")
        from_owner = body[msg_end:].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DingTalkCryptoError("钉钉加密回调明文不是合法 UTF-8") from exc
    if not hmac.compare_digest(from_owner.encode("utf-8"), owner.encode("utf-8")):
        raise DingTalkCryptoError("钉钉加密回调的 ownerKey 不匹配")
    return message


def decrypt_event(
    *,
    token: str,
    aes_key: str,
    owner_key: str,
    signature: str | None,
    timestamp: str | None,
    nonce: str | None,
    encrypt: str | None,
) -> dict[str, Any]:
    """解密并解析为 JSON 对象；明文不是 JSON 对象时抛错。"""
    message = decrypt_message(
        token=token,
        aes_key=aes_key,
        owner_key=owner_key,
        signature=signature,
        timestamp=timestamp,
        nonce=nonce,
        encrypt=encrypt,
    )
    try:
        data = json.loads(message)
    except ValueError as exc:
        raise DingTalkCryptoError("钉钉加密回调明文不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise DingTalkCryptoError("钉钉加密回调明文不是 JSON 对象")
    return data


def encrypt_message(aes_key: str, owner_key: str, plaintext: str) -> str:
    """按同一套算法加密，返回 base64 密文（测试与本地联调用）。"""
    owner = (owner_key or "").strip()
    if not owner:
        raise DingTalkCryptoError("加密钉钉回调需要非空的 ownerKey")
    key = decode_aes_key(aes_key)
    payload = (plaintext or "").encode("utf-8")
    body = (
        os.urandom(_RANDOM_BYTES)
        + struct.pack(">I", len(payload))
        + payload
        + owner.encode("utf-8")
    )
    padder = padding.PKCS7(_PADDING_BITS).padder()
    padded = padder.update(body) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode("ascii")


def build_encrypted_response(
    *,
    token: str,
    aes_key: str,
    owner_key: str,
    plaintext: str = "success",
    timestamp: str | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """构造官方要求的应答体（字段名保持文档里的驼峰 ``timeStamp``）。"""
    ts = timestamp or str(int(time.time() * 1000))
    nc = nonce or secrets.token_hex(8)
    encrypt = encrypt_message(aes_key, owner_key, plaintext)
    return {
        "msg_signature": compute_signature(token, ts, nc, encrypt),
        "timeStamp": ts,
        "nonce": nc,
        "encrypt": encrypt,
    }
