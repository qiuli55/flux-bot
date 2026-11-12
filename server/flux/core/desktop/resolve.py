"""桌面入口地址与凭据的别名解析。

两个分支各自引入了一组配置名，指向同一组事实（环境变量由 pydantic-settings
统一加 ``FLUX_`` 前缀）：

====================  ==========================  ===============================
事实                  主用字段（更具体）           兼容别名
====================  ==========================  ===============================
网关 / 主机地址        ``desktop_host``             ``desktop_gateway``
                     ``FLUX_DESKTOP_HOST``        ``FLUX_DESKTOP_GATEWAY``
网桥地址               ``desktop_bridge``           ``desktop_bridge_url``
                     ``FLUX_DESKTOP_BRIDGE``      ``FLUX_DESKTOP_BRIDGE_URL``
长期访问令牌           ``desktop_gateway_token``    ``desktop_token``
                     ``FLUX_DESKTOP_GATEWAY_TOKEN`` ``FLUX_DESKTOP_TOKEN``
网桥密钥               ``desktop_bridge_key``       （无别名）
                     ``FLUX_DESKTOP_BRIDGE_KEY``
====================  ==========================  ===============================

解析顺序：

1. 公开的 ``desktop.json``（只覆盖网关 / 网桥地址；``DesktopService`` 负责抓取）；
2. 主用字段；
3. 兼容别名。

空白值一律视为未设置，因此 ``FLUX_DESKTOP_HOST=""`` 不会挡住
``FLUX_DESKTOP_GATEWAY``。凭据只从环境变量读取，从不来自 ``desktop.json``。
"""

from __future__ import annotations

from dataclasses import dataclass

from flux.config import Settings


def _first_non_empty(*values: object) -> str:
    """按顺序返回第一个非空白字符串；全部为空时返回空串。"""
    for value in values:
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
    return ""


@dataclass(frozen=True)
class DesktopEndpoints:
    """解析后的桌面连接事实；空字符串表示对应项未配置。"""

    gateway: str = ""
    bridge: str = ""
    gateway_token: str = ""
    bridge_key: str = ""
    config_url: str = ""
    tunnel_updated_at: str = ""

    @property
    def has_urls(self) -> bool:
        return bool(self.gateway and self.bridge)

    @property
    def has_long_lived_credentials(self) -> bool:
        """长期凭据必须成对存在，避免下发半个连接参数。"""
        return bool(self.gateway_token and self.bridge_key)

    def configured(self, ticket_secret: str | None) -> bool:
        """地址齐全，且要么有长期凭据，要么能签发短期票据。"""
        return self.has_urls and (
            self.has_long_lived_credentials or bool((ticket_secret or "").strip())
        )


def resolve_desktop_endpoints(
    settings: Settings, public: dict[str, object] | None = None
) -> DesktopEndpoints:
    """合并 ``desktop.json`` 与 env 别名，返回唯一的桌面连接事实。"""
    data = public or {}
    gateway = _first_non_empty(data.get("gateway"), settings.desktop_host, settings.desktop_gateway)
    bridge = _first_non_empty(
        data.get("bridge"), settings.desktop_bridge, settings.desktop_bridge_url
    ).rstrip("/")
    return DesktopEndpoints(
        gateway=gateway,
        bridge=bridge,
        gateway_token=_first_non_empty(settings.desktop_gateway_token, settings.desktop_token),
        bridge_key=_first_non_empty(settings.desktop_bridge_key),
        config_url=_first_non_empty(settings.desktop_config_url),
        tunnel_updated_at=_first_non_empty(data.get("updated")),
    )


__all__ = ["DesktopEndpoints", "resolve_desktop_endpoints"]
