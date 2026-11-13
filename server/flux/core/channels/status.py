"""Bot IM 频道状态快照（只读）。

每个频道的配置要求来自频道自身已有的 fail-closed 规则：

- 入站：回调验签 / 解密需要的变量，以及把消息定位到 Bot 的 BOT_ID；
- 出站：审批通过后真正调用平台发送接口需要的应用凭据。

两套要求可能不同（例如企业微信入站只需要 token + EncodingAESKey，出站还需要
corp_id / agent_id / secret），所以状态里分别给出 `missing_inbound_env` 与
`missing_outbound_env`，再汇总成 `missing_env`。

对外只暴露**环境变量名**与布尔值，绝不返回值；运行时间线来自：

- 最近发送：`outbound_drafts` 中 status=sent 的 `sent_at`（跨重启持久）；
- 最近接收：`ChannelActivityRegistry` 的进程内登记（重启清空）；
- 最近错误：发送失败草稿 + 进程内入站错误，按时间倒序合并。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from flux.config import Settings
from flux.core.channels.activity import ChannelActivityRegistry, sanitize_error
from flux.core.channels.outbound_drafts import OutboundDraftService

__all__ = [
    "CHANNEL_SPECS",
    "ChannelSpec",
    "ChannelStatusService",
    "collect_channel_statuses",
]

#: 状态接口最近错误条数上限
RECENT_ERRORS_LIMIT = 5


@dataclass(frozen=True)
class ChannelSpec:
    """一条 Bot IM 频道的配置要求与展示名。"""

    name: str
    label: str
    inbound_fields: tuple[str, ...]
    outbound_fields: tuple[str, ...]
    optional_fields: tuple[str, ...] = ()

    @property
    def required_fields(self) -> tuple[str, ...]:
        """去重后的必填字段（保持配置顺序）。"""
        seen: dict[str, None] = {}
        for field_name in (*self.inbound_fields, *self.outbound_fields):
            seen.setdefault(field_name, None)
        return tuple(seen)


#: 已存在、且能干净接入状态接口的 Bot IM 频道。
#: 字段名是 Settings 属性名；对应的环境变量名由 `_env_name` 推导。
CHANNEL_SPECS: tuple[ChannelSpec, ...] = (
    ChannelSpec(
        name="feishu",
        label="飞书",
        inbound_fields=("feishu_verification_token", "feishu_bot_id"),
        outbound_fields=("feishu_app_id", "feishu_app_secret"),
        optional_fields=("feishu_encrypt_key",),
    ),
    ChannelSpec(
        name="wecom",
        label="企业微信",
        inbound_fields=("wecom_token", "wecom_encoding_aes_key", "wecom_bot_id"),
        outbound_fields=("wecom_corp_id", "wecom_agent_id", "wecom_secret"),
    ),
    ChannelSpec(
        name="dingtalk",
        label="钉钉",
        inbound_fields=(
            "dingtalk_token",
            "dingtalk_aes_key",
            "dingtalk_app_key",
            "dingtalk_bot_id",
        ),
        outbound_fields=("dingtalk_app_key", "dingtalk_app_secret"),
    ),
)


def _env_name(field_name: str) -> str:
    """Settings 属性名 → 环境变量名（pydantic-settings 的 FLUX_ 前缀）。"""
    return f"FLUX_{field_name.upper()}"


def _present(value: object) -> bool:
    """只判断是否配置：None / 空串 / 纯空白都算缺失，绝不读取或返回真实值。"""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _as_utc(value: datetime | None) -> datetime | None:
    """统一成带时区的 UTC：SQLite 读回的时间可能是 naive。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    normalized = _as_utc(value)
    return normalized.isoformat() if normalized is not None else None


def config_status(missing: list[str], present_count: int) -> str:
    """configured / partial / missing 三态。"""
    if not missing:
        return "configured"
    if present_count == 0:
        return "missing"
    return "partial"


class ChannelStatusService:
    """把配置、出站草稿与进程内活动拼成只读状态。"""

    def __init__(
        self,
        settings: Settings,
        activity: ChannelActivityRegistry,
        outbound_drafts: OutboundDraftService,
    ) -> None:
        self._settings = settings
        self._activity = activity
        self._outbound_drafts = outbound_drafts

    async def collect(self) -> list[dict[str, Any]]:
        return [await self._one(spec) for spec in CHANNEL_SPECS]

    async def _one(self, spec: ChannelSpec) -> dict[str, Any]:
        missing_inbound = [
            _env_name(field)
            for field in spec.inbound_fields
            if not _present(getattr(self._settings, field, None))
        ]
        missing_outbound = [
            _env_name(field)
            for field in spec.outbound_fields
            if not _present(getattr(self._settings, field, None))
        ]
        missing: list[str] = []
        for name in (*missing_inbound, *missing_outbound):
            if name not in missing:
                missing.append(name)

        required = spec.required_fields
        present_count = len(required) - len(missing)
        last_send_at = await self._outbound_drafts.latest_sent_at(spec.name)
        failures = await self._outbound_drafts.recent_failures(spec.name, limit=RECENT_ERRORS_LIMIT)

        errors: list[dict[str, Any]] = []
        for row in failures:
            errors.append(
                {
                    "time": _iso(row.resolved_at or row.updated_at or row.created_at),
                    "code": "outbound_send_failed",
                    "message": sanitize_error(row.error or "发送失败"),
                    "source": "outbound",
                }
            )
        for record in self._activity.recent_errors(spec.name, limit=RECENT_ERRORS_LIMIT):
            errors.append(
                {
                    "time": _iso(record.at),
                    "code": record.code,
                    "message": record.message,
                    "source": record.source,
                }
            )
        # 两路来源时区表示可能不同，先统一成 UTC 再按时间倒序截断。
        errors.sort(key=lambda item: item["time"] or "", reverse=True)

        return {
            "channel": spec.name,
            "label": spec.label,
            "config_status": config_status(missing, present_count),
            "inbound_configured": not missing_inbound,
            "outbound_configured": not missing_outbound,
            "missing_env": missing,
            "missing_inbound_env": missing_inbound,
            "missing_outbound_env": missing_outbound,
            "last_send_at": _iso(last_send_at),
            "last_receive_at": _iso(self._activity.last_receive_at(spec.name)),
            "recent_errors": errors[:RECENT_ERRORS_LIMIT],
        }


async def collect_channel_statuses(
    settings: Settings,
    activity: ChannelActivityRegistry,
    outbound_drafts: OutboundDraftService,
) -> list[dict[str, Any]]:
    """便捷入口（API 层与测试共用）。"""
    return await ChannelStatusService(settings, activity, outbound_drafts).collect()
