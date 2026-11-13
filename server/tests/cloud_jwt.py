"""测试用账号云 JWT（标准库手写 HS256，不引入 PyJWT）。

与 `flux.cloud.session.verify_hs256` 使用同一套签名/编码方式，避免测试里出现
"用别的库生成、用本仓库验签"的假象。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

#: 测试专用假密钥；绝不能使用真实共享密钥。
CLOUD_TEST_SECRET = "test-cloud-secret"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def mint_cloud_token(
    sub: object = "1",
    *,
    typ: str = "access",
    secret: str = CLOUD_TEST_SECRET,
    expires_in: int | None = 3600,
    include_sub: bool = True,
    include_typ: bool = True,
) -> str:
    """签发一枚测试 access token；可按需丢掉 typ / sub 或造过期令牌。"""
    header = _b64url(
        json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8")
    )
    claims: dict[str, object] = {"iat": int(time.time())}
    if include_typ:
        claims["typ"] = typ
    if include_sub:
        claims["sub"] = sub
    if expires_in is not None:
        claims["exp"] = int(time.time()) + expires_in
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header}.{payload}"
    signature = hmac.new(
        secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{signing_input}.{_b64url(signature)}"
