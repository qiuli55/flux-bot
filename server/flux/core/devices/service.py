"""设备配对码与设备令牌（手机端长期凭据）。

安全约束：

- 设备令牌明文形如 ``fbd_<32 字节 urlsafe>``，只在配对成功响应里出现一次；
  库中仅存 sha256，列表只展示前 8 个字符。
- 配对码 8 位、来自去掉 0/O/1/I 的字母表，单次有效；错误原因不区分
  “未知 / 已用 / 过期”，统一 ``invalid_pairing_code``，避免探测。
- 设备鉴权带 30 秒进程内缓存；撤销会立刻清除缓存项，其他进程最多延迟
  一个缓存 TTL（文档里明确说明）。
- ``last_seen_at`` 最多每分钟写一次，避免每个请求都打数据库。
"""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.config import Settings
from flux.errors import AIOSError, BadRequestError, NotFoundError
from flux.logging import get_logger
from flux.models.device import Device, PairingCode

logger = get_logger(__name__)

DEVICE_TOKEN_PREFIX = "fbd_"
DEVICE_TOKEN_SECRET_BYTES = 32

#: 配对码字母表：去掉 0/O/1/I，避免用户抄错。
PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIRING_CODE_LENGTH = 8

#: 设备鉴权缓存的进程内 TTL（秒）。撤销后其他进程最迟在这个时间后生效。
DEVICE_CACHE_TTL_SECONDS = 30.0
#: last_seen_at 的最小写入间隔（秒）。
LAST_SEEN_INTERVAL_SECONDS = 60.0

#: 每个来源 IP 每分钟允许的失败次数。
PAIRING_MAX_FAILURES = 10
PAIRING_FAILURE_WINDOW_SECONDS = 60.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite 读回的时间会丢 tzinfo；比较前统一按 UTC 归一。"""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def hash_device_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_user_id(value: object) -> str | None:
    """云用户 id 统一字符串；None / 空白视为未提供。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class PairingRateLimiter:
    """按来源 IP 的失败次数限流（进程内，够用且不引入外部依赖）。"""

    def __init__(
        self,
        *,
        max_failures: int = PAIRING_MAX_FAILURES,
        window_seconds: float = PAIRING_FAILURE_WINDOW_SECONDS,
    ) -> None:
        self._max_failures = max(1, max_failures)
        self._window_seconds = max(1.0, window_seconds)
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        bucket = self._failures[key]
        cutoff = now - self._window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        return bucket

    def allowed(self, key: str) -> bool:
        now = time.monotonic()
        return len(self._prune(key, now)) < self._max_failures

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        self._prune(key, now)
        self._failures[key].append(now)

    def reset(self) -> None:
        self._failures.clear()


