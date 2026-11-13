"""用户账号日历连接器：CalDAV 日历与已发布的 ICS 订阅。

只依赖标准库与项目已有的 httpx，不引入 caldav / icalendar 等新运行时依赖。

两种模式共用一份安装记录（`connector_installations`）：

- ``ics``：读取一个 https 的已发布 ICS 订阅，可选 Basic Auth；只读。
- ``caldav``：读取一个 https 的 CalDAV 日历集合，用户名 + 密码；可新建事件。

凭据是一个 JSON 文档（url / mode / username / password / timeout），用密钥库
`KeyVaultService.seal_text` 信封加密后写入 `credential_ciphertext`，任何接口响应
与日志都不出现密码或带 userinfo 的完整 URL。

ICS 解析器只覆盖 VEVENT 的常用字段（UID / SUMMARY / DTSTART / DTEND /
DESCRIPTION / LOCATION / STATUS / TZID），并保留 RRULE 字符串但**不展开重复规则**：
v1 只把第一条 DTSTART 作为该事件的发生时间。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.config import Settings
from flux.core.key_vault.service import KeyVaultService
from flux.errors import (
    BadRequestError,
    ConfigurationError,
    ConflictError,
    NotFoundError,
    ProviderError,
)
from flux.logging import get_logger
from flux.models.connector_installation import ConnectorInstallation

logger = get_logger(__name__)

MODE_ICS = "ics"
MODE_CALDAV = "caldav"
_MODES = (MODE_ICS, MODE_CALDAV)
_AUTH_TYPE = "api_key"

_DEFAULT_TIMEOUT_SECONDS = 15.0
_MIN_TIMEOUT_SECONDS = 1.0
_MAX_TIMEOUT_SECONDS = 60.0
_MAX_RESPONSE_BYTES = 5_000_000
_MAX_URL_CHARS = 2048
_MAX_USERNAME_CHARS = 256
_MAX_PASSWORD_CHARS = 4096
MAX_EVENT_LIMIT = 100
_DEFAULT_EVENT_LIMIT = 50
_RAW_EVENT_MAX_CHARS = 4000

_ICS_DATETIME_RE = re.compile(
    r"^(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})"
    r"(?:T(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})(?P<z>Z)?)?$"
)
_DURATION_RE = re.compile(
    r"^(?P<sign>[+-])?P"
    r"(?:(?P<weeks>\d+)W)?"
    r"(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)

_REPORT_BODY = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
    '<c:filter><c:comp-filter name="VCALENDAR">'
    '<c:comp-filter name="VEVENT"/></c:comp-filter></c:filter>'
    "</c:calendar-query>"
)
_REPORT_HEADERS = {
    "Content-Type": "application/xml; charset=utf-8",
    "Accept": "application/xml, text/calendar",
    "Depth": "1",
}
_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><d:resourcetype/><d:displayname/><c:calendar-description/></d:prop>"
    "</d:propfind>"
)
_PROPFIND_HEADERS = {
    "Content-Type": "application/xml; charset=utf-8",
    "Accept": "application/xml",
    "Depth": "0",
}


@dataclass
class CalendarEvent:
    """解析后的事件；时间统一转成 UTC 的 aware datetime。"""

    uid: str
    title: str
    start: datetime
    end: datetime
    description: str = ""
    location: str = ""
    status: str = ""
    timezone_name: str = ""
    all_day: bool = False
    rrule: str = ""
    raw: str = ""

    def to_summary(self) -> dict[str, Any]:
        """列表视图：不返回 description 与原始 ICS。"""
        return {
            "uid": self.uid,
            "title": self.title,
            "summary": self.title,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "location": self.location,
            "status": self.status,
            "all_day": self.all_day,
            "timezone": self.timezone_name,
            "recurrence": self.rrule,
        }

    def to_detail(self) -> dict[str, Any]:
        data = self.to_summary()
        data["description"] = self.description
        data["raw"] = self.raw[:_RAW_EVENT_MAX_CHARS]
        return data


class CalendarConnectorService:
    """日历连接的安装、凭据读取与 CalDAV / ICS 运行时。"""

    connector_id = "calendar"
    connector_name = "Calendar (CalDAV / ICS)"
    description = "读取已发布的 ICS 订阅或 CalDAV 日历，查看事件并在 CalDAV 上新建事件"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        key_vault: KeyVaultService,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._key_vault = key_vault
        # 测试注入 MockTransport；生产每次临时建连，避免长期持有连接池。
        self._client = client

    # --- 安装状态 ---

    async def row(self, account_key: str) -> ConnectorInstallation | None:
        async with self._session_factory() as session:
            return (
                await session.scalars(
                    select(ConnectorInstallation).where(
                        ConnectorInstallation.account_key == account_key,
                        ConnectorInstallation.connector_id == self.connector_id,
                    )
                )
            ).first()

    async def public_state(self, account_key: str) -> dict[str, Any]:
        row = await self.row(account_key)
        if row is None:
            return {
                "id": self.connector_id,
                "name": self.connector_name,
                "description": self.description,
                "connected": False,
                "account": "",
                "display_name": "",
                "avatar_url": "",
                "scopes": [],
                "connected_at": None,
                "updated_at": None,
                "mode": "",
                "host": "",
            }
        credentials = self._open_credentials(row)
        host = _url_host(str(credentials.get("url") or ""))
        # 账号名优先用用户名；公开 ICS 没有用户名时退回主机名。
        # 绝不返回完整 URL（已发布 ICS 的查询串里常带私有令牌）。
        account = str(credentials.get("username") or "").strip() or host
        return {
            "id": self.connector_id,
            "name": self.connector_name,
            "description": self.description,
            "connected": True,
            "account": account,
            "display_name": account,
            "avatar_url": "",
            "scopes": row.scope_list(),
            "connected_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "mode": str(credentials.get("mode") or ""),
            "host": host,
        }

    async def install(
        self,
        account_key: str,
        *,
        api_key: str | None,
        settings: dict[str, Any] | None,
        scopes: str,
    ) -> dict[str, Any]:
        if await self.row(account_key) is not None:
            raise ConflictError(
                "该连接器已安装；如需更换凭据请先卸载",
                details={"connector": self.connector_id},
            )
        credentials = normalize_credentials(api_key=api_key, settings=settings)
        async with self._session_factory() as session:
            session.add(
                ConnectorInstallation(
                    account_key=account_key,
                    connector_id=self.connector_id,
                    auth_type=_AUTH_TYPE,
                    scopes=scopes,
                    credential_ciphertext=self._key_vault.seal_text(
                        json.dumps(credentials, ensure_ascii=False)
                    ),
                )
            )
            await session.commit()
        logger.info(
            "connector.calendar.install account=%s mode=%s host=%s",
            account_key,
            credentials["mode"],
            _url_host(credentials["url"]),
        )
        return await self.public_state(account_key)

    # --- 运行时 ---

    async def list_events(
        self,
        account_key: str,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = _DEFAULT_EVENT_LIMIT,
    ) -> list[dict[str, Any]]:
        credentials = await self._credentials_for(account_key)
        start_dt = _parse_api_datetime(start, "start") if start else datetime.now(timezone.utc)
        end_dt = _parse_api_datetime(end, "end") if end else start_dt + timedelta(days=7)
        if end_dt <= start_dt:
            raise BadRequestError(
                "end 必须晚于 start",
                details={"connector": self.connector_id, "start": start, "end": end},
            )
        cap = max(1, min(int(limit), MAX_EVENT_LIMIT))
        events = await self._fetch_events(credentials)
        window = [event for event in events if event.start <= end_dt and event.end >= start_dt]
        window.sort(key=lambda event: (event.start, event.uid))
        return [event.to_summary() for event in window[:cap]]

    async def get_event(self, account_key: str, uid: str) -> dict[str, Any]:
        credentials = await self._credentials_for(account_key)
        wanted = (uid or "").strip()
        if not wanted:
            raise BadRequestError("事件 uid 不能为空", details={"connector": self.connector_id})
        for event in await self._fetch_events(credentials):
            if event.uid == wanted:
                return event.to_detail()
        raise NotFoundError(
            "日历中没有该事件",
            details={"connector": self.connector_id, "uid": wanted},
        )

    async def create_event(
        self,
        account_key: str,
        *,
        title: str,
        start: str,
        end: str,
        description: str = "",
        location: str = "",
    ) -> dict[str, Any]:
        credentials = await self._credentials_for(account_key)
        if credentials.get("mode") != MODE_CALDAV:
            raise BadRequestError(
                "ICS 订阅是只读的，不能新建事件；请改用 CalDAV 日历",
                details={"connector": self.connector_id, "mode": credentials.get("mode")},
            )
        clean_title = (title or "").strip()
        if not clean_title:
            raise BadRequestError("事件标题不能为空", details={"field": "title"})
        start_dt = _parse_api_datetime(start, "start")
        end_dt = _parse_api_datetime(end, "end")
        if end_dt < start_dt:
            raise BadRequestError(
                "结束时间不能早于开始时间",
                details={"connector": self.connector_id, "field": "end"},
            )

        uid = f"{uuid.uuid4().hex}@flux.local"
        body = _build_vevent(
            uid=uid,
            title=clean_title,
            start=start_dt,
            end=end_dt,
            description=description,
            location=location,
        )
        collection = str(credentials["url"]).rstrip("/")
        target = f"{collection}/{quote(uid + '.ics', safe='')}"
        headers = {
            "Content-Type": "text/calendar; charset=utf-8",
            "Accept": "text/calendar",
            "If-None-Match": "*",
        }
        response = await self._request(
            credentials, "PUT", target, headers=headers, content=body.encode("utf-8")
        )
        if response.status_code in (405, 501):
            # 少数 CalDAV 服务器只接受向集合 POST。
            post_headers = {
                "Content-Type": "text/calendar; charset=utf-8",
                "Accept": "text/calendar",
                "Slug": uid,
            }
            response = await self._request(
                credentials,
                "POST",
                collection,
                headers=post_headers,
                content=body.encode("utf-8"),
            )
        if response.status_code not in (200, 201, 204):
            _raise_upstream(response)
        logger.info(
            "connector.calendar.create account=%s uid=%s status=%s",
            account_key,
            uid,
            response.status_code,
        )
        return {
            "uid": uid,
            "title": clean_title,
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "location": (location or "").strip(),
            "status": "CONFIRMED",
        }

    async def test_connection(self, account_key: str) -> dict[str, Any]:
        credentials = await self._credentials_for(account_key)
        mode = credentials.get("mode")
        if mode == MODE_ICS:
            text = await self._fetch_ics(credentials)
            events = parse_ics(text)
            return {"ok": True, "mode": MODE_ICS, "events": len(events)}
        response = await self._request(
            credentials,
            "PROPFIND",
            str(credentials["url"]),
            headers=_PROPFIND_HEADERS,
            content=_PROPFIND_BODY.encode("utf-8"),
        )
        if response.status_code not in (200, 204, 207):
            _raise_upstream(response)
        return {"ok": True, "mode": MODE_CALDAV, "status": response.status_code}

    # --- 上游读取 ---

    async def _fetch_events(self, credentials: dict[str, Any]) -> list[CalendarEvent]:
        if credentials.get("mode") == MODE_ICS:
            text = await self._fetch_ics(credentials)
        else:
            text = await self._fetch_caldav(credentials)
        return parse_ics(text)

    async def _fetch_ics(self, credentials: dict[str, Any]) -> str:
        response = await self._request(
            credentials,
            "GET",
            str(credentials["url"]),
            headers={"Accept": "text/calendar, text/plain;q=0.9"},
        )
        _raise_upstream(response)
        return _response_text(response)

    async def _fetch_caldav(self, credentials: dict[str, Any]) -> str:
        url = str(credentials["url"])
        response = await self._request(
            credentials,
            "REPORT",
            url,
            headers=_REPORT_HEADERS,
            content=_REPORT_BODY.encode("utf-8"),
        )
        if response.status_code in (400, 405, 501):
            # 兼容不支持 REPORT 的服务器：CalDAV 集合通常也接受整份 GET。
            response = await self._request(
                credentials,
                "GET",
                url,
                headers={"Accept": "text/calendar"},
            )
        _raise_upstream(response)
        return _response_text(response)

    async def _request(
        self,
        credentials: dict[str, Any],
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        timeout = float(
            credentials.get("timeout")
            or self._settings.calendar_timeout_seconds
            or _DEFAULT_TIMEOUT_SECONDS
        )
        auth = _basic_auth(credentials)
        try:
            if self._client is not None:
                return await self._client.request(
                    method, url, headers=headers, content=content, auth=auth
                )
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
            ) as client:
                return await client.request(
                    method, url, headers=headers, content=content, auth=auth
                )
        except httpx.HTTPError as exc:
            raise ProviderError(
                "无法连接日历服务，请检查地址或网络",
                details={"connector": self.connector_id, "error": exc.__class__.__name__},
            ) from exc

    # --- 凭据 ---

    async def _credentials_for(self, account_key: str) -> dict[str, Any]:
        row = await self.row(account_key)
        if row is None:
            raise NotFoundError(
                "请先在「账户 → 连接器」安装日历连接器",
                details={"connector": self.connector_id},
            )
        return self._open_credentials(row)

    def _open_credentials(self, row: ConnectorInstallation) -> dict[str, Any]:
        ciphertext = row.credential_ciphertext
        if ciphertext is None:
            raise ConfigurationError(
                "日历连接凭据缺失，请重新安装",
                details={"connector": self.connector_id},
            )
        # 主密钥不匹配时 open_text 会抛 ConfigurationError（503，fail-closed）。
        raw = self._key_vault.open_text(ciphertext)
        try:
            document = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                "日历连接凭据已损坏，请重新安装",
                details={"connector": self.connector_id},
            ) from exc
        if not isinstance(document, dict) or not document.get("url"):
            raise ConfigurationError(
                "日历连接凭据不完整，请重新安装",
                details={"connector": self.connector_id},
            )
        return document


# --- 凭据校验 ---


def normalize_credentials(
    *, api_key: str | None, settings: dict[str, Any] | None
) -> dict[str, Any]:
    """把安装请求归一成可加密的 JSON 文档；所有校验错误都是 400。"""
    if settings is not None and not isinstance(settings, dict):
        raise BadRequestError("settings 必须是对象", details={"field": "settings"})
    options = dict(settings or {})

    raw_url = str(options.get("url") or "").strip()
    if not raw_url:
        raise BadRequestError(
            "请填写日历地址（https://…）",
            details={"connector": CalendarConnectorService.connector_id, "field": "url"},
        )
    if len(raw_url) > _MAX_URL_CHARS:
        raise BadRequestError("日历地址过长", details={"field": "url"})
    clean_url, url_user, url_password = _split_userinfo(raw_url)
    _require_https(clean_url)

    username = str(options.get("username") or "").strip() or url_user
    password = str(options.get("password") or "").strip()
    if not password:
        password = (api_key or "").strip() or url_password
    if len(username) > _MAX_USERNAME_CHARS:
        raise BadRequestError("用户名过长", details={"field": "username"})
    if len(password) > _MAX_PASSWORD_CHARS:
        raise BadRequestError("密码过长", details={"field": "password"})

    mode = str(options.get("mode") or "").strip().lower()
    if not mode:
        # 未显式指定时：给了用户名和密码就按 CalDAV，否则按只读 ICS 订阅。
        mode = MODE_CALDAV if (username and password) else MODE_ICS
    if mode not in _MODES:
        raise BadRequestError(
            "mode 只能是 ics 或 caldav",
            details={"field": "mode", "allowed": list(_MODES)},
        )
    if mode == MODE_CALDAV and (not username or not password):
        raise BadRequestError(
            "CalDAV 模式需要用户名和密码",
            details={"connector": CalendarConnectorService.connector_id, "mode": mode},
        )

    return {
        "mode": mode,
        "url": clean_url,
        "username": username,
        "password": password,
        "timeout": _normalize_timeout(options.get("timeout")),
    }


def _require_https(url: str) -> None:
    if "\n" in url or "\r" in url or " " in url or "\t" in url:
        raise BadRequestError(
            "日历地址必须是合法的 https 地址",
            details={"connector": CalendarConnectorService.connector_id, "field": "url"},
        )
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
    except ValueError as exc:
        raise BadRequestError(
            "日历地址必须是合法的 https 地址",
            details={"connector": CalendarConnectorService.connector_id, "field": "url"},
        ) from exc
    if parsed.scheme.lower() != "https" or not parsed.netloc or not host:
        raise BadRequestError(
            "日历地址必须使用 https://（拒绝明文 http 与其他协议）",
            details={"connector": CalendarConnectorService.connector_id, "field": "url"},
        )


def _split_userinfo(url: str) -> tuple[str, str, str]:
    """拆出 URL 里的 user:pass@，避免把凭据留在可展示的地址里。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url, "", ""
    if "@" not in parts.netloc:
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, "")), "", ""
    userinfo, host = parts.netloc.rsplit("@", 1)
    user, _, password = userinfo.partition(":")
    clean = urlunsplit((parts.scheme, host, parts.path, parts.query, ""))
    return clean, unquote(user), unquote(password)


