"""企业微信回调的加解密与签名工具。

企业微信（WeCom）在「接收消息与事件」里使用一套与公众号同源、但与飞书不同的
加密约定（官方文档 path/90930）：

- ``EncodingAESKey`` 是 43 个字符的 base64（末尾省略了 ``=``），补上一个 ``=``
  解码后正好 32 字节，即 AES-256 密钥；初始向量固定取密钥前 16 字节；
- 明文打包为 ``random(16) + msg_len(4, 网络字节序) + msg + receiveid``；
- 按 PKCS#7 填充到 32 字节的整数倍，再 AES-CBC 加密并 base64 编码；
- 签名是 ``sha1``：把 ``token``、``timestamp``、``nonce`` 与密文
  （GET 的 ``echostr`` / POST 的 ``Encrypt``）按字典序排序后直接拼接再哈希。

这里只做纯函数，不读配置、不发请求；任何一步不合法都抛
`WeComCryptoError`，调用方据此 fail-closed，绝不返回"看着还行"的内容。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import struct

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

__all__ = [
    "WeComCryptoError",
    "compute_signature",
    "decrypt_message",
    "encrypt_message",
    "verify_signature",
]

#: AES-CBC 分组长度（字节）
_BLOCK_BYTES = 16
#: 企业微信 PKCS#7 填充块长度（字节）；与公众号一致按 32 字节对齐
_PAD_BYTES = 32
#: PKCS#7 填充位数（cryptography 以 bit 计）
_PADDING_BITS = _PAD_BYTES * 8
#: EncodingAESKey 去掉末尾等号后的标准长度
_ENCODING_KEY_CHARS = 43
#: 打包明文里随机前缀的长度
_RANDOM_BYTES = 16
#: 解密后允许的最大明文长度，避免畸形长度字段导致超大读取
_MAX_PLAINTEXT_BYTES = 1_048_576


class WeComCryptoError(ValueError):
    """回调签名或密文不合法；调用方据此 fail-closed。"""


def _aes_key(encoding_aes_key: str) -> bytes:
    """把 43 字符的 EncodingAESKey 还原成 32 字节 AES 密钥。"""
    raw = (encoding_aes_key or "").strip()
    if not raw:
        raise WeComCryptoError("企业微信回调缺少 EncodingAESKey")
    # 官方约定末尾省略了一个 '='；已经带上时不要重复补。
    padded = raw if raw.endswith("=") else raw + "="
    try:
        key = base64.b64decode(padded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise WeComCryptoError("EncodingAESKey 不是合法 base64") from exc
    if len(key) != 32:
        raise WeComCryptoError("EncodingAESKey 解码后不是 32 字节")
    return key


def compute_signature(token: str, timestamp: str, nonce: str, encrypted: str) -> str:
    """计算回调签名：sha1(字典序拼接 token / timestamp / nonce / 密文)。"""
    parts = sorted([token or "", timestamp or "", nonce or "", encrypted or ""])
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()


def verify_signature(
    *,
    token: str | None,
    timestamp: str | None,
    nonce: str | None,
    encrypted: str | None,
    signature: str | None,
) -> bool:
    """常量时间比较签名；任一参数缺失都返回 False（fail-closed）。"""
    if not token or not timestamp or not nonce or not encrypted or not signature:
        return False
    expected = compute_signature(token, timestamp, nonce, encrypted)
    # 用 bytes 比较：签名参数来自公网，非 ASCII 的字符串会让 str 版 compare_digest 抛错。
    return hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8"))


def decrypt_message(
    encoding_aes_key: str,
    encrypted: str,
    *,
    expected_receiveid: str | None = None,
) -> tuple[str, str]:
    """解密回调密文，返回 ``(msg, receiveid)``。

    ``msg`` 在 GET 校验里就是需要原样返回的明文 echostr，在 POST 里是消息 XML。
    ``expected_receiveid`` 非空时要求明文尾部的 receiveid 与之一致，否则拒绝。
    """
    key = _aes_key(encoding_aes_key)
    if not isinstance(encrypted, str) or not encrypted:
        raise WeComCryptoError("企业微信回调缺少密文")

    try:
        blob = base64.b64decode(encrypted, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise WeComCryptoError("企业微信回调密文不是合法 base64") from exc
    if len(blob) <= _BLOCK_BYTES or len(blob) % _BLOCK_BYTES != 0:
        raise WeComCryptoError("企业微信回调密文长度不合法")

    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:_BLOCK_BYTES])).decryptor()
    padded = decryptor.update(blob) + decryptor.finalize()
    unpadder = padding.PKCS7(_PADDING_BITS).unpadder()
    try:
        plaintext = unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        raise WeComCryptoError("企业微信回调填充不合法") from exc

    if len(plaintext) < _RANDOM_BYTES + 4:
        raise WeComCryptoError("企业微信回调明文过短")
    msg_len = struct.unpack(">I", plaintext[_RANDOM_BYTES : _RANDOM_BYTES + 4])[0]
    start = _RANDOM_BYTES + 4
    end = start + msg_len
    if msg_len > _MAX_PLAINTEXT_BYTES or end > len(plaintext):
        raise WeComCryptoError("企业微信回调明文长度字段不合法")

    try:
        msg = plaintext[start:end].decode("utf-8")
        receiveid = plaintext[end:].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WeComCryptoError("企业微信回调明文不是合法 UTF-8") from exc

    if expected_receiveid and receiveid != expected_receiveid:
        raise WeComCryptoError("企业微信回调的 receiveid 与配置的 CorpID 不一致")
    return msg, receiveid


def encrypt_message(encoding_aes_key: str, msg: str, receiveid: str) -> str:
    """按同一套打包规则加密（测试与本地联调用），返回 base64 字符串。"""
    key = _aes_key(encoding_aes_key)
    body = (msg or "").encode("utf-8")
    if len(body) > _MAX_PLAINTEXT_BYTES:
        raise WeComCryptoError("待加密内容过长")
    packed = (
        os.urandom(_RANDOM_BYTES)
        + struct.pack(">I", len(body))
        + body
        + (receiveid or "").encode("utf-8")
    )
    padder = padding.PKCS7(_PADDING_BITS).padder()
    padded = padder.update(packed) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:_BLOCK_BYTES])).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode("ascii")
