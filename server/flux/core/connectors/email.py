"""账号侧 email 连接器：IMAP 读取邮件 + SMTP 发送邮件。

只使用标准库（`imaplib` / `smtplib` / `email` / `ssl`），不新增运行时依赖。
IMAP/SMTP 的连接参数与邮箱密码组成一份 JSON 凭据文档，用密钥库的 SecretBox
加密后落在 `connector_installations.credential_ciphertext`；接口与日志只出现
用户名（邮箱地址），密码永不进入明文列、响应或日志。

所有阻塞的网络调用都通过 `asyncio.to_thread` 执行，并设置 15 秒超时，
避免请求或测试悬挂。
"""

from __future__ import annotations

import asyncio
import contextlib
import email as email_package
import email.policy
import imaplib
import json
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.core.key_vault.service import KeyVaultService
from flux.errors import ConfigurationError, NotFoundError, ProviderError, ValidationError
from flux.logging import get_logger
from flux.models.connector_installation import ConnectorInstallation

logger = get_logger(__name__)

CONNECTOR_ID = "email"
CONNECTOR_NAME = "Email (IMAP/SMTP)"
DESCRIPTION = "用 IMAP 读取邮箱、SMTP 发送邮件；每个账号使用独立的邮箱地址与应用密码"
SCOPES = ("mail.read", "mail.send")

#: 网络超时（秒）：服务端与测试都不允许无限等待
NETWORK_TIMEOUT_SECONDS = 15
DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 50
MAX_BODY_BYTES = 256 * 1024
MAX_RECIPIENTS = 50
MAX_SUBJECT_CHARS = 998
MAX_MAILBOX_CHARS = 255

DEFAULT_IMAP_PORT = 993
DEFAULT_SMTP_PORT = 587
DEFAULT_SMTP_SSL_PORT = 465


# --- 凭据文档 ---------------------------------------------------------------


def prepare_credentials(api_key: str | None, settings: dict[str, Any] | None) -> dict[str, Any]:
    """校验安装参数并返回待加密的凭据文档。

    密码可以放在 `api_key`，也可以放在 `settings.password`；两者同时给出时
    必须完全一致（否则 422），避免客户端以为改了密码却没生效。
    """
    raw = dict(settings or {})
    api_secret = api_key or ""
    settings_secret = raw.get("password")
    settings_password = "" if settings_secret is None else str(settings_secret)
    if api_secret.strip() and settings_password.strip() and api_secret != settings_password:
        raise ValidationError(
            "api_key 与 settings.password 不一致，请只提供其中一个或保持一致",
            details={"field": "password", "connector": CONNECTOR_ID},
        )
    password = settings_password if settings_password.strip() else api_secret
    if not password.strip():
        raise ValidationError(
            "安装 email 连接器需要邮箱密码或应用专用密码",
            details={"field": "password", "connector": CONNECTOR_ID},
        )

    username = _clean_text(raw.get("username"), "username", max_chars=320)
    if not username:
        raise ValidationError(
            "安装 email 连接器需要提供邮箱用户名",
            details={"field": "username", "connector": CONNECTOR_ID},
        )

    imap_host = _clean_host(raw.get("imap_host"), "imap_host")
    smtp_host = _clean_host(raw.get("smtp_host"), "smtp_host")
    if not imap_host and not smtp_host:
        raise ValidationError(
            "至少需要配置 imap_host（收信）或 smtp_host（发信）",
            details={"field": "imap_host", "connector": CONNECTOR_ID},
        )

    smtp_ssl = _parse_bool(raw.get("smtp_ssl"), False, "smtp_ssl")
    # smtp_ssl 与 starttls 互斥；用户只写 smtp_ssl=true 时自动关掉默认的 starttls
    if smtp_ssl and "smtp_starttls" not in raw:
        smtp_starttls = False
    else:
        smtp_starttls = _parse_bool(raw.get("smtp_starttls"), True, "smtp_starttls")
    if smtp_ssl and smtp_starttls:
        raise ValidationError(
            "smtp_ssl 与 smtp_starttls 不能同时开启",
            details={"field": "smtp_starttls", "connector": CONNECTOR_ID},
        )

    return {
        "imap_host": imap_host,
        "imap_port": _parse_port(
            raw.get("imap_port"),
            DEFAULT_IMAP_PORT,
            "imap_port",
            default_when_missing=bool(imap_host),
        ),
        "imap_ssl": _parse_bool(raw.get("imap_ssl"), True, "imap_ssl"),
        "smtp_host": smtp_host,
        "smtp_port": _parse_port(
            raw.get("smtp_port"),
            DEFAULT_SMTP_SSL_PORT if smtp_ssl else DEFAULT_SMTP_PORT,
            "smtp_port",
            default_when_missing=bool(smtp_host),
        ),
        "smtp_ssl": smtp_ssl,
        "smtp_starttls": smtp_starttls,
        "username": username,
        "password": password,
    }