def _normalize_timeout(value: Any) -> float:
    if value is None or value == "":
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise BadRequestError(
            "timeout 必须是秒数",
            details={"field": "timeout"},
        ) from exc
    return min(max(timeout, _MIN_TIMEOUT_SECONDS), _MAX_TIMEOUT_SECONDS)


def _basic_auth(credentials: dict[str, Any]) -> tuple[str, str] | None:
    username = str(credentials.get("username") or "")
    password = str(credentials.get("password") or "")
    if not username and not password:
        return None
    return username, password


def _url_host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").strip()
    except ValueError:
        return ""


# --- 上游响应 ---


def _raise_upstream(response: httpx.Response) -> None:
    status = response.status_code
    if status in (401, 403):
        raise ProviderError(
            "日历服务拒绝访问：用户名或密码可能不正确",
            details={"connector": CalendarConnectorService.connector_id, "status": status},
        )
    if status >= 500:
        raise ProviderError(
            f"日历服务暂时不可用（HTTP {status}）",
            details={"connector": CalendarConnectorService.connector_id, "status": status},
        )
    if status >= 400:
        raise ProviderError(
            f"日历服务返回 HTTP {status}",
            details={"connector": CalendarConnectorService.connector_id, "status": status},
        )


def _response_text(response: httpx.Response) -> str:
    content = response.content
    if len(content) > _MAX_RESPONSE_BYTES:
        raise ProviderError(
            "日历响应过大，已拒绝处理",
            details={"connector": CalendarConnectorService.connector_id},
        )
    content_type = response.headers.get("content-type", "").lower()
    text = content.decode("utf-8", errors="replace")
    stripped = text.lstrip()
    looks_xml = "xml" in content_type or stripped.startswith("<?xml") or stripped.startswith("<")
    if not looks_xml:
        return text
    try:
        return _calendar_data_from_xml(content)
    except ElementTree.ParseError as exc:
        raise ProviderError(
            "日历服务返回了无法解析的 XML",
            details={"connector": CalendarConnectorService.connector_id},
        ) from exc