class DeviceService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._cache: dict[str, tuple[float, Device]] = {}
        self._rate_limiter = PairingRateLimiter()

    # --- 配对码 ---

    def _generate_code(self) -> str:
        return "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))

    async def issue_pairing_code(self, *, cloud_user_id: str | None = None) -> tuple[str, datetime]:
        """生成一次性配对码，返回 (明文, 过期时间)。明文不落库。

        `cloud_user_id` 由 API / CLI 层解析（云 JWT 的 sub，或主用户兜底）；
        兑换出的设备会继承它，从而把"这台手机属于谁"钉在配对码上。
        """
        code = self._generate_code()
        now = _utcnow()
        expires_at = now + timedelta(seconds=max(1, int(self._settings.pairing_code_ttl_seconds)))
        async with self._session_factory() as session:
            row = PairingCode(
                id=uuid.uuid4(),
                code_hash=hash_device_token(code),
                expires_at=expires_at,
                cloud_user_id=_normalize_user_id(cloud_user_id),
            )
            session.add(row)
            await session.commit()
        logger.info(
            "device.pairing_code.issue expires_at=%s cloud_user_id=%s",
            expires_at.isoformat(),
            _normalize_user_id(cloud_user_id),
        )
        return code, expires_at

    async def pair(self, *, code: str, device_name: str, client_ip: str) -> tuple[Device, str]:
        """用配对码兑换设备令牌。失败统一抛 invalid_pairing_code。"""
        if not self._rate_limiter.allowed(client_ip):
            raise AIOSError(
                "配对尝试过于频繁，请稍后再试",
                code="rate_limited",
                http_status=429,
                details={"retry_after_seconds": int(PAIRING_FAILURE_WINDOW_SECONDS)},
            )

        normalized = (code or "").strip().upper()
        digest = hash_device_token(normalized)
        now = _utcnow()
        failure = BadRequestError(
            "配对码无效或已过期",
            code="invalid_pairing_code",
            details={"code": "invalid_pairing_code"},
        )
        async with self._session_factory() as session:
            row = await session.scalar(select(PairingCode).where(PairingCode.code_hash == digest))
            if row is None:
                self._rate_limiter.record_failure(client_ip)
                raise failure
            expired = _aware(row.expires_at) <= now
            if row.used_at is not None or expired:
                row.attempts = int(row.attempts or 0) + 1
                await session.commit()
                self._rate_limiter.record_failure(client_ip)
                raise failure
            # 条件更新 + 插入设备放在同一事务里：要么都成功，要么都不生效。
            # WHERE used_at IS NULL 保证并发兑换时只有第一个 writer 拿到 rowcount=1。
            claimed = await session.execute(
                update(PairingCode)
                .where(PairingCode.id == row.id, PairingCode.used_at.is_(None))
                .values(used_at=now, attempts=PairingCode.attempts + 1)
            )
            if claimed.rowcount != 1:
                await session.rollback()
                self._rate_limiter.record_failure(client_ip)
                raise failure

            raw = f"{DEVICE_TOKEN_PREFIX}{secrets.token_urlsafe(DEVICE_TOKEN_SECRET_BYTES)}"
            expires_at = self._device_expiry(now)
            # 设备归属继承配对码；老码 NULL 时按主用户兜底（仍无则保持 NULL）。
            device = Device(
                id=uuid.uuid4(),
                name=(device_name or "").strip()[:128],
                token_hash=hash_device_token(raw),
                token_prefix=raw[:8],
                expires_at=expires_at,
                cloud_user_id=_normalize_user_id(row.cloud_user_id)
                or _normalize_user_id(self._settings.owner_cloud_user_id),
            )
            session.add(device)
            await session.commit()
            await session.refresh(device)
        logger.info(
            "device.pair success device=%s prefix=%s cloud_user_id=%s expires_at=%s",
            device.id,
            device.token_prefix,
            device.cloud_user_id,
            device.expires_at.isoformat() if device.expires_at else None,
        )
        return device, raw

    def _device_expiry(self, now: datetime) -> datetime | None:
        days = int(self._settings.device_token_ttl_days)
        return now + timedelta(days=days) if days > 0 else None

    # --- 设备令牌鉴权 ---

    async def resolve_token(self, raw: str | None) -> Device | None:
        """校验设备令牌；未命中/已撤销/已过期返回 None。命中时按需刷新 last_seen。"""
        if not raw:
            return None
        digest = hash_device_token(raw)
        now = _utcnow()
        now_monotonic = time.monotonic()

        cached = self._cache.get(digest)
        if cached is not None:
            loaded_at, device = cached
            if now_monotonic - loaded_at <= DEVICE_CACHE_TTL_SECONDS:
                if device.revoked_at is not None:
                    self._cache.pop(digest, None)
                    return None
                expires = _aware(device.expires_at)
                if expires is not None and expires <= now:
                    self._cache.pop(digest, None)
                    return None
                await self._touch_last_seen(device, now)
                return device
            self._cache.pop(digest, None)

        async with self._session_factory() as session:
            device = await session.scalar(select(Device).where(Device.token_hash == digest))
        if device is None or device.revoked_at is not None:
            return None
        expires = _aware(device.expires_at)
        if expires is not None and expires <= now:
            return None
        self._cache[digest] = (now_monotonic, device)
        await self._touch_last_seen(device, now)
        return device

    async def _touch_last_seen(self, device: Device, now: datetime) -> None:
        last_seen = _aware(device.last_seen_at)
        if last_seen is not None and (now - last_seen).total_seconds() < LAST_SEEN_INTERVAL_SECONDS:
            return
        async with self._session_factory() as session:
            row = await session.get(Device, device.id)
            if row is None:
                return
            row.last_seen_at = now
            await session.commit()
        device.last_seen_at = now

    # --- 管理面 ---

    async def list(self, *, cloud_user_id: str) -> list[Device]:
        """只列当前用户名下的设备；NULL 归属仅对配置的主用户可见。"""
        owner = _normalize_user_id(cloud_user_id)
        if owner is None:
            return []
        statement = select(Device).where(self._owned_clause(owner))
        async with self._session_factory() as session:
            return list(await session.scalars(statement.order_by(Device.created_at.asc())))

    def _owned_clause(self, cloud_user_id: str):
        """当前用户可见的设备范围：自己的 + （仅主用户）历史 NULL 归属的。"""
        conditions = [Device.cloud_user_id == cloud_user_id]
        if cloud_user_id == _normalize_user_id(self._settings.owner_cloud_user_id):
            conditions.append(Device.cloud_user_id.is_(None))
        return or_(*conditions)

    async def get(self, device_id: str | uuid.UUID) -> Device:
        key = self._as_uuid(device_id)
        async with self._session_factory() as session:
            device = await session.get(Device, key)
            if device is None:
                raise NotFoundError(
                    f"设备 {device_id} 不存在", details={"device_id": str(device_id)}
                )
            return device

    async def revoke(
        self,
        device_id: str | uuid.UUID,
        *,
        cloud_user_id: str | None = None,
    ) -> Device:
        """撤销设备并立即清除本进程缓存。

        `cloud_user_id` 非空时做归属校验：别人的设备一律按"不存在"处理（404），
        避免泄露设备 id 是否存在。设备自助登出（`/devices/me/revoke`）传 None，
        因为它的 device_id 已由令牌限定为调用者自己。
        """
        key = self._as_uuid(device_id)
        async with self._session_factory() as session:
            device = await session.get(Device, key)
            if device is None:
                raise NotFoundError(
                    f"设备 {device_id} 不存在", details={"device_id": str(device_id)}
                )
            if cloud_user_id is not None:
                owner = _normalize_user_id(cloud_user_id)
                legacy_ok = (
                    _normalize_user_id(device.cloud_user_id) is None
                    and owner is not None
                    and owner == _normalize_user_id(self._settings.owner_cloud_user_id)
                )
                if _normalize_user_id(device.cloud_user_id) != owner and not legacy_ok:
                    # 与"设备不存在"同一个错误，避免跨用户探测设备 id。
                    raise NotFoundError(
                        f"设备 {device_id} 不存在", details={"device_id": str(device_id)}
                    )
            if device.revoked_at is None:
                device.revoked_at = _utcnow()
            await session.commit()
        self._cache.pop(device.token_hash, None)
        logger.info("device.revoke id=%s prefix=%s", device.id, device.token_prefix)
        return device

    async def revoke_cached_token(self, raw: str) -> None:
        self._cache.pop(hash_device_token(raw), None)

    @staticmethod
    def _as_uuid(value: str | uuid.UUID) -> uuid.UUID:
        if isinstance(value, uuid.UUID):
            return value
        try:
            return uuid.UUID(str(value))
        except ValueError as exc:
            raise NotFoundError(
                f"设备标识非法：{value}", details={"device_id": str(value)}
            ) from exc
