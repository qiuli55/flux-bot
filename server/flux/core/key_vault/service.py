"""密钥库服务（api_keys 表的唯一读写入口）。

职责：
- 加密落库：API Key 用 `SecretBox` 信封加密，明文只存在于内存与运行中的子进程环境；
- CRUD：对外只返回掩码与元数据；
- 模型通道解析：把任务级 `model + api_key_id` 解析成 `ModelChannel`，供各 runtime 启动 Run；
- 删除 key 时清空引用任务的模型覆盖（fail-safe，避免下一次 Run 用不存在的凭据）。

主密钥来源：`FLUX_KEY_VAULT_SECRET` 优先；未配置时首次启动自动生成
`~/.flux/key-vault.key`（0600）。主密钥丢失后已存 key 无法解密，只能重新录入。
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.config import Settings
from flux.core.agent_runtime.model_catalog import (
    KEY_PROVIDERS,
    PROVIDER_TITLES,
    KeyEntry,
    default_base_url,
    models_for_runtime,
    supports_provider,
)
from flux.core.agent_runtime.platforms import secure_file
from flux.core.agent_runtime.runtimes.base import ModelChannel
from flux.core.context.secretbox import SecretBox
from flux.errors import BadRequestError, ConfigurationError, ConflictError, NotFoundError
from flux.logging import get_logger
from flux.models.api_key import ApiKey
from flux.models.bot import Bot
from flux.models.task import Task

logger = get_logger(__name__)

#: 一次最多登记多少个模型，避免把整份模型目录塞进一条 key
_MAX_MODELS = 64
_MAX_MODEL_LENGTH = 128

_UNSET: object = object()


class KeyVaultService:
    """api_keys 表的读写与模型通道解析。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._box: SecretBox | None = None
        self._box_lock = threading.Lock()

    # --- 查询 ---

    async def list_entries(self, *, enabled_only: bool = False) -> list[ApiKey]:
        async with self._session_factory() as session:
            stmt = select(ApiKey).order_by(ApiKey.created_at.asc(), ApiKey.name.asc())
            if enabled_only:
                stmt = stmt.where(ApiKey.enabled.is_(True))
            rows = (await session.scalars(stmt)).all()
            return list(rows)

    async def list_public(self, *, enabled_only: bool = False) -> list[dict[str, object]]:
        rows = await self.list_entries(enabled_only=enabled_only)
        return [row.to_dict() for row in rows]

    async def catalog_entries(self, *, enabled_only: bool = True) -> list[KeyEntry]:
        """给模型目录用的安全投影（id 为字符串，含 models / provider / enabled）。"""
        rows = await self.list_entries(enabled_only=enabled_only)
        return [_entry_of(row) for row in rows]

    async def resolve_selection(
        self,
        *,
        runtime: str | None,
        model: str,
        key_id: str | None,
    ) -> tuple[str | None, uuid.UUID | None]:
        """校验任务级模型选择，返回 (模型 id, key 库 UUID)。

        - 无 model 且无 key_id：返回 (None, None)，表示用 runtime 默认模型；
        - 有 key_id：key 必须启用、模型必须在 key 的列表里、runtime 协议必须兼容；
          只给 key 不给 model 时取该 key 的第一个模型；
        - 无 key_id：按 runtime 原生模型目录校验（目录为空时沿用旧口径放行）。
        """
        wanted = (model or "").strip()
        if not wanted and not key_id:
            return None, None
        if not key_id:
            allowed = models_for_runtime(self._settings, runtime)
            if allowed and wanted not in allowed:
                raise BadRequestError(
                    f"该 Agent 不支持模型 {wanted}",
                    details={"model": wanted, "allowed": allowed},
                )
            return wanted, None
        row = await self.get(key_id)
        if not row.enabled:
            raise BadRequestError(f"密钥 {row.name!r} 已停用", details={"key_id": str(row.id)})
        if not supports_provider(runtime, row.provider):
            provider_title = PROVIDER_TITLES.get(row.provider, row.provider)
            raise BadRequestError(
                f"当前 Agent 的 runtime（{runtime or 'claude-code'}）不能使用"
                f" {provider_title} 协议的密钥",
                details={"key_id": str(row.id), "provider": row.provider, "runtime": runtime},
            )
        models = [str(item).strip() for item in (row.models or []) if str(item).strip()]
        if not wanted:
            if not models:
                raise BadRequestError(
                    f"密钥 {row.name!r} 没有登记任何模型", details={"key_id": str(row.id)}
                )
            wanted = models[0]
        if wanted not in models:
            raise BadRequestError(
                f"密钥 {row.name!r} 没有登记模型 {wanted}",
                details={"key_id": str(row.id), "model": wanted, "allowed": models},
            )
        return wanted, row.id

    async def get(self, key_id: str | uuid.UUID) -> ApiKey:
        key_uuid = _parse_uuid(key_id)
        async with self._session_factory() as session:
            row = await session.get(ApiKey, key_uuid)
            if row is None:
                raise NotFoundError(f"密钥 {key_id} 不存在", details={"key_id": str(key_id)})
            return row

    # --- 写入 ---

    async def create(
        self,
        *,
        name: str,
        provider: str,
        base_url: str = "",
        api_key: str = "",
        models: Iterable[str] = (),
        wire_api: str = "",
        enabled: bool = True,
    ) -> ApiKey:
        clean_name = _require_name(name)
        clean_provider = _require_provider(provider)
        clean_base_url = _normalize_base_url(clean_provider, base_url)
        clean_wire_api = _normalize_wire_api(wire_api)
        clean_models = _normalize_models(models)
        secret = _require_api_key(api_key)
        row = ApiKey(
            name=clean_name,
            provider=clean_provider,
            base_url=clean_base_url,
            wire_api=clean_wire_api,
            models=clean_models,
            key_ciphertext=self._seal(secret),
            key_hint=_hint(secret),
            enabled=bool(enabled),
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ConflictError(
                    f"密钥名称 {clean_name!r} 已存在", details={"name": clean_name}
                ) from exc
            await session.refresh(row)
            logger.info(
                "key_vault.create id=%s name=%s provider=%s models=%s",
                row.id,
                row.name,
                row.provider,
                len(row.models or []),
            )
            return row

    async def update(
        self,
        key_id: str | uuid.UUID,
        *,
        name: object = _UNSET,
        provider: object = _UNSET,
        base_url: object = _UNSET,
        api_key: object = _UNSET,
        models: object = _UNSET,
        wire_api: object = _UNSET,
        enabled: object = _UNSET,
    ) -> ApiKey:
        key_uuid = _parse_uuid(key_id)
        async with self._session_factory() as session:
            row = await session.get(ApiKey, key_uuid)
            if row is None:
                raise NotFoundError(f"密钥 {key_id} 不存在", details={"key_id": str(key_id)})
            if name is not _UNSET:
                row.name = _require_name(str(name))
            if provider is not _UNSET:
                row.provider = _require_provider(str(provider))
            if base_url is not _UNSET:
                row.base_url = str(base_url or "").strip()
            if wire_api is not _UNSET:
                row.wire_api = _normalize_wire_api(str(wire_api or ""))
            if models is not _UNSET:
                row.models = _normalize_models(models)  # type: ignore[arg-type]
            if api_key is not _UNSET and str(api_key or "").strip():
                secret = _require_api_key(str(api_key))
                row.key_ciphertext = self._seal(secret)
                row.key_hint = _hint(secret)
            if enabled is not _UNSET:
                row.enabled = bool(enabled)
            # 兼容端点必须给出 base_url；改完 provider 后统一校验
            row.base_url = _normalize_base_url(row.provider, row.base_url)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ConflictError(
                    f"密钥名称 {row.name!r} 已存在", details={"name": row.name}
                ) from exc
            await session.refresh(row)
            logger.info(
                "key_vault.update id=%s name=%s provider=%s enabled=%s",
                row.id,
                row.name,
                row.provider,
                row.enabled,
            )
            return row

    async def delete(self, key_id: str | uuid.UUID) -> None:
        key_uuid = _parse_uuid(key_id)
        async with self._session_factory() as session:
            row = await session.get(ApiKey, key_uuid)
            if row is None:
                raise NotFoundError(f"密钥 {key_id} 不存在", details={"key_id": str(key_id)})
            # fail-safe：引用该 key 的任务回到 Agent 默认模型，避免下一次 Run 用不存在的凭据
            await session.execute(
                update(Task).where(Task.api_key_id == key_uuid).values(model=None, api_key_id=None)
            )
            await session.delete(row)
            await session.commit()
            logger.info("key_vault.delete id=%s name=%s", key_uuid, row.name)

    # --- 模型通道 ---

    async def channel_for_task(self, task: Task) -> ModelChannel | None:
        """解析任务的模型通道；任务没有 key 覆盖时返回 None（runtime 用原生配置）。"""
        return await self._resolve_channel(task.api_key_id, task.model)

    async def channel_for_bot(self, bot: Bot) -> ModelChannel | None:
        """解析 Bot 的模型通道；Bot 没有 key 覆盖时返回 None（走平台默认）。"""
        return await self._resolve_channel(bot.api_key_id, bot.model)

    async def _resolve_channel(
        self, api_key_id: uuid.UUID | None, model: str | None
    ) -> ModelChannel | None:
        if api_key_id is None:
            return None
        row = await self.get(api_key_id)
        if not row.enabled:
            raise ConfigurationError(
                f"密钥 {row.name!r} 已停用，无法启动 Run",
                details={"key_id": str(row.id), "hint": "在密钥库中启用它，或切换到其他模型"},
            )
        model = (model or "").strip()
        if not model:
            models = [str(item) for item in (row.models or []) if str(item).strip()]
            if not models:
                raise ConfigurationError(
                    f"密钥 {row.name!r} 没有登记任何模型",
                    details={"key_id": str(row.id)},
                )
            model = models[0]
        return await self._channel(row, model)

    async def channel_for_key(self, key_id: str, model: str) -> ModelChannel:
        """按 key + 模型解析通道（Run 创建时的显式选择用）。"""
        row = await self.get(key_id)
        if not row.enabled:
            raise BadRequestError(f"密钥 {row.name!r} 已停用", details={"key_id": str(row.id)})
        return await self._channel(row, model)

    async def _channel(self, row: ApiKey, model: str) -> ModelChannel:
        base_url = _normalize_base_url(
            row.provider, row.base_url, require=False
        ) or default_base_url(self._settings, row.provider)
        if not base_url:
            raise ConfigurationError(
                f"密钥 {row.name!r} 没有可用的 Base URL",
                details={"key_id": str(row.id), "provider": row.provider},
            )
        return ModelChannel(
            provider=row.provider,
            base_url=base_url,
            api_key=self._open(row.key_ciphertext),
            wire_api=row.wire_api,
            label=row.name,
            key_id=str(row.id),
        )

    # --- 加密 ---

    def seal_text(self, value: str) -> bytes:
        """用同一把主密钥加密任意文本（外部连接器令牌复用，避免再实现一套主密钥管理）。"""
        return self._seal(value)

    def open_text(self, ciphertext: bytes) -> str:
        return self._open(ciphertext)

    def _box_or_create(self) -> SecretBox:
        with self._box_lock:
            if self._box is None:
                self._box = SecretBox(self._master_key())
            return self._box

    def _master_key(self) -> str:
        configured = (self._settings.key_vault_secret or "").strip()
        if configured:
            return configured
        path = self._settings.key_vault_key_path
        try:
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ConfigurationError(
                f"无法读取密钥库主密钥文件：{path}",
                details={"path": str(path), "error": str(exc)},
            ) from exc
        secret = secrets.token_hex(32)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(secret, encoding="utf-8")
        except OSError as exc:
            raise ConfigurationError(
                f"无法创建密钥库主密钥文件：{path}",
                details={"path": str(path), "error": str(exc)},
            ) from exc
        if not secure_file(path):
            logger.warning("密钥库主密钥文件权限弱化（未能收紧到仅属主可访问）path=%s", path)
        if self._has_stored_secrets():
            logger.warning(
                "密钥库主密钥文件不存在，已自动生成新主密钥：%s。"
                "警告：数据库中已有加密密钥，新主密钥与它们不匹配，现有密钥将无法解密！"
                "丢失该文件会导致已存密钥永久不可用；请立即备份该文件，"
                "或设置 FLUX_KEY_VAULT_SECRET 以避免依赖本机文件。",
                path,
            )
        else:
            logger.warning(
                "密钥库主密钥文件不存在，已自动生成：%s。"
                "丢失该文件会导致已存的 API Key / GitHub Token 无法解密；"
                "强烈建议设置 FLUX_KEY_VAULT_SECRET 或立即备份该文件。",
                path,
            )
        logger.info("key_vault.master_key_created path=%s", path)
        return secret

    def _has_stored_secrets(self) -> bool:
        """尽力检查库里是否已有密文；SQLite 用只读连接同步查，其他库直接跳过。

        只在自动生成主密钥时调用一次，属于廉价探测；表不存在或连接失败都按
        「无法确认」处理，不阻断密钥生成本身。
        """
        try:
            url = make_url(self._settings.database_url)
            if url.get_backend_name() != "sqlite" or not url.database:
                return False
            db_path = Path(url.database)
            if not db_path.is_file():
                return False
            with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
                for table, column in (
                    ("api_keys", "key_ciphertext"),
                    ("github_connections", "token_ciphertext"),
                    ("connector_installations", "credential_ciphertext"),
                ):
                    try:
                        row = conn.execute(
                            f"SELECT 1 FROM {table} WHERE {column} IS NOT NULL LIMIT 1"
                        ).fetchone()
                    except sqlite3.Error:
                        continue
                    if row is not None:
                        return True
        except (sqlite3.Error, OSError, ValueError):
            return False
        return False

    def _seal(self, secret: str) -> bytes:
        return self._box_or_create().seal(secret.encode("utf-8"))

    def _open(self, ciphertext: bytes) -> str:
        try:
            return self._box_or_create().open(bytes(ciphertext)).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ConfigurationError(
                "密钥库主密钥不匹配，已存 key 无法解密；请重新录入该 key",
                details={"policy": "key_vault decrypt failed"},
            ) from exc


def _entry_of(row: ApiKey) -> KeyEntry:
    return KeyEntry(
        id=str(row.id),
        name=row.name,
        provider=row.provider,
        base_url=row.base_url,
        wire_api=row.wire_api,
        models=tuple(str(item) for item in (row.models or [])),
        key_hint=row.key_hint,
        enabled=bool(row.enabled),
    )


def _parse_uuid(value: str | uuid.UUID) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise BadRequestError(f"非法的密钥 id：{value!r}", details={"key_id": str(value)}) from exc


def _require_name(raw: str) -> str:
    name = (raw or "").strip()
    if not name:
        raise BadRequestError("密钥名称不能为空", details={"field": "name"})
    if len(name) > 128:
        raise BadRequestError("密钥名称过长（最多 128 字符）", details={"field": "name"})
    return name


def _require_provider(raw: str) -> str:
    provider = (raw or "").strip().lower()
    if provider not in KEY_PROVIDERS:
        raise BadRequestError(
            f"不支持的供应商类型：{raw!r}",
            details={"provider": provider, "allowed": list(KEY_PROVIDERS)},
        )
    return provider


def _require_api_key(raw: str) -> str:
    secret = (raw or "").strip()
    if not secret:
        raise BadRequestError("API Key 不能为空", details={"field": "api_key"})
    if len(secret) > 4096:
        raise BadRequestError("API Key 过长", details={"field": "api_key"})
    return secret


def _normalize_base_url(provider: str, raw: str, *, require: bool = True) -> str:
    url = (raw or "").strip().rstrip("/")
    if not url and require and provider == "openai_compatible":
        raise BadRequestError("OpenAI 兼容端点必须填写 Base URL", details={"field": "base_url"})
    if url and not url.startswith(("http://", "https://")):
        raise BadRequestError("Base URL 必须是 http(s) 地址", details={"field": "base_url"})
    return url


def _normalize_wire_api(raw: str) -> str:
    value = (raw or "").strip().lower()
    if value not in ("", "chat", "responses"):
        raise BadRequestError(
            "wire_api 只能是 chat / responses（或留空自动推导）",
            details={"field": "wire_api", "value": value},
        )
    return value


def _normalize_models(models: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    for item in models:
        value = str(item or "").strip()
        if not value:
            continue
        if len(value) > _MAX_MODEL_LENGTH:
            raise BadRequestError(
                f"模型 id 过长（最多 {_MAX_MODEL_LENGTH} 字符）：{value[:32]}…",
                details={"field": "models"},
            )
        if value not in cleaned:
            cleaned.append(value)
    if not cleaned:
        raise BadRequestError("至少登记一个模型 id", details={"field": "models"})
    if len(cleaned) > _MAX_MODELS:
        raise BadRequestError(
            f"一条 key 最多登记 {_MAX_MODELS} 个模型", details={"field": "models"}
        )
    return cleaned


def _hint(secret: str) -> str:
    tail = secret[-4:] if len(secret) >= 8 else ""
    return f"••••{tail}" if tail else "••••"


__all__ = ["KeyVaultService", "PROVIDER_TITLES"]
