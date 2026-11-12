"""用户账号连接器框架（catalog / install / uninstall / 凭据隔离）。"""

from flux.core.connectors.framework import (
    AUTH_API_KEY,
    AUTH_OAUTH,
    ConnectorCatalog,
    ConnectorDefinition,
    ConnectorFrameworkService,
    build_default_catalog,
)

__all__ = [
    "AUTH_API_KEY",
    "AUTH_OAUTH",
    "ConnectorCatalog",
    "ConnectorDefinition",
    "ConnectorFrameworkService",
    "build_default_catalog",
]