def decode_credentials(key_vault: KeyVaultService, ciphertext: bytes) -> dict[str, Any]:
    """解密并校验凭据文档；任何异常都 fail-closed 成 503。"""
    try:
        document = json.loads(key_vault.open_text(ciphertext))
    except ConfigurationError:
        raise
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ConfigurationError(
            "email 连接器凭据格式已损坏，请重新安装",
            details={"connector": CONNECTOR_ID},
        ) from exc
    if not isinstance(document, dict):
        raise ConfigurationError(
            "email 连接器凭据格式已损坏，请重新安装",
            details={"connector": CONNECTOR_ID},
        )

    username = str(document.get("username") or "").strip()
    password = str(document.get("password") or "")
    if not username or not password:
        raise ConfigurationError(
            "email 连接器凭据缺少用户名或密码，请重新安装",
            details={"connector": CONNECTOR_ID},
        )
    return {
        "imap_host": _credential_str(document, "imap_host"),
        "imap_port": _credential_port(document, "imap_port", DEFAULT_IMAP_PORT),
        "imap_ssl": _credential_bool(document, "imap_ssl", True),
        "smtp_host": _credential_str(document, "smtp_host"),
        "smtp_port": _credential_port(document, "smtp_port", DEFAULT_SMTP_PORT),
        "smtp_ssl": _credential_bool(document, "smtp_ssl", False),
        "smtp_starttls": _credential_bool(document, "smtp_starttls", True),
        "username": username,
        "password": password,
    }


def account_label(credentials: dict[str, Any]) -> str:
    """列表状态里展示的账号名：用户名（通常是邮箱地址），绝不是密码。"""
    return str(credentials.get("username") or "").strip()


# --- 服务 -------------------------------------------------------------------


