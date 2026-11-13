"""IM 频道 ↔ Flux 云账号绑定与一次性绑定码。

与设备配对码同一套安全口径：

- 绑定码 8 位、来自去掉 0/O/1/I 的字母表，单次有效，TTL 默认 10 分钟；
- 库中只存 sha256，明文只在签发响应里出现一次；
- 兑换用条件 UPDATE 做原子消费，并发下只有一个请求能成功；
- 失败原因对调用方统一为「无效或已过期」，不区分未知 / 已用 / 过期。

入站消息不携带任何凭据，唯一可信的身份来源是这条绑定关系。没有绑定的发送者
不会以任何人的身份被处理（详见 `flux.core.channels.binding_flow`）。
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.config import Settings
from flux.core.devices.service import PAIRING_ALPHABET
from flux.core.user_scope import normalize_cloud_user_id
from flux.errors import BadRequestError, NotFoundError, PermissionDeniedError
from flux.logging import get_logger
from flux.models.channel_binding import ChannelBindCode, ChannelUserBinding

logger = get_logger(__name__)

#: 支持的频道；外部用户 id 的取法见各频道的入站解析。
SUPPORTED_CHANNELS = frozenset({"feishu", "wecom", "dingtalk"})

#: 与配对码共用字母表（去掉 0/O/1/I，便于在 IM 里手输）。
BIND_CODE_ALPHABET = PAIRING_ALPHABET
BIND_CODE_LENGTH = 8

#: 绑定码默认有效期（秒），可由 FLUX_CHANNEL_BIND_CODE_TTL_SECONDS 覆盖。
DEFAULT_BIND_CODE_TTL_SECONDS = 600


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite 读回的时间会丢 tzinfo；比较前统一按 UTC 归一。"""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def hash_bind_code(raw: str) -> str:
    return hashlib.sha256((raw or "").strip().upper().encode("utf-8")).hexdigest()


def looks_like_bind_code(text: str) -> bool:
    """整条消息是否像一个绑定码（大小写不敏感）。

    只做形状判断；是否真的有效由 `redeem_code` 原子判定。形状不符的普通消息
    不会被误当成绑定码，绑定用户仍可正常聊天。
    """
    candidate = (text or "").strip().upper()
    return len(candidate) == BIND_CODE_LENGTH and all(
        char in BIND_CODE_ALPHABET for char in candidate
    )


def normalize_channel(value: str) -> str:
    channel = (value or "").strip().lower()
    if channel not in SUPPORTED_CHANNELS:
        raise BadRequestError(
            f"不支持的频道：{value}",
            details={"channel": value, "allowed": sorted(SUPPORTED_CHANNELS)},
        )
    return channel