def _calendar_data_from_xml(payload: bytes) -> str:
    root = ElementTree.fromstring(payload)
    chunks: list[str] = []
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "calendar-data" and element.text:
            chunks.append(element.text)
    return "\r\n".join(chunks)


# --- ICS 解析 ---


def parse_ics(text: str) -> list[CalendarEvent]:
    events: list[CalendarEvent] = []
    current: list[str] | None = None
    for line in _unfold_lines(text):
        marker = line.strip().upper()
        if marker == "BEGIN:VEVENT":
            current = []
            continue
        if marker == "END:VEVENT":
            if current is not None:
                event = _parse_vevent(current)
                if event is not None:
                    events.append(event)
            current = None
            continue
        if current is not None:
            current.append(line)
    return events


def _unfold_lines(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for line in normalized.split("\n"):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _parse_property(line: str) -> tuple[str, dict[str, str], str]:
    name_part, separator, value = line.partition(":")
    if not separator:
        return line.strip().upper(), {}, ""
    pieces = name_part.split(";")
    name = pieces[0].strip().upper()
    params: dict[str, str] = {}
    for piece in pieces[1:]:
        key, _, raw = piece.partition("=")
        params[key.strip().upper()] = raw.strip().strip('"')
    return name, params, value


def _parse_vevent(lines: list[str]) -> CalendarEvent | None:
    fields: dict[str, tuple[str, dict[str, str]]] = {}
    for line in lines:
        name, params, value = _parse_property(line)
        if not name or name in fields:
            continue
        fields[name] = (value, params)
    if "DTSTART" not in fields:
        return None
    start_info = _parse_ics_datetime(*fields["DTSTART"])
    if start_info is None:
        return None
    start, all_day, timezone_name = start_info

    end_info = _parse_ics_datetime(*fields["DTEND"]) if "DTEND" in fields else None
    if end_info is not None:
        end = end_info[0]
    else:
        duration = _parse_duration(fields.get("DURATION", ("", {}))[0])
        end = start + duration if duration is not None else start

    raw = "\r\n".join(lines)
    uid = _unescape_text(fields.get("UID", ("", {}))[0]).strip()
    if not uid:
        # 缺少 UID 的订阅仍要能被列出；用内容哈希保证同一条记录稳定。
        uid = "flux-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]
    return CalendarEvent(
        uid=uid,
        title=_unescape_text(fields.get("SUMMARY", ("", {}))[0]).strip(),
        start=start,
        end=end,
        description=_unescape_text(fields.get("DESCRIPTION", ("", {}))[0]).strip(),
        location=_unescape_text(fields.get("LOCATION", ("", {}))[0]).strip(),
        status=fields.get("STATUS", ("", {}))[0].strip().upper(),
        timezone_name=timezone_name,
        all_day=all_day,
        rrule=fields.get("RRULE", ("", {}))[0].strip(),
        raw=raw,
    )


def _parse_ics_datetime(value: str, params: dict[str, str]) -> tuple[datetime, bool, str] | None:
    match = _ICS_DATETIME_RE.match((value or "").strip())
    if match is None:
        return None
    year = int(match.group("year"))
    month = int(match.group("month"))
    day = int(match.group("day"))
    hour = match.group("hour")
    if hour is None:
        # 全天事件：按 UTC 零点参与窗口比较，并标记 all_day。
        return datetime(year, month, day, tzinfo=timezone.utc), True, ""
    minute = int(match.group("minute"))
    second = int(match.group("second"))
    tzid = params.get("TZID", "").strip()
    zone: Any = timezone.utc
    if not match.group("z") and tzid:
        try:
            zone = ZoneInfo(tzid)
        except (ZoneInfoNotFoundError, ValueError):
            # 未知 TZID：按 UTC 解释，避免整条事件被丢弃。
            zone = timezone.utc
    try:
        parsed = datetime(year, month, day, int(hour), minute, second, tzinfo=zone)
    except ValueError:
        return None
    label = "UTC" if match.group("z") else tzid
    return parsed.astimezone(timezone.utc), False, label


def _parse_duration(value: str) -> timedelta | None:
    match = _DURATION_RE.match((value or "").strip().upper())
    if match is None:
        return None
    parts = {
        key: int(match.group(key) or 0)
        for key in (
            "weeks",
            "days",
            "hours",
            "minutes",
            "seconds",
        )
    }
    if not any(parts.values()):
        return None
    duration = timedelta(**parts)
    return -duration if match.group("sign") == "-" else duration


def _unescape_text(value: str) -> str:
    output: list[str] = []
    index = 0
    mapping = {"n": "\n", "N": "\n", ",": ",", ";": ";", "\\": "\\"}
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            if nxt in mapping:
                output.append(mapping[nxt])
                index += 2
                continue
        output.append(char)
        index += 1
    return "".join(output)


def _escape_text(value: str) -> str:
    return (
        (value or "")
        .replace("\\", "\\\\")
        .replace("\r", "")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


# --- 写入 ---


def _build_vevent(
    *,
    uid: str,
    title: str,
    start: datetime,
    end: datetime,
    description: str,
    location: str,
) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Flux//Calendar Connector//EN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{_ics_utc(start)}",
        f"DTEND:{_ics_utc(end)}",
        f"SUMMARY:{_escape_text(title)}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{_escape_text(description)}")
    if location:
        lines.append(f"LOCATION:{_escape_text(location)}")
    lines.extend(["STATUS:CONFIRMED", "END:VEVENT", "END:VCALENDAR", ""])
    return "\r\n".join(lines)


def _ics_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_api_datetime(value: str | None, field: str) -> datetime:
    raw = (value or "").strip()
    if not raw:
        raise BadRequestError(f"{field} 不能为空", details={"field": field})
    normalized = raw[:-1] + "+00:00" if raw[-1:] in ("Z", "z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise BadRequestError(
            f"{field} 必须是 ISO-8601 时间",
            details={"field": field},
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


__all__ = [
    "MAX_EVENT_LIMIT",
    "MODE_CALDAV",
    "MODE_ICS",
    "CalendarConnectorService",
    "CalendarEvent",
    "normalize_credentials",
    "parse_ics",
]