class EmailConnectorService:
    """`connector_installations` 中 email 凭据与 IMAP/SMTP 动作的唯一入口。"""

    connector_id = CONNECTOR_ID
    connector_name = CONNECTOR_NAME
    description = DESCRIPTION
    scopes = SCOPES

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        key_vault: KeyVaultService,
    ) -> None:
        self._session_factory = session_factory
        self._key_vault = key_vault

    # --- 安装状态 ---

    async def installation(self, account_key: str) -> ConnectorInstallation | None:
        async with self._session_factory() as session:
            return (
                await session.scalars(
                    select(ConnectorInstallation).where(
                        ConnectorInstallation.account_key == account_key,
                        ConnectorInstallation.connector_id == CONNECTOR_ID,
                    )
                )
            ).first()

    async def credentials(self, account_key: str) -> dict[str, Any]:
        """按账号读取凭据；未安装 404，解密失败 503（fail-closed）。"""
        row = await self.installation(account_key)
        if row is None:
            raise NotFoundError(
                "email 连接器尚未安装",
                details={"connector": CONNECTOR_ID},
            )
        if row.credential_ciphertext is None:
            raise ConfigurationError(
                "email 连接器缺少凭据，请重新安装",
                details={"connector": CONNECTOR_ID},
            )
        return decode_credentials(self._key_vault, row.credential_ciphertext)

    async def public_label(self, account_key: str) -> str:
        """列表展示用的账号名；读不到凭据时只留空，不让整个列表失败。"""
        row = await self.installation(account_key)
        if row is None or row.credential_ciphertext is None:
            return ""
        return await self.label_for_ciphertext(row.credential_ciphertext)

    async def label_for_ciphertext(self, ciphertext: bytes) -> str:
        return await asyncio.to_thread(self._label_or_empty, ciphertext)

    def _label_or_empty(self, ciphertext: bytes) -> str:
        try:
            return account_label(decode_credentials(self._key_vault, ciphertext))
        except ConfigurationError:
            logger.warning("email.state credentials_unreadable connector=%s", CONNECTOR_ID)
            return ""

    # --- 读信 ---

    async def list_messages(
        self,
        account_key: str,
        *,
        mailbox: str = "INBOX",
        limit: int = DEFAULT_LIST_LIMIT,
        since_uid: int | None = None,
    ) -> list[dict[str, Any]]:
        """列出邮件头。

        默认（`since_uid=None`）保持原有语义：最新的 `limit` 封，新的在前。
        传 `since_uid` 时改为「UID 严格大于水位线、从最老的开始取 `limit` 封」，
        让入站轮询的水位线连续前进：窗口满时剩下的更晚邮件留到下一轮，
        不会因为只取最新 N 封而跳过中间的新邮件。
        """
        credentials = await self._credentials_for(account_key, "imap")
        clean_mailbox = _clean_mailbox(mailbox)
        clean_limit = max(1, min(int(limit), MAX_LIST_LIMIT))
        clean_since: int | None = None
        if since_uid is not None:
            clean_since = max(0, int(since_uid))
        try:
            messages = await asyncio.to_thread(
                self._list_sync, credentials, clean_mailbox, clean_limit, clean_since
            )
        except Exception as exc:
            self._log_protocol_failure("list", account_key, exc)
            raise ProviderError(
                "IMAP 读取失败，请检查服务器地址、端口与账号设置",
                details={"connector": CONNECTOR_ID, "protocol": "imap"},
            ) from exc
        logger.info(
            "email.list account=%s mailbox=%s limit=%d since_uid=%s count=%d",
            account_key,
            clean_mailbox,
            clean_limit,
            clean_since,
            len(messages),
        )
        return messages

    async def get_message(
        self,
        account_key: str,
        message_id: str,
        *,
        mailbox: str = "INBOX",
    ) -> dict[str, Any]:
        credentials = await self._credentials_for(account_key, "imap")
        uid = _clean_uid(message_id)
        clean_mailbox = _clean_mailbox(mailbox)
        try:
            message = await asyncio.to_thread(self._get_sync, credentials, clean_mailbox, uid)
        except Exception as exc:
            self._log_protocol_failure("get", account_key, exc)
            raise ProviderError(
                "IMAP 读取失败，请检查服务器地址、端口与账号设置",
                details={"connector": CONNECTOR_ID, "protocol": "imap"},
            ) from exc
        logger.info(
            "email.get account=%s uid=%s body_bytes=%d content_type=%s",
            account_key,
            uid.decode("ascii", "ignore"),
            len(message["body"].encode("utf-8")),
            message["content_type"],
        )
        return message

    # --- 发信 ---

    async def send_message(
        self,
        account_key: str,
        *,
        to: str | list[str] | None,
        cc: str | list[str] | None = None,
        subject: str,
        body: str,
        content_type: str = "text/plain",
    ) -> dict[str, Any]:
        credentials = await self._credentials_for(account_key, "smtp")
        recipients_to = _normalize_recipients(to, "to")
        recipients_cc = _normalize_recipients(cc, "cc")
        if not recipients_to:
            raise ValidationError("至少需要一个收件人", details={"field": "to"})
        if len(recipients_to) + len(recipients_cc) > MAX_RECIPIENTS:
            raise ValidationError(
                f"收件人最多 {MAX_RECIPIENTS} 个",
                details={"field": "to"},
            )
        clean_subject = str(subject or "")
        if not clean_subject.strip():
            raise ValidationError("邮件主题不能为空", details={"field": "subject"})
        if len(clean_subject) > MAX_SUBJECT_CHARS:
            raise ValidationError(
                f"邮件主题最多 {MAX_SUBJECT_CHARS} 个字符",
                details={"field": "subject"},
            )
        if "\r" in clean_subject or "\n" in clean_subject:
            raise ValidationError("邮件主题不能包含换行", details={"field": "subject"})
        clean_type = (content_type or "text/plain").strip().lower()
        if clean_type not in ("text/plain", "text/html"):
            raise ValidationError(
                "content_type 只能是 text/plain 或 text/html",
                details={"field": "content_type"},
            )
        clean_body = str(body or "")
        if len(clean_body.encode("utf-8")) > MAX_BODY_BYTES:
            raise ValidationError(
                f"邮件正文最多 {MAX_BODY_BYTES} 字节",
                details={"field": "body"},
            )

        message = EmailMessage()
        message["From"] = credentials["username"]
        message["To"] = ", ".join(recipients_to)
        if recipients_cc:
            message["Cc"] = ", ".join(recipients_cc)
        message["Subject"] = clean_subject
        message["Message-ID"] = make_msgid()
        if clean_type == "text/html":
            message.set_content(clean_body, subtype="html")
        else:
            message.set_content(clean_body)

        try:
            await asyncio.to_thread(self._send_sync, credentials, message)
        except Exception as exc:
            self._log_protocol_failure("send", account_key, exc)
            raise ProviderError(
                "SMTP 发送失败，请检查服务器地址、端口与账号设置",
                details={"connector": CONNECTOR_ID, "protocol": "smtp"},
            ) from exc

        message_id = str(message["Message-ID"])
        recipient_count = len(recipients_to) + len(recipients_cc)
        # 日志只记 message-id / 收件人数 / 主题长度，绝不记正文或密码
        logger.info(
            "email.send account=%s recipients=%d subject_len=%d message_id=%s",
            account_key,
            recipient_count,
            len(clean_subject),
            message_id,
        )
        return {
            "sent": True,
            "message_id": message_id,
            "to": recipients_to,
            "cc": recipients_cc,
            "recipients": recipient_count,
        }

    # --- 连通性自检 ---

    async def test_connection(self, account_key: str) -> dict[str, Any]:
        """只登录并 NOOP，不发送任何真实邮件。"""
        credentials = await self.credentials(account_key)
        result = await asyncio.to_thread(self._test_sync, credentials, account_key)
        logger.info(
            "email.test account=%s imap=%s smtp=%s",
            account_key,
            result["imap"]["ok"],
            result["smtp"]["ok"],
        )
        return result

    # --- 内部：账号校验 ---

    async def _credentials_for(self, account_key: str, side: str) -> dict[str, Any]:
        credentials = await self.credentials(account_key)
        host_key = "imap_host" if side == "imap" else "smtp_host"
        if not credentials.get(host_key):
            protocol = "IMAP" if side == "imap" else "SMTP"
            action = "收信" if side == "imap" else "发信"
            raise ValidationError(
                f"该 email 连接器未配置 {protocol} 主机，无法{action}",
                details={"connector": CONNECTOR_ID, "field": host_key},
            )
        return credentials

    # --- 内部：同步 IMAP ---

    def _imap_open(self, credentials: dict[str, Any]) -> imaplib.IMAP4:
        host = credentials["imap_host"]
        port = int(credentials["imap_port"])
        context = ssl.create_default_context()
        if credentials["imap_ssl"]:
            connection = imaplib.IMAP4_SSL(
                host,
                port,
                timeout=NETWORK_TIMEOUT_SECONDS,
                ssl_context=context,
            )
        else:
            connection = imaplib.IMAP4(host, port, timeout=NETWORK_TIMEOUT_SECONDS)
        try:
            connection.login(credentials["username"], credentials["password"])
        except Exception:
            _imap_close_quietly(connection)
            raise
        return connection

    def _list_sync(
        self,
        credentials: dict[str, Any],
        mailbox: str,
        limit: int,
        since_uid: int | None = None,
    ) -> list[dict[str, Any]]:
        connection = self._imap_open(credentials)
        try:
            status, _ = connection.select(mailbox, readonly=True)
            if status != "OK":
                raise _ProtocolFailure("IMAP SELECT failed")
            status, data = connection.uid("search", None, "ALL")
            if status != "OK":
                raise _ProtocolFailure("IMAP SEARCH failed")
            raw_uids = (data[0] if data else b"") or b""
            uids = [item for item in raw_uids.split() if item.isdigit()]
            if since_uid is None:
                selected = list(reversed(uids[-limit:]))
            else:
                # 水位线之后按 UID 升序取最老的 limit 封，保证下一轮从连续的位置继续。
                newer = sorted((uid for uid in uids if int(uid) > since_uid), key=int)
                selected = newer[:limit]
            summaries: list[dict[str, Any]] = []
            for uid in selected:
                raw, seen = self._fetch(connection, uid, b"(FLAGS BODY.PEEK[HEADER])")
                message = email_package.message_from_bytes(raw, policy=email.policy.default)
                summaries.append(_summary(uid, message, seen))
            return summaries
        finally:
            _imap_close_quietly(connection)

    def _get_sync(
        self,
        credentials: dict[str, Any],
        mailbox: str,
        uid: bytes,
    ) -> dict[str, Any]:
        connection = self._imap_open(credentials)
        try:
            status, _ = connection.select(mailbox, readonly=True)
            if status != "OK":
                raise _ProtocolFailure("IMAP SELECT failed")
            raw, seen = self._fetch(connection, uid, b"(FLAGS BODY.PEEK[])")
            message = email_package.message_from_bytes(raw, policy=email.policy.default)
            body, content_type, truncated = _extract_body(message)
            result = _summary(uid, message, seen)
            result.update(
                {
                    "body": body,
                    "content_type": content_type,
                    "body_truncated": truncated,
                }
            )
            return result
        finally:
            _imap_close_quietly(connection)

    @staticmethod
    def _fetch(
        connection: imaplib.IMAP4,
        uid: bytes,
        query: bytes,
    ) -> tuple[bytes, bool]:
        status, data = connection.uid("fetch", uid, query)
        if status != "OK" or not data:
            raise _ProtocolFailure("IMAP FETCH failed")
        raw = b""
        meta = b""
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2:
                meta = item[0] or b""
                raw = item[1] or b""
                break
        if not raw:
            raise _ProtocolFailure("IMAP FETCH returned no body")
        return raw, b"\\Seen" in meta

    # --- 内部：同步 SMTP ---

    def _smtp_open(self, credentials: dict[str, Any]) -> smtplib.SMTP:
        host = credentials["smtp_host"]
        port = int(credentials["smtp_port"])
        context = ssl.create_default_context()
        if credentials["smtp_ssl"]:
            connection = smtplib.SMTP_SSL(
                host,
                port,
                timeout=NETWORK_TIMEOUT_SECONDS,
                context=context,
            )
        else:
            connection = smtplib.SMTP(host, port, timeout=NETWORK_TIMEOUT_SECONDS)
        try:
            connection.ehlo()
            if not credentials["smtp_ssl"] and credentials["smtp_starttls"]:
                connection.starttls(context=context)
                connection.ehlo()
            connection.login(credentials["username"], credentials["password"])
        except Exception:
            _smtp_close_quietly(connection)
            raise
        return connection

    def _send_sync(self, credentials: dict[str, Any], message: EmailMessage) -> None:
        connection = self._smtp_open(credentials)
        try:
            connection.send_message(message)
        finally:
            _smtp_close_quietly(connection)

    # --- 内部：自检与日志 ---

    def _test_sync(self, credentials: dict[str, Any], account_key: str) -> dict[str, Any]:
        imap_configured = bool(credentials["imap_host"])
        smtp_configured = bool(credentials["smtp_host"])
        result: dict[str, Any] = {
            "imap": {"configured": imap_configured, "ok": False, "error": ""},
            "smtp": {"configured": smtp_configured, "ok": False, "error": ""},
        }
        if imap_configured:
            try:
                connection = self._imap_open(credentials)
                try:
                    connection.noop()
                finally:
                    _imap_close_quietly(connection)
                result["imap"]["ok"] = True
            except Exception as exc:
                result["imap"]["error"] = "IMAP 连接或登录失败，请检查主机、端口与凭据"
                self._log_protocol_failure("test_imap", account_key, exc)
        if smtp_configured:
            try:
                connection = self._smtp_open(credentials)
                try:
                    connection.noop()
                finally:
                    _smtp_close_quietly(connection)
                result["smtp"]["ok"] = True
            except Exception as exc:
                result["smtp"]["error"] = "SMTP 连接或登录失败，请检查主机、端口与凭据"
                self._log_protocol_failure("test_smtp", account_key, exc)

        configured_sides: list[dict[str, Any]] = []
        if result["imap"]["configured"]:
            configured_sides.append(result["imap"])
        if result["smtp"]["configured"]:
            configured_sides.append(result["smtp"])
        result["ok"] = bool(configured_sides) and all(side["ok"] for side in configured_sides)
        return result

    @staticmethod
    def _log_protocol_failure(action: str, account_key: str, exc: Exception) -> None:
        # 只记录异常类型：IMAP/SMTP 的原始文本可能包含服务器应答，不能整体落日志
        logger.warning(
            "email.protocol_failure action=%s account=%s error=%s",
            action,
            account_key,
            type(exc).__name__,
        )


