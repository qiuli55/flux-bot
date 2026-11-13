"""用户保险库服务（站点登录 / 支付卡 / 联系人）。

设计要点：

- **加密复用**：所有密文都走注入的 `KeyVaultService.seal_text / open_text`，
  与模型密钥库共用同一把主密钥（`FLUX_KEY_VAULT_SECRET` 或
  `~/.flux/key-vault.key`），不复制密钥派生逻辑；
- **只写用户 API**：本服务只被 `/api/v1/vault/...` 调用，MCP / Agent 面不暴露写工具；
- **最小暴露**：列表 / 详情只回掩码与元数据；登录密码只支持显式 reveal（一次性返回并
  记审计日志，日志里不含密码）；支付卡没有任何返回 PAN / CVV 的接口；
- **失配即失败**：主密钥不匹配时 `KeyVaultService.open_text` 抛 `ConfigurationError`，
  本层不吞异常，绝不把乱码当明文返回。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.core.key_vault.service import KeyVaultService
from flux.errors import BadRequestError, ConfigurationError, NotFoundError
from flux.logging import get_logger
from flux.models.vault import VaultCard, VaultContact, VaultLogin

logger = get_logger(__name__)

_UNSET: object = object()

_MAX_SITE = 512
_MAX_USERNAME = 256
_MAX_SECRET = 4096
_MAX_NOTES = 4000
_MAX_LABEL = 128
_MAX_BRAND = 32
_MAX_CONTACT_NAME = 128
_MAX_PHONES = 20
_MAX_EMAILS = 20
_MAX_PHONE_LENGTH = 40
_MAX_EMAIL_LENGTH = 254

_PAN_RE = re.compile(r"^\d{12,19}$")
_LAST4_RE = re.compile(r"^\d{4}$")
_CVV_RE = re.compile(r"^\d{3,4}$")


class VaultService:
    """`vault_logins` / `vault_cards` / `vault_contacts` 的唯一读写入口。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        key_vault: KeyVaultService,
    ) -> None:
        self._session_factory = session_factory
        self._key_vault = key_vault

    # --- 站点登录 ---

    async def list_logins(self) -> list[VaultLogin]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(VaultLogin).order_by(VaultLogin.created_at.asc(), VaultLogin.id.asc())
            )
            return list(rows)

    async def get_login(self, login_id: str | uuid.UUID) -> VaultLogin:
        async with self._session_factory() as session:
            return await _require(session, VaultLogin, login_id, "登录条目")

    async def create_login(
        self,
        *,
        site: str,
        username: str,
        password: str,
        notes: str = "",
    ) -> VaultLogin:
        clean_site = _require_text(site, "site", _MAX_SITE)
        clean_username = _require_text(username, "username", _MAX_USERNAME)
        secret = _require_secret(password, "password", _MAX_SECRET)
        row = VaultLogin(
            site=clean_site,
            username=clean_username,
            password_ciphertext=self._key_vault.seal_text(secret),
            notes=_clean_text(notes, "notes", _MAX_NOTES),
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            logger.info("vault.login_create id=%s", row.id)
            return row

    async def update_login(
        self,
        login_id: str | uuid.UUID,
        *,
        site: object = _UNSET,
        username: object = _UNSET,
        password: object = _UNSET,
        notes: object = _UNSET,
    ) -> VaultLogin:
        async with self._session_factory() as session:
            row = await _require(session, VaultLogin, login_id, "登录条目")
            if site is not _UNSET:
                row.site = _require_text(str(site), "site", _MAX_SITE)
            if username is not _UNSET:
                row.username = _require_text(str(username), "username", _MAX_USERNAME)
            if notes is not _UNSET:
                row.notes = _clean_text(str(notes), "notes", _MAX_NOTES)
            # 与密钥库一致：留空表示"不改密码"，避免误清空；改写走同一加密路径。
            if password is not _UNSET and str(password or ""):
                secret = _require_secret(str(password), "password", _MAX_SECRET)
                row.password_ciphertext = self._key_vault.seal_text(secret)
            await session.commit()
            await session.refresh(row)
            logger.info("vault.login_update id=%s", row.id)
            return row

    async def delete_login(self, login_id: str | uuid.UUID) -> None:
        async with self._session_factory() as session:
            row = await _require(session, VaultLogin, login_id, "登录条目")
            await session.delete(row)
            await session.commit()
            logger.info("vault.login_delete id=%s", row.id)

    async def reveal_login(self, login_id: str | uuid.UUID) -> str:
        """显式取回一次密码明文；审计日志只记 id，不记密码 / 用户名 / 站点。"""
        row = await self.get_login(login_id)
        secret = self._key_vault.open_text(row.password_ciphertext)
        logger.info("vault.login_reveal id=%s", row.id)
        return secret

    # --- 支付卡 ---

    async def list_cards(self) -> list[VaultCard]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(VaultCard).order_by(VaultCard.created_at.asc(), VaultCard.id.asc())
            )
            return list(rows)

    async def get_card(self, card_id: str | uuid.UUID) -> VaultCard:
        async with self._session_factory() as session:
            return await _require(session, VaultCard, card_id, "银行卡")

    async def create_card(
        self,
        *,
        label: str = "",
        brand: str = "",
        pan: str | None = None,
        cvv: str | None = None,
        last4: str | None = None,
        exp_month: int | None = None,
        exp_year: int | None = None,
        notes: str = "",
    ) -> VaultCard:
        clean_pan, final_last4 = _resolve_card_identity(pan, last4)
        clean_cvv = _normalize_cvv(cvv) if str(cvv or "").strip() else None
        month, year = _validate_exp(exp_month, exp_year)
        clean_brand = (
            _clean_text(brand, "brand", _MAX_BRAND).lower() if str(brand or "").strip() else ""
        )
        if not clean_brand and clean_pan:
            clean_brand = _brand_from_pan(clean_pan)
        row = VaultCard(
            label=_clean_text(label, "label", _MAX_LABEL),
            brand=clean_brand,
            last4=final_last4,
            exp_month=month,
            exp_year=year,
            secret_ciphertext=self._seal_card_secret(clean_pan, clean_cvv),
            has_pan=bool(clean_pan),
            has_cvv=bool(clean_cvv),
            notes=_clean_text(notes, "notes", _MAX_NOTES),
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            logger.info("vault.card_create id=%s last4=%s", row.id, row.last4)
            return row

    async def update_card(
        self,
        card_id: str | uuid.UUID,
        *,
        label: object = _UNSET,
        brand: object = _UNSET,
        pan: object = _UNSET,
        cvv: object = _UNSET,
        last4: object = _UNSET,
        exp_month: object = _UNSET,
        exp_year: object = _UNSET,
        notes: object = _UNSET,
    ) -> VaultCard:
        async with self._session_factory() as session:
            row = await _require(session, VaultCard, card_id, "银行卡")
            pan_changed = pan is not _UNSET and str(pan or "").strip() != ""
            cvv_changed = cvv is not _UNSET and str(cvv or "").strip() != ""
            clean_pan = _normalize_pan(str(pan)) if pan_changed else None
            clean_cvv = _normalize_cvv(str(cvv)) if cvv_changed else None
            explicit_last4 = (
                _normalize_last4(str(last4))
                if last4 is not _UNSET and str(last4 or "").strip()
                else None
            )

            if pan_changed:
                assert clean_pan is not None
                derived = clean_pan[-4:]
                if explicit_last4 is not None and explicit_last4 != derived:
                    raise BadRequestError("last4 与完整卡号不一致", details={"field": "last4"})
                row.last4 = derived
                if brand is _UNSET or not str(brand or "").strip():
                    row.brand = _brand_from_pan(clean_pan)
            elif explicit_last4 is not None:
                # 已存完整卡号时 last4 必须由卡号派生，避免展示值与密文里的卡号脱节。
                if row.has_pan:
                    raise BadRequestError(
                        "已保存完整卡号的卡片不能单独改 last4，请同时提供完整卡号",
                        details={"field": "last4"},
                    )
                row.last4 = explicit_last4

            if pan_changed or cvv_changed:
                payload = self._open_card_secret(row.secret_ciphertext)
                if pan_changed:
                    payload["pan"] = clean_pan or ""
                if cvv_changed:
                    payload["cvv"] = clean_cvv or ""
                pan_value = str(payload.get("pan") or "")
                cvv_value = str(payload.get("cvv") or "")
                row.secret_ciphertext = self._seal_card_secret(pan_value, cvv_value)
                row.has_pan = bool(pan_value)
                row.has_cvv = bool(cvv_value)

            if brand is not _UNSET and str(brand or "").strip():
                row.brand = _clean_text(brand, "brand", _MAX_BRAND).lower()
            if label is not _UNSET:
                row.label = _clean_text(str(label), "label", _MAX_LABEL)
            if notes is not _UNSET:
                row.notes = _clean_text(str(notes), "notes", _MAX_NOTES)
            if exp_month is not _UNSET or exp_year is not _UNSET:
                month = row.exp_month if exp_month is _UNSET else exp_month
                year = row.exp_year if exp_year is _UNSET else exp_year
                row.exp_month, row.exp_year = _validate_exp(month, year)  # type: ignore[arg-type]

            await session.commit()
            await session.refresh(row)
            logger.info("vault.card_update id=%s last4=%s", row.id, row.last4)
            return row

    async def delete_card(self, card_id: str | uuid.UUID) -> None:
        async with self._session_factory() as session:
            row = await _require(session, VaultCard, card_id, "银行卡")
            await session.delete(row)
            await session.commit()
            logger.info("vault.card_delete id=%s last4=%s", row.id, row.last4)

    # --- 联系人 ---

    async def list_contacts(self) -> list[VaultContact]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(VaultContact).order_by(VaultContact.created_at.asc(), VaultContact.id.asc())
            )
            return list(rows)

    async def get_contact(self, contact_id: str | uuid.UUID) -> VaultContact:
        async with self._session_factory() as session:
            return await _require(session, VaultContact, contact_id, "联系人")

    async def create_contact(
        self,
        *,
        name: str,
        phones: list[str] | None = None,
        emails: list[str] | None = None,
        notes: str = "",
    ) -> VaultContact:
        row = VaultContact(
            name=_require_text(name, "name", _MAX_CONTACT_NAME),
            phones=_clean_string_list(
                phones or [], "phones", max_items=_MAX_PHONES, max_length=_MAX_PHONE_LENGTH
            ),
            emails=_clean_string_list(
                emails or [], "emails", max_items=_MAX_EMAILS, max_length=_MAX_EMAIL_LENGTH
            ),
            notes=_clean_text(notes, "notes", _MAX_NOTES),
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            # 联系人属 PII：只记 id，不倾倒姓名 / 电话 / 邮箱。
            logger.info("vault.contact_create id=%s", row.id)
            return row

    async def update_contact(
        self,
        contact_id: str | uuid.UUID,
        *,
        name: object = _UNSET,
        phones: object = _UNSET,
        emails: object = _UNSET,
        notes: object = _UNSET,
    ) -> VaultContact:
        async with self._session_factory() as session:
            row = await _require(session, VaultContact, contact_id, "联系人")
            if name is not _UNSET:
                row.name = _require_text(str(name), "name", _MAX_CONTACT_NAME)
            if phones is not _UNSET:
                row.phones = _clean_string_list(
                    phones or [],  # type: ignore[arg-type]
                    "phones",
                    max_items=_MAX_PHONES,
                    max_length=_MAX_PHONE_LENGTH,
                )
            if emails is not _UNSET:
                row.emails = _clean_string_list(
                    emails or [],  # type: ignore[arg-type]
                    "emails",
                    max_items=_MAX_EMAILS,
                    max_length=_MAX_EMAIL_LENGTH,
                )
            if notes is not _UNSET:
                row.notes = _clean_text(str(notes), "notes", _MAX_NOTES)
            await session.commit()
            await session.refresh(row)
            logger.info("vault.contact_update id=%s", row.id)
            return row

    async def delete_contact(self, contact_id: str | uuid.UUID) -> None:
        async with self._session_factory() as session:
            row = await _require(session, VaultContact, contact_id, "联系人")
            await session.delete(row)
            await session.commit()
            logger.info("vault.contact_delete id=%s", row.id)

    # --- 加密 ---

    def _seal_card_secret(self, pan: str | None, cvv: str | None) -> bytes | None:
        if not pan and not cvv:
            return None
        payload = json.dumps({"pan": pan or "", "cvv": cvv or ""}, separators=(",", ":"))
        return self._key_vault.seal_text(payload)

    def _open_card_secret(self, ciphertext: bytes | None) -> dict[str, str]:
        if not ciphertext:
            return {"pan": "", "cvv": ""}
        raw = self._key_vault.open_text(bytes(ciphertext))
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigurationError("保险库密文损坏，无法读取") from exc
        if not isinstance(data, dict):
            raise ConfigurationError("保险库密文格式不合法")
        return {"pan": str(data.get("pan") or ""), "cvv": str(data.get("cvv") or "")}


async def _require(
    session: AsyncSession,
    model: type[VaultLogin] | type[VaultCard] | type[VaultContact],
    row_id: str | uuid.UUID,
    label: str,
) -> Any:
    row_uuid = _parse_uuid(row_id, label)
    row = await session.get(model, row_uuid)
    if row is None:
        raise NotFoundError(f"{label} {row_id} 不存在", details={"id": str(row_id)})
    return row


def _parse_uuid(value: str | uuid.UUID, label: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError) as exc:
        raise BadRequestError(f"非法的{label} id：{value!r}", details={"id": str(value)}) from exc


def _require_text(raw: str, field: str, max_length: int) -> str:
    value = (raw or "").strip()
    if not value:
        raise BadRequestError(f"{field} 不能为空", details={"field": field})
    if len(value) > max_length:
        raise BadRequestError(f"{field} 过长（最多 {max_length} 字符）", details={"field": field})
    return value


def _require_secret(raw: str, field: str, max_length: int) -> str:
    value = raw or ""
    if not value:
        raise BadRequestError(f"{field} 不能为空", details={"field": field})
    if len(value) > max_length:
        raise BadRequestError(f"{field} 过长（最多 {max_length} 字符）", details={"field": field})
    return value


def _clean_text(raw: str, field: str, max_length: int) -> str:
    value = "" if raw is None else str(raw).strip()
    if len(value) > max_length:
        raise BadRequestError(f"{field} 过长（最多 {max_length} 字符）", details={"field": field})
    return value


def _clean_string_list(values: Any, field: str, *, max_items: int, max_length: int) -> list[str]:
    if not isinstance(values, list):
        raise BadRequestError(f"{field} 必须是字符串数组", details={"field": field})
    cleaned: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > max_length:
            raise BadRequestError(
                f"{field} 中的条目过长（最多 {max_length} 字符）",
                details={"field": field},
            )
        if text not in cleaned:
            cleaned.append(text)
    if len(cleaned) > max_items:
        raise BadRequestError(f"{field} 最多 {max_items} 条", details={"field": field})
    return cleaned


def _normalize_pan(raw: str) -> str:
    """去掉空格 / 连字符后校验卡号长度；明文只在本函数与调用栈内存在。"""
    value = re.sub(r"[\s-]+", "", str(raw or ""))
    if not _PAN_RE.fullmatch(value):
        raise BadRequestError("卡号必须是 12–19 位数字", details={"field": "pan"})
    return value


def _normalize_last4(raw: str) -> str:
    value = str(raw or "").strip()
    if not _LAST4_RE.fullmatch(value):
        raise BadRequestError("last4 必须是 4 位数字", details={"field": "last4"})
    return value


def _normalize_cvv(raw: str) -> str:
    value = str(raw or "").strip()
    if not _CVV_RE.fullmatch(value):
        raise BadRequestError("CVV 必须是 3–4 位数字", details={"field": "cvv"})
    return value


def _resolve_card_identity(pan: str | None, last4: str | None) -> tuple[str | None, str]:
    clean_pan = _normalize_pan(pan) if str(pan or "").strip() else None
    explicit_last4 = _normalize_last4(last4) if str(last4 or "").strip() else None
    if clean_pan is not None:
        derived = clean_pan[-4:]
        if explicit_last4 is not None and explicit_last4 != derived:
            raise BadRequestError("last4 与完整卡号不一致", details={"field": "last4"})
        return clean_pan, derived
    if explicit_last4 is None:
        raise BadRequestError("至少提供完整卡号或 last4", details={"fields": ["pan", "last4"]})
    return None, explicit_last4


def _validate_exp(month: Any, year: Any) -> tuple[int | None, int | None]:
    if month is None and year is None:
        return None, None
    if month is None or year is None:
        raise BadRequestError(
            "有效期必须同时提供月份与年份", details={"fields": ["exp_month", "exp_year"]}
        )
    try:
        month_value = int(month)
        year_value = int(year)
    except (TypeError, ValueError) as exc:
        raise BadRequestError(
            "有效期必须是整数", details={"fields": ["exp_month", "exp_year"]}
        ) from exc
    if not 1 <= month_value <= 12:
        raise BadRequestError("exp_month 必须在 1–12 之间", details={"field": "exp_month"})
    if not 2000 <= year_value <= 2100:
        raise BadRequestError("exp_year 必须在 2000–2100 之间", details={"field": "exp_year"})
    return month_value, year_value


def _brand_from_pan(pan: str) -> str:
    """按 IIN 前缀给出展示品牌；识别不了就留空，不影响存取。"""
    prefix2 = pan[:2]
    prefix4 = pan[:4]
    if prefix2 == "34" or prefix2 == "37":
        return "amex"
    if prefix2 == "35":
        return "jcb"
    if prefix2 in {"30", "36", "38", "39"}:
        return "diners"
    if prefix2 == "62":
        return "unionpay"
    if pan.startswith("4"):
        return "visa"
    if prefix2 in {"51", "52", "53", "54", "55"}:
        return "mastercard"
    try:
        if 2221 <= int(prefix4) <= 2720:
            return "mastercard"
    except ValueError:  # pragma: no cover - _PAN_RE 已保证是数字
        pass
    if (
        prefix4 == "6011"
        or prefix2 == "65"
        or pan[:3] in {"644", "645", "646", "647", "648", "649"}
    ):
        return "discover"
    return ""


__all__ = ["VaultService"]