class ChannelBindingService:
    """`channel_user_bindings` / `channel_bind_codes` 的读写入口。"""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], settings: Settings
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    @property
    def ttl_seconds(self) -> int:
        return max(1, int(self._settings.channel_bind_code_ttl_seconds))

    # --- 绑定码 ---

    async def issue_code(self, *, cloud_user_id: str | None) -> tuple[str, datetime]:
        """签发一次性绑定码，返回 (明文, 过期时间)。明文不落库。"""
        user = normalize_cloud_user_id(cloud_user_id)
        if user is None:
            raise PermissionDeniedError(
                "该操作需要 Flux 账号登录（或由部署方设置 FLUX_OWNER_CLOUD_USER_ID）",
                code="cloud_login_required",
            )
        code = "".join(secrets.choice(BIND_CODE_ALPHABET) for _ in range(BIND_CODE_LENGTH))
        now = _utcnow()
        expires_at = now + timedelta(seconds=self.ttl_seconds)
        async with self._session_factory() as session:
            session.add(
                ChannelBindCode(
                    id=uuid.uuid4(),
                    code_hash=hash_bind_code(code),
                    cloud_user_id=user,
                    expires_at=expires_at,
                    attempts=0,
                )
            )
            await session.commit()
        logger.info(
            "channel.bind_code.issue expires_at=%s cloud_user_id=%s",
            expires_at.isoformat(),
            user,
        )
        return code, expires_at

    async def redeem_code(self, code: str) -> str | None:
        """原子消费一个绑定码；无效 / 已用 / 过期返回 None。"""
        digest = hash_bind_code(code)
        now = _utcnow()
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ChannelBindCode).where(ChannelBindCode.code_hash == digest)
            )
            if row is None:
                return None
            if row.used_at is not None or (_aware(row.expires_at) or now) <= now:
                row.attempts = int(row.attempts or 0) + 1
                await session.commit()
                return None
            claimed = await session.execute(
                update(ChannelBindCode)
                .where(
                    ChannelBindCode.id == row.id,
                    ChannelBindCode.used_at.is_(None),
                )
                .values(used_at=now, attempts=ChannelBindCode.attempts + 1)
            )
            if claimed.rowcount != 1:
                await session.rollback()
                return None
            await session.commit()
            return normalize_cloud_user_id(row.cloud_user_id)

    # --- 绑定关系 ---

    async def get_binding(self, channel: str, external_user_id: str) -> ChannelUserBinding | None:
        channel_name = normalize_channel(channel)
        external = (external_user_id or "").strip()
        if not external:
            return None
        async with self._session_factory() as session:
            return await session.scalar(
                select(ChannelUserBinding).where(
                    ChannelUserBinding.channel == channel_name,
                    ChannelUserBinding.external_user_id == external,
                )
            )

    async def bind(
        self, *, channel: str, external_user_id: str, cloud_user_id: str
    ) -> ChannelUserBinding:
        """绑定 / 重新绑定一个外部用户；同一外部 id 只保留一条记录。"""
        channel_name = normalize_channel(channel)
        external = (external_user_id or "").strip()
        user = normalize_cloud_user_id(cloud_user_id)
        if not external or user is None:
            raise BadRequestError(
                "绑定需要有效的频道用户与 Flux 账号",
                details={"channel": channel_name, "external_user_id": external},
            )
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ChannelUserBinding).where(
                    ChannelUserBinding.channel == channel_name,
                    ChannelUserBinding.external_user_id == external,
                )
            )
            if row is None:
                row = ChannelUserBinding(
                    id=uuid.uuid4(),
                    channel=channel_name,
                    external_user_id=external,
                    cloud_user_id=user,
                )
                session.add(row)
            else:
                row.cloud_user_id = user
            try:
                await session.commit()
            except IntegrityError:
                # 并发首绑：另一路已插入同一 (channel, external_user_id)，改成更新它。
                await session.rollback()
                existing = await session.scalar(
                    select(ChannelUserBinding).where(
                        ChannelUserBinding.channel == channel_name,
                        ChannelUserBinding.external_user_id == external,
                    )
                )
                if existing is None:
                    raise
                existing.cloud_user_id = user
                await session.commit()
                row = existing
            await session.refresh(row)
        logger.info(
            "channel.binding.upsert channel=%s external=%s cloud_user_id=%s",
            channel_name,
            external,
            user,
        )
        return row

    async def list(self, cloud_user_id: str) -> list[ChannelUserBinding]:
        user = normalize_cloud_user_id(cloud_user_id)
        if user is None:
            return []
        statement = (
            select(ChannelUserBinding)
            .where(ChannelUserBinding.cloud_user_id == user)
            .order_by(ChannelUserBinding.created_at.asc(), ChannelUserBinding.id.asc())
        )
        async with self._session_factory() as session:
            return list(await session.scalars(statement))

    async def unbind(self, binding_id: str | uuid.UUID, *, cloud_user_id: str) -> None:
        """删除自己的绑定；别人的绑定按不存在处理（404），不泄露 id 是否存在。"""
        user = normalize_cloud_user_id(cloud_user_id)
        key = _as_uuid(binding_id)
        if user is None or key is None:
            raise NotFoundError(
                f"绑定 {binding_id} 不存在", details={"binding_id": str(binding_id)}
            )
        async with self._session_factory() as session:
            row = await session.get(ChannelUserBinding, key)
            if row is None or row.cloud_user_id != user:
                raise NotFoundError(
                    f"绑定 {binding_id} 不存在", details={"binding_id": str(binding_id)}
                )
            await session.delete(row)
            await session.commit()
        logger.info("channel.binding.removed id=%s cloud_user_id=%s", key, user)


def _as_uuid(value: str | uuid.UUID) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


__all__ = [
    "BIND_CODE_ALPHABET",
    "BIND_CODE_LENGTH",
    "ChannelBindingService",
    "SUPPORTED_CHANNELS",
    "hash_bind_code",
    "looks_like_bind_code",
    "normalize_channel",
]