# --- 工具函数 ---------------------------------------------------------------


class _ProtocolFailure(Exception):
    """内部标记：IMAP 返回了非 OK 状态，不携带服务器原文。"""


def _summary(uid: bytes, message: Any, seen: bool) -> dict[str, Any]:
    return {
        "id": uid.decode("ascii", "ignore"),
        "uid": int(uid),
        "from": _header(message, "From"),
        "to": _header(message, "To"),
        "subject": _header(message, "Subject"),
        "date": _header(message, "Date"),
        "seen": bool(seen),
    }


def _header(message: Any, name: str) -> str:
    try:
        value = message.get(name)
    except Exception:  # noqa: BLE001 - 畸形邮件头不能拖垮整页列表
        return ""
    if value is None:
        return ""
    return str(value).strip()


def _extract_body(message: Any) -> tuple[str, str, bool]:
    """优先 text/plain；只有 HTML 时返回 HTML 并明确告知调用方。"""
    for wanted in ("text/plain", "text/html"):
        for part in message.walk():
            if part.get_content_type() != wanted:
                continue
            if part.get_content_disposition() == "attachment":
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            truncated = len(payload) > MAX_BODY_BYTES
            if truncated:
                payload = payload[:MAX_BODY_BYTES]
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
            return text, wanted, truncated
    return "", "text/plain", False


def _imap_close_quietly(connection: imaplib.IMAP4) -> None:
    try:
        connection.logout()
    except Exception:  # noqa: BLE001 - 连接已断开时 logout 会抛错
        with contextlib.suppress(Exception):
            connection.close()


def _smtp_close_quietly(connection: smtplib.SMTP) -> None:
    try:
        connection.quit()
    except Exception:  # noqa: BLE001 - 连接已断开时 quit 会抛错
        with contextlib.suppress(Exception):
            connection.close()


def _clean_text(value: Any, field: str, *, max_chars: int) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > max_chars:
        raise ValidationError(
            f"{field} 过长（最多 {max_chars} 字符）",
            details={"field": field},
        )
    if any(character in text for character in ("\r", "\n", "\x00")):
        raise ValidationError(
            f"{field} 不能包含换行或控制字符",
            details={"field": field},
        )
    return text


def _clean_host(value: Any, field: str) -> str:
    host = _clean_text(value, field, max_chars=255)
    if not host:
        return ""
    if any(character.isspace() for character in host) or "/" in host:
        raise ValidationError(
            "主机名不合法：不能包含空白或路径",
            details={"field": field},
        )
    return host


def _clean_mailbox(value: Any) -> str:
    mailbox = _clean_text(value, "mailbox", max_chars=MAX_MAILBOX_CHARS)
    if not mailbox:
        raise ValidationError("邮箱目录不能为空", details={"field": "mailbox"})
    # imaplib.select 不替参数做引号转义，目录名里不能出现引号或反斜杠
    if any(character in mailbox for character in ('"', "\\")):
        raise ValidationError(
            "邮箱目录不能包含引号或反斜杠",
            details={"field": "mailbox"},
        )
    return mailbox


def _clean_uid(value: Any) -> bytes:
    text = str(value or "").strip()
    if not text.isdigit():
        raise ValidationError(
            "邮件 id 不合法",
            details={"field": "id"},
        )
    return text.encode("ascii")


def _normalize_recipients(value: str | list[str] | None, field: str) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    recipients: list[str] = []
    for item in items:
        for chunk in str(item).split(","):
            address = chunk.strip()
            if not address:
                continue
            if not _valid_address(address):
                raise ValidationError(
                    "收件人地址不合法",
                    details={"field": field},
                )
            if address not in recipients:
                recipients.append(address)
    return recipients


def _valid_address(value: str) -> bool:
    if any(character in value for character in ("\r", "\n", "\x00")):
        return False
    if any(character.isspace() for character in value):
        return False
    local, _, domain = value.partition("@")
    return bool(local and domain and "@" not in domain and "." in domain)


def _parse_bool(value: Any, default: bool, field: str) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        raise ValidationError(f"{field} 必须是布尔值", details={"field": field})
    normalized = str(value).strip().lower()
    if normalized in ("true", "1", "yes", "on"):
        return True
    if normalized in ("false", "0", "no", "off"):
        return False
    raise ValidationError(f"{field} 必须是布尔值", details={"field": field})


def _parse_port(
    value: Any,
    default: int,
    field: str,
    *,
    default_when_missing: bool = True,
) -> int:
    if value is None or value == "":
        if default_when_missing:
            return default
        return 0
    if isinstance(value, bool):
        raise ValidationError(f"{field} 必须是 1~65535 的端口号", details={"field": field})
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"{field} 必须是 1~65535 的端口号",
            details={"field": field},
        ) from exc
    if not 1 <= port <= 65535:
        raise ValidationError(
            f"{field} 必须是 1~65535 的端口号",
            details={"field": field},
        )
    return port


def _credential_str(document: dict[str, Any], field: str) -> str:
    value = document.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigurationError(
            "email 连接器凭据格式已损坏，请重新安装",
            details={"connector": CONNECTOR_ID},
        )
    return value


def _credential_port(document: dict[str, Any], field: str, default: int) -> int:
    value = document.get(field)
    if value is None:
        return default
    # 0 是 prepare_credentials 对「该方向未配置」写入的哨兵值（只配 SMTP 或只配 IMAP
    # 时另一侧的端口就是 0）；真正的可用性由 _credentials_for 按 host 判定。
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65535:
        raise ConfigurationError(
            "email 连接器凭据格式已损坏，请重新安装",
            details={"connector": CONNECTOR_ID},
        )
    return value


def _credential_bool(document: dict[str, Any], field: str, default: bool) -> bool:
    value = document.get(field)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigurationError(
            "email 连接器凭据格式已损坏，请重新安装",
            details={"connector": CONNECTOR_ID},
        )
    return value


__all__ = [
    "CONNECTOR_ID",
    "EmailConnectorService",
    "account_label",
    "decode_credentials",
    "prepare_credentials",
]
